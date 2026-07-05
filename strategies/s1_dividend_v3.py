# -*- coding: utf-8 -*-
"""S1 红利低波 V3 —— Barra 7因子增强版（P0升级）。

基于 factors.py 因子库 + riskmodel.py 风险模型 + macro.py 宏观模块:
- 7因子: VALUE(0.2)+QUALITY(0.15)+LOW_VOL(0.15)+SIZE(0.1)+BETA(-,0.15)+EARNINGS_YIELD(0.15)+LEVERAGE(-,0.1)
- 流水线: 去极值(MAD)→标准化(z-score)→正交化(Gram-Schmidt)消除共线性
- 最终评分 = 加权求和后归一化到 -1~1
- BETA负向=偏好低Beta防御股; LEVERAGE负向=偏好低杠杆公司
- 宏观适配: 收缩期自动提高LOW_VOL/BETA/LEVERAGE负向权重
- 风险控制: 特质风险占比>30%时降低仓位
- 延续 v2 门槛: 股息率>=4% + 连续3年分红 + ROE>8%
- 月末调仓, 等权持有
"""
import logging
import numpy as np
import pandas as pd
import fundamental as F
import factors
import riskmodel
import macro
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s1")
POOL_INDEX = "sh000300"


class S1DividendV3(BaseStrategy):
    """S1 v3: Barra 7因子增强红利低波策略（P0升级）。

    与 v2 差异:
    - 排名法 -> Barra式去极值/标准化/正交化复合因子
    - 4因子 -> 7因子（新加入 BETA反向/EARNINGS_YIELD/LEVERAGE反向）
    - 取消"低波后30%"硬截断, 低波因子以-VOLATILITY复合入评分
    - 新增行业集中度约束(max_per_industry)
    - 新增风险模型仓位调节(特质风险>30%降仓)
    - 新增宏观regime自适应: 收缩期自动提高防御因子权重
    - BETA因子: 反向使用(偏好低Beta防御股), 适配红利低波定位
    - EARNINGS_YIELD: 与VALUE互补衡量估值
    - LEVERAGE: 反向使用(偏好低杠杆), 与QUALITY互补
    """
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        # ── 参数 ──
        min_dy = self.params.get("min_dividend_yield", 0.04)
        div_years = self.params.get("dividend_years", 3)
        roe_years = self.params.get("roe_years", 3)
        roe_min = self.params.get("roe_min", 0.08)
        # 7因子权重: VALUE/QUALITY/LOW_VOL/SIZE/BETA/EARNINGS_YIELD/LEVERAGE
        fw = self.params.get("factor_weights", {
            "VALUE": 0.20, "QUALITY": 0.15, "LOW_VOL": 0.15, "SIZE": 0.10,
            "BETA": 0.15, "EARNINGS_YIELD": 0.15, "LEVERAGE": 0.10,
        })
        hold_n = self.params.get("hold_n", 10)
        max_per_industry = self.params.get("max_per_industry", 2)

        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        # ── 股票池 ──
        pool = ctx.members(POOL_INDEX, date)
        if not pool:
            return []

        # ── 门槛过滤（复用 v2 逻辑） ──
        valid_codes = []
        for code in pool:
            if not ctx.is_tradable(code, date):
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("dividend_yield") or f["dividend_yield"] < min_dy:
                continue
            if ctx.dividend_years(code, div_years) < div_years:
                continue
            ok, roe = F.roe_quality(code, date, years=roe_years, min_roe=roe_min, conn=ctx.conn)
            if not ok:
                continue
            valid_codes.append(code)

        if len(valid_codes) < eff:
            return []

        # ── 宏观 regime 自适应 ──
        try:
            regime = macro.detect_regime(date, conn=ctx.conn)
            ms = macro.macro_score(date, conn=ctx.conn)
            mf = macro.macro_factor(date, conn=ctx.conn)
        except Exception:
            regime = "neutral"
            ms = 0.0
            mf = {}
        log.info("s1_v3: macro regime=%s score=%+.2f, date=%s", regime, ms, date)

        # ── 因子暴露（全池截面，pipeline: 去极值→标准化→正交化） ──
        all_exposures = factors.compute_factor_exposures(pool, date, conn=ctx.conn)
        if all_exposures.empty:
            log.warning("s1_v3: 因子暴露为空, date=%s", date)
            return []

        # 筛选到通过门槛的股票
        exposures = all_exposures.reindex(valid_codes)
        if exposures.dropna(how="all").empty:
            return []

        # ── 宏观 regime 自适应权重调整 ──
        # 收缩期: 提高防御属性(低波/低Beta/低杠杆), 降低SIZE
        adj_fw = dict(fw)
        if regime == "contraction":
            adj_fw["LOW_VOL"] = adj_fw.get("LOW_VOL", 0.15) * 1.3
            adj_fw["BETA"] = adj_fw.get("BETA", 0.15) * 1.2
            adj_fw["LEVERAGE"] = adj_fw.get("LEVERAGE", 0.10) * 1.2
            adj_fw["VALUE"] = adj_fw.get("VALUE", 0.20) * 1.1
            log.debug("s1_v3: 收缩期防御加权 applied")
        elif regime == "expansion":
            adj_fw["QUALITY"] = adj_fw.get("QUALITY", 0.15) * 1.2
            adj_fw["EARNINGS_YIELD"] = adj_fw.get("EARNINGS_YIELD", 0.15) * 1.1
            log.debug("s1_v3: 扩张期质量加权 applied")

        # 归一化权重
        total_w = sum(adj_fw.values())
        adj_fw = {k: v / total_w for k, v in adj_fw.items()}

        # ── 计算综合评分 ──
        # VALUE(正向) + QUALITY(正向) + LOW_VOL(负向=-VOLATILITY) + SIZE(正向偏大盘)
        # + BETA(负向=偏好低Beta防御) + EARNINGS_YIELD(正向) + LEVERAGE(负向=偏好低杠杆)
        score = pd.Series(0.0, index=exposures.index)
        # 正向因子
        positive_factors = ["VALUE", "QUALITY", "SIZE", "EARNINGS_YIELD"]
        for fac in positive_factors:
            w = adj_fw.get(fac, 0)
            if w == 0:
                continue
            col = fac
            if col in exposures.columns:
                score += w * exposures[col].fillna(0)
            else:
                log.debug("s1_v3: 正向因子 %s 不在暴露矩阵中, 跳过", fac)

        # 负向因子（反向使用取负号）
        negative_factors = {
            "LOW_VOL": "VOLATILITY",   # 低波=偏好低波动，VOLATILITY取负
            "BETA": "BETA",            # 偏好低Beta防御
            "LEVERAGE": "LEVERAGE",    # 偏好低杠杆
        }
        for fac_name, col_name in negative_factors.items():
            w = adj_fw.get(fac_name, 0)
            if w == 0:
                continue
            if col_name in exposures.columns:
                score += w * (-exposures[col_name].fillna(0))
            else:
                log.debug("s1_v3: 负向因子 %s(col=%s) 不在暴露矩阵中, 跳过", fac_name, col_name)

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

        # ── 风险控制：特质风险占比 > 30% 则降仓位 ──
        orig_eff = eff
        try:
            rm = riskmodel.estimate_cached(ctx.conn, date, pool=pool)
            if rm is not None:
                weights_dict = {c: 1.0 / len(target) for c in target}
                sys_risk = rm.calc_systematic_risk(weights_dict)
                spec_risk = rm.calc_specific_risk(weights_dict)
                total_var = sys_risk + spec_risk
                if total_var > 0 and spec_risk / total_var > 0.30:
                    spec_pct = spec_risk / total_var * 100
                    log.info("s1_v3: 特质风险占比%.1f%% > 30%%, 降低仓位", spec_pct)
                    eff = max(3, int(orig_eff * 0.7))
                    target = target[:eff]
                    w = common.target_weight(eff)
        except Exception as e:
            log.debug("s1_v3: 风险模型调用失败(非致命): %s", e)

        # ── 排名信息（供理由展示） ──
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
                    reason = (f"红利7因子调仓:{nm}综合排名第{rank_map[code]}/{n_total}"
                              f"掉出前{len(target)},卖出")
                else:
                    reason = f"红利7因子:{nm}不再满足股息率/连续分红/ROE门槛,卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                nm = ctx.name(code)
                rk = rank_map.get(code, "?")
                ind = industry_map.get(code, "未知")
                s_val = score.get(code, 0)
                f_data = ctx.fundamental(code)
                dy_val = f_data.get("dividend_yield", 0) if f_data else 0
                orders.append(Order(self.strategy_id, code, "buy", w,
                    f"红利7因子:买入{nm}(股息率{dy_val:.1%}·Score{s_val:+.2f}·"
                    f"第{rk}/{n_total}·行业:{ind}·regime:{regime})", date))
        return orders
