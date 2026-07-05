# -*- coding: utf-8 -*-
"""S4 小市值多因子 V2 —— Barra 7因子增强版（P0升级）。

基于 factors.py 因子库:
- 7因子: -SIZE(0.2)+MOMENTUM(0.2)+VALUE(0.15)+LIQUIDITY(0.1)+BETA(0.15)+EARNINGS_YIELD(0.1)+QUALITY(0.1)
- 流水线: 去极值(MAD)→标准化(z-score)→正交化(Gram-Schmidt)
- 最终评分 = 加权求和后归一化到 -1~1
- SIZE负向=偏好小市值; BETA正向=牛市时偏好高弹性股
- 宏观适配: 扩张期自动提高MOMENTUM/BETA权重, 收缩期提高VALUE/QUALITY防御权重
- 行业动量倾斜: 根据申万31行业近60日涨幅排名计算行业动量加分
- 风险过滤: 剔除残差波动 z>1.28(约最高10%)的高波股
- 行业集中度约束: max_per_industry

与 v1 差异:
- 20日动量 -> RSTR 12-1月动量(MOMENTUM因子)
- PB排名 -> BTOP z分 + 加入VALUE复合理念
- 市值排名 -> -SIZE z分(小市值正向)
- 新增 LIQUIDITY/BETA/EARNINGS_YIELD/QUALITY 因子
- 新增宏观regime自适应 + 行业动量倾斜
- 新增残差波动帽与行业约束
- 月调仓, 等权持有
"""
import logging
import numpy as np
import pandas as pd
import factors
import riskmodel
import macro
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s4")
POOL_INDEX = "sh000300"


class S4SmallcapV2(BaseStrategy):
    """S4 v2: 小市值多因子价值增强(Barra 7因子, P0升级)。

    名称展示: "多因子价值增强(沪深300)".
    """
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        # ── 参数 ──
        fw = self.params.get("factor_weights", {
            "SIZE": 0.20, "MOMENTUM": 0.20, "VALUE": 0.15, "LIQUIDITY": 0.10,
            "BETA": 0.15, "EARNINGS_YIELD": 0.10, "QUALITY": 0.10,
        })
        resvol_cap_z = self.params.get("resvol_cap_z", 1.28)  # 残差波动帽
        hold_n = self.params.get("hold_n", 20)
        max_per_industry = self.params.get("max_per_industry", 3)

        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        # ── 股票池 ──
        pool = ctx.members(POOL_INDEX, date)
        if not pool:
            return []

        # ── 过滤: 可交易 + 上市满1年(近似250日K线) + 基本面有效 ──
        valid_codes = []
        for code in pool:
            if not ctx.is_tradable(code, date):
                continue
            c = ctx.close(code, 260)
            if len(c) < 250:
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("market_cap") or not f.get("pb") or f["pb"] <= 0:
                continue
            if not f.get("pe") or f["pe"] == 0:
                # PE可负(保留), 仅排除缺失/0
                pass
            valid_codes.append(code)

        if len(valid_codes) < eff:
            return []

        # ── 因子暴露（全池截面） ──
        all_exposures = factors.compute_factor_exposures(pool, date, conn=ctx.conn)
        if all_exposures.empty:
            log.warning("s4_v2: 因子暴露为空, date=%s", date)
            return []

        exposures = all_exposures.reindex(valid_codes)
        if exposures.dropna(how="all").empty:
            return []

        # ── 宏观 regime 自适应 ──
        try:
            regime = macro.detect_regime(date, conn=ctx.conn)
        except Exception:
            regime = "neutral"
        log.info("s4_v2: macro regime=%s, date=%s", regime, date)

        # ── 风险过滤: 剔除残差波动 z > resvol_cap_z ──
        if "VOLATILITY" in exposures.columns:
            vol_z = exposures["VOLATILITY"]
            before = len(exposures)
            exposures = exposures[vol_z.fillna(0) <= resvol_cap_z]
            after = len(exposures)
            if after < before:
                log.debug("s4_v2: 残差波动过滤剔除 %d 股 (z>%.2f)", before - after, resvol_cap_z)

        if exposures.empty or len(exposures) < eff:
            return []

        valid_codes = list(exposures.index)

        # ── 宏观 regime 自适应权重调整 ──
        adj_fw = dict(fw)
        if regime == "expansion":
            adj_fw["MOMENTUM"] = adj_fw.get("MOMENTUM", 0.20) * 1.3
            adj_fw["BETA"] = adj_fw.get("BETA", 0.15) * 1.2
            adj_fw["QUALITY"] = adj_fw.get("QUALITY", 0.10) * 0.8
            log.debug("s4_v2: 扩张期动量弹性加权 applied")
        elif regime == "contraction":
            adj_fw["VALUE"] = adj_fw.get("VALUE", 0.15) * 1.3
            adj_fw["EARNINGS_YIELD"] = adj_fw.get("EARNINGS_YIELD", 0.10) * 1.2
            adj_fw["QUALITY"] = adj_fw.get("QUALITY", 0.10) * 1.2
            adj_fw["MOMENTUM"] = adj_fw.get("MOMENTUM", 0.20) * 0.7
            adj_fw["BETA"] = adj_fw.get("BETA", 0.15) * 0.5
            log.debug("s4_v2: 收缩期防御加权 applied")

        total_w = sum(adj_fw.values())
        adj_fw = {k: v / total_w for k, v in adj_fw.items()}

        # ── 行业动量倾斜加分 ──
        try:
            sector_momentum = macro.industry_momentum(date, lookback=60, conn=ctx.conn)
        except Exception:
            sector_momentum = {}
        # 提前获取 industry_map（行业动量+贪心选取共用）
        industry_map = factors.get_industry(ctx.conn, valid_codes)

        # ── 计算综合评分 ──
        # SIZE(负向=小市值) + MOMENTUM(正向) + VALUE(正向) + LIQUIDITY(正向)
        # + BETA(正向=牛市高弹性) + EARNINGS_YIELD(正向) + QUALITY(正向)
        score = pd.Series(0.0, index=exposures.index)

        # 正向因子
        for fac in ["MOMENTUM", "VALUE", "LIQUIDITY", "BETA", "EARNINGS_YIELD", "QUALITY"]:
            w = adj_fw.get(fac, 0)
            if w == 0:
                continue
            if fac in exposures.columns:
                score += w * exposures[fac].fillna(0)

        # SIZE: 负向(小市值正向)
        size_w = adj_fw.get("SIZE", 0)
        if size_w != 0 and "SIZE" in exposures.columns:
            score += size_w * (-exposures["SIZE"].fillna(0))

        # 行业动量倾斜: 所属行业近期涨幅排名前30%的股票加分
        if sector_momentum and industry_map:
            n_industries = len(sector_momentum)
            if n_industries > 0:
                top_threshold = max(1, int(n_industries * 0.3))
                top_sectors = set(sorted(sector_momentum, key=sector_momentum.get, reverse=True)[:top_threshold])
                for code in score.index:
                    ind = industry_map.get(code, "")
                    if ind in top_sectors:
                        score[code] += 0.15  # 行业动量加分

        # 归一化到 -1~1
        smax = score.abs().max()
        if smax is not None and smax > 1e-9:
            score = score / smax
        else:
            return []

        # ── 行业约束贪心选取 ──
        sorted_codes = score.sort_values(ascending=False)
        target = []
        industry_count = {}
        for code in sorted_codes.index:
            if code not in exposures.index or pd.isna(score.get(code)):
                continue
            ind = industry_map.get(code, "未知")
            if industry_count.get(ind, 0) >= max_per_industry:
                continue
            target.append(code)
            industry_count[ind] = industry_count.get(ind, 0) + 1
            if len(target) >= eff:
                break

        if not target:
            return []

        # ── 风险模型暴露摘要(供理由展示) ──
        try:
            rm = riskmodel.estimate_cached(ctx.conn, date, pool=pool)
            if rm is not None:
                weights_dict = {c: 1.0 / len(target) for c in target}
                port_exposure = rm.portfolio_exposure(weights_dict)
                pred_vol = rm.calc_portfolio_risk(weights_dict)
                log.info("s4_v2: 组合预测波动 %.1f%%", pred_vol * 100)
        except Exception as e:
            log.debug("s4_v2: 风险模型调用失败(非致命): %s", e)

        # ── 排名(供理由) ──
        rank_map = {}
        for i, code in enumerate(sorted_codes.index):
            rank_map[code] = i + 1
        n_total = len(sorted_codes)

        # ── 生成订单 ──
        held = set(account.positions.keys())
        orders = []
        for code in held:
            if code not in target:
                nm = ctx.name(code)
                if code in rank_map:
                    reason = (f"多因子7F调仓:{nm}综合排名第{rank_map[code]}/{n_total}"
                              f"掉出前{len(target)},卖出")
                else:
                    reason = f"多因子7F:{nm}掉出候选池(流动性/波动/门槛过滤),卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                nm = ctx.name(code)
                rk = rank_map.get(code, "?")
                ind = industry_map.get(code, "未知")
                s_val = score.get(code, 0)
                f_data = ctx.fundamental(code)
                mcap = f_data.get("market_cap", 0) if f_data else 0
                orders.append(Order(self.strategy_id, code, "buy", w,
                    f"多因子7F:买入{nm}(市值{mcap/1e8:.0f}亿·Score{s_val:+.2f}·"
                    f"第{rk}/{n_total}·行业:{ind}·regime:{regime})", date))
        return orders
