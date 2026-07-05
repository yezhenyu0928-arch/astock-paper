# -*- coding: utf-8 -*-
"""S1 红利低波 V3 —— Barra 风格因子增强版。

基于 factors.py 因子库 + riskmodel.py 风险模型:
- 因子: VALUE(0.4) + QUALITY(0.3) + LOW_VOL(反向, 0.2) + SIZE偏大盘(0.1)
- 流水线: 去极值(MAD)→标准化(z-score)→正交化(Gram-Schmidt)
- 最终评分 = 加权求和后归一化到 -1~1
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
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s1")
POOL_INDEX = "sh000300"


class S1DividendV3(BaseStrategy):
    """S1 v3: Barra多因子增强红利低波策略。

    与 v2 差异:
    - 排名法 -> Barra式去极值/标准化/正交化复合因子
    - 因子来源: factors.compute_factor_exposures() + pipeline()
    - 取消"低波后30%"硬截断, 低波因子以-VOLATILITY复合入评分
    - 新增行业集中度约束(max_per_industry)
    - 新增风险模型仓位调节(特质风险>30%降仓)
    """
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        # ── 参数 ──
        min_dy = self.params.get("min_dividend_yield", 0.04)
        div_years = self.params.get("dividend_years", 3)
        roe_years = self.params.get("roe_years", 3)
        roe_min = self.params.get("roe_min", 0.08)
        # 因子权重: VALUE/QUALITY/LOW_VOL/SIZE
        fw = self.params.get("factor_weights", {"VALUE": 0.4, "QUALITY": 0.3, "LOW_VOL": 0.2, "SIZE": 0.1})
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

        # ── 因子暴露（全池截面，pipeline: 去极值→标准化→正交化） ──
        all_exposures = factors.compute_factor_exposures(pool, date, conn=ctx.conn)
        if all_exposures.empty:
            log.warning("s1_v3: 因子暴露为空, date=%s", date)
            return []

        # 筛选到通过门槛的股票
        exposures = all_exposures.reindex(valid_codes)
        if exposures.dropna(how="all").empty:
            return []

        # ── 计算综合评分 ──
        # VALUE 正向, QUALITY 正向, LOW_VOL=负向VOLATILITY, SIZE 正向(偏大盘)
        score = pd.Series(0.0, index=exposures.index)
        for factor_name, factor_weight in fw.items():
            if factor_name == "LOW_VOL":
                col = "VOLATILITY"
                if col in exposures.columns:
                    score += factor_weight * (-exposures[col].fillna(0))
            elif factor_name in exposures.columns:
                score += factor_weight * exposures[factor_name].fillna(0)
            else:
                log.debug("s1_v3: 因子 %s 不在暴露矩阵中, 跳过", factor_name)

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
                    reason = (f"红利Barra调仓:{nm}综合排名第{rank_map[code]}/{n_total}"
                              f"掉出前{len(target)},卖出")
                else:
                    reason = f"红利Barra:{nm}不再满足股息率/连续分红/ROE门槛,卖出"
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
                    f"红利Barra:买入{nm}(股息率{dy_val:.1%}·Score{s_val:+.2f}·"
                    f"第{rk}/{n_total}·行业:{ind})", date))
        return orders
