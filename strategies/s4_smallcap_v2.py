# -*- coding: utf-8 -*-
"""S4 小市值多因子 V2 —— Barra 风格因子增强版。

基于 factors.py 因子库:
- 因子: SIZE(0.3, 负向偏小市值) + MOMENTUM(0.3) + VALUE(0.2) + LIQUIDITY(0.2)
- 流水线: 去极值(MAD)→标准化(z-score)→正交化(Gram-Schmidt)
- 最终评分 = 加权求和后归一化到 -1~1
- 风险过滤: 剔除残差波动 z>1.28(约最高10%)的高波股
- 行业集中度约束: max_per_industry

与 v1 差异:
- 20日动量 -> RSTR 12-1月动量(MOMENTUM因子)
- PB排名 -> BTOP z分 + 加入VALUE复合理念
- 市值排名 -> -SIZE z分(小市值正向)
- 新增 LIQUIDITY 因子
- 新增残差波动帽与行业约束
- 月调仓, 等权持有
"""
import logging
import numpy as np
import pandas as pd
import factors
import riskmodel
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s4")
POOL_INDEX = "sh000300"


class S4SmallcapV2(BaseStrategy):
    """S4 v2: 小市值多因子价值增强(Barra)。

    名称展示: "多因子价值增强(沪深300)".
    """
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        # ── 参数 ──
        fw = self.params.get("factor_weights", {"SIZE": 0.3, "MOMENTUM": 0.3, "VALUE": 0.2, "LIQUIDITY": 0.2})
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

        # ── 计算综合评分 ──
        # SIZE: 负向(小市值正向), MOMENTUM: 正向, VALUE: 正向, LIQUIDITY: 正向(偏好高流动性)
        score = pd.Series(0.0, index=exposures.index)
        for factor_name, factor_weight in fw.items():
            if factor_name == "SIZE":
                col = "SIZE"
                if col in exposures.columns:
                    # SIZE 因子中 ln市值 越大值越大, 小市值策略要取负
                    score += factor_weight * (-exposures[col].fillna(0))
            elif factor_name in exposures.columns:
                score += factor_weight * exposures[factor_name].fillna(0)
            else:
                log.debug("s4_v2: 因子 %s 不在暴露矩阵中, 跳过", factor_name)

        # 归一化到 -1~1
        smax = score.abs().max()
        if smax is not None and smax > 1e-9:
            score = score / smax
        else:
            return []

        # ── 行业约束贪心选取 ──
        industry_map = factors.get_industry(ctx.conn, valid_codes)
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
                    reason = (f"多因子价值调仓:{nm}综合排名第{rank_map[code]}/{n_total}"
                              f"掉出前{len(target)},卖出")
                else:
                    reason = f"多因子价值:{nm}掉出候选池(流动性/波动/门槛过滤),卖出"
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
                    f"多因子价值:买入{nm}(市值{mcap/1e8:.0f}亿·Score{s_val:+.2f}·"
                    f"第{rk}/{n_total}·行业:{ind})", date))
        return orders
