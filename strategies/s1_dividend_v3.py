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
import atexit
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

# ── TEMP-DIAG(排查CI全程零交易根因,查完即删,见 docs/OPTIMIZE_V4.md 附录) ──
_DIAG = {"calls": 0, "gate_no_pool": 0, "gate_valid_codes_too_few": 0,
         "gate_exposures_empty": 0, "gate_dropna_empty": 0, "gate_smax_zero": 0,
         "success": 0, "valid_codes_samples": [], "eff_samples": [],
         "exposures_shape_samples": [], "smax_samples": []}


def _diag_dump():
    try:
        import datetime
        with open("reports/s1v3_diag.txt", "a", encoding="utf-8") as f:
            f.write(f"\n=== dump @ {datetime.datetime.now().isoformat()} pid={__import__('os').getpid()} ===\n")
            for k, v in _DIAG.items():
                f.write(f"{k}: {v}\n")
    except Exception as e:
        pass


atexit.register(_diag_dump)


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

        if _DIAG["calls"] == 0:   # 只在第一次调用时记录实际生效的参数值(排查参数加载问题)
            try:
                with open("reports/s1v3_diag.txt", "a", encoding="utf-8") as fp:
                    # 新假设排查:stock_annual 表在当前CI库的真实覆盖度(不经roe_quality间接推断,直查表)
                    try:
                        total_rows = ctx.conn.execute("SELECT COUNT(1) FROM stock_annual").fetchone()[0]
                        distinct_codes = ctx.conn.execute("SELECT COUNT(DISTINCT code) FROM stock_annual").fetchone()[0]
                        date_range = ctx.conn.execute("SELECT MIN(pub_date), MAX(pub_date) FROM stock_annual WHERE pub_date IS NOT NULL AND pub_date<>''").fetchone()
                        sh600000_rows = ctx.conn.execute("SELECT stat_year, roe, net_profit, pub_date FROM stock_annual WHERE code='sh600000' ORDER BY stat_year DESC").fetchall()
                        stat_year_range = ctx.conn.execute("SELECT MIN(stat_year), MAX(stat_year) FROM stock_annual").fetchone()
                        fp.write(f"\n=== stock_annual 表真实覆盖度诊断(直查,不经roe_quality) ===\n"
                                 f"  总行数={total_rows}  不同code数={distinct_codes}\n"
                                 f"  pub_date范围={date_range}  stat_year范围={stat_year_range}\n"
                                 f"  sh600000(浦发银行)全部年报行={sh600000_rows}\n")
                    except Exception as e:
                        fp.write(f"\n=== stock_annual诊断查询异常: {e} ===\n")
                    fp.write(f"\n--- params@first-call date={date} ---\n"
                             f"  self.params raw = {dict(self.params)}\n"
                             f"  min_dy={min_dy!r} div_years={div_years!r} "
                             f"roe_years={roe_years!r} roe_min={roe_min!r}\n"
                             f"  hold_n={hold_n!r} eff={eff!r} capital={account.init_capital!r}\n"
                             f"  strategy_id={self.strategy_id!r}\n")
            except Exception:
                pass

        # ── 股票池 ──
        _DIAG["calls"] += 1
        pool = ctx.members(POOL_INDEX, date)
        if not pool:
            _DIAG["gate_no_pool"] += 1
            return []

        # ── 门槛过滤（复用 v2 逻辑） ──
        detail = _DIAG["calls"] <= 2   # 只对前2次调用做逐股细分,避免输出过大
        if detail:
            reasons = {"not_tradable": 0, "no_fund_or_dy_low": 0, "div_years_fail": 0,
                       "roe_fail": 0, "pass": 0, "fund_none": 0, "dy_missing_key": 0,
                       "sample_fund": None, "sample_roe": None}
        valid_codes = []
        for code in pool:
            if not ctx.is_tradable(code, date):
                if detail:
                    reasons["not_tradable"] += 1
                continue
            f = ctx.fundamental(code)
            if detail and reasons["sample_fund"] is None and f:
                reasons["sample_fund"] = (code, dict(f))
            if not f or not f.get("dividend_yield") or f["dividend_yield"] < min_dy:
                if detail:
                    if not f:
                        reasons["fund_none"] += 1
                    elif not f.get("dividend_yield"):
                        reasons["dy_missing_key"] += 1
                    else:
                        reasons["no_fund_or_dy_low"] += 1
                continue
            if ctx.dividend_years(code, div_years) < div_years:
                if detail:
                    reasons["div_years_fail"] += 1
                continue
            ok, roe = F.roe_quality(code, date, years=roe_years, min_roe=roe_min, conn=ctx.conn)
            if detail and reasons["sample_roe"] is None:
                reasons["sample_roe"] = (code, ok, roe)
            if not ok:
                if detail:
                    reasons["roe_fail"] += 1
                continue
            if detail:
                reasons["pass"] += 1
            valid_codes.append(code)
        if detail:
            try:
                with open("reports/s1v3_diag.txt", "a", encoding="utf-8") as fp:
                    fp.write(f"\n--- detail call#{_DIAG['calls']} date={date} pool_n={len(pool)} ---\n")
                    for k, v in reasons.items():
                        fp.write(f"  {k}: {v}\n")
            except Exception:
                pass

        if len(valid_codes) < eff:
            _DIAG["gate_valid_codes_too_few"] += 1
            if len(_DIAG["valid_codes_samples"]) < 20:
                _DIAG["valid_codes_samples"].append((str(date), len(valid_codes), eff, len(pool)))
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
        if len(_DIAG["exposures_shape_samples"]) < 20:
            _DIAG["exposures_shape_samples"].append((str(date), all_exposures.shape,
                                                       list(all_exposures.columns)[:3]))
        if all_exposures.empty:
            log.warning("s1_v3: 因子暴露为空, date=%s", date)
            _DIAG["gate_exposures_empty"] += 1
            return []

        # 筛选到通过门槛的股票
        exposures = all_exposures.reindex(valid_codes)
        if exposures.dropna(how="all").empty:
            _DIAG["gate_dropna_empty"] += 1
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
        if len(_DIAG["smax_samples"]) < 20:
            _DIAG["smax_samples"].append((str(date), None if smax is None else float(smax), len(valid_codes)))
        if smax is not None and smax > 1e-9:
            score = score / smax
        else:
            _DIAG["gate_smax_zero"] += 1
            return []
        _DIAG["success"] += 1

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
            _DIAG["gate_target_empty"] = _DIAG.get("gate_target_empty", 0) + 1
            return []
        _DIAG["target_nonempty"] = _DIAG.get("target_nonempty", 0) + 1

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
