# -*- coding: utf-8 -*-
"""S8 价值质量清单(卡M.2,OPTIMIZE_V4.md;UZI-Skill investor_criteria.py 加权规则清单范式移植)。
月频,沪深300池。R1-R9 加权规则表打分,月末取分数前列等权持有,行业≤2(复用 factors.get_industry)。

数据只经 ctx.conn 批量 SQL 直查(整池向量化,禁止逐股循环查库);不依赖 factors.compute_
factor_exposures(该管线七列 NaN 故障,卡L.1 另批修复)——R3 历史PE中位数/R7 年化波动率/
R8 12-1月动量均在本文件内直接基于 daily_bar/fundamental/stock_annual 计算。
防未来函数:年报按 pub_date≤信号日过滤;PE历史中位数窗口/价格窗口均截至信号日。

规则表(rule_id/名称/权重):
  R1 ROE连续5年>15%(权5) / R2 ROE连续3年>10%(权3,与R1阶梯计分不叠加、取高者)
  R3 PE(TTM)低于自身可得历史(≥3年)中位数(权3)   R4 净利润连续5年>0(权3)
  R5 净利润5年复合增长>0(权2)                    R6 股息率>2%(权2)
  R7 年化波动率处池内后50%(权2,即波动最低的那一半)  R8 12-1月动量>0(权2)
  R9 距52周高点回撤<25% 且 高于52周低点>30%(权2)
评分=Σ通过权重/Σ总权重(R1/R2取高者,分母仍按9条权重之和=24计,故满分实际上限为21/24)。
reason 含逐条规则结果摘要,供看板卡P渲染。

本文件不注册 registry.yaml/config.yaml(留给验证批,按"验证完成才冻结"纪律)。"""
import logging
from datetime import datetime, timedelta
from statistics import median, pstdev

import pandas as pd

from models import Order
from strategies.base import BaseStrategy
from strategies import common
from strategies import news_guard
import factors   # 仅用 get_industry(自成一体,不经过故障的 compute_factor_exposures 管线)
import macro      # compute_market_regime: 宏观 regime 自适应降仓(S8@v2 低回撤核心防线)

log = logging.getLogger("s8")
POOL_INDEX = "sh000300"

RULE_WEIGHTS = {"R1": 5, "R2": 3, "R3": 3, "R4": 3, "R5": 2, "R6": 2, "R7": 2, "R8": 2, "R9": 2}
TOTAL_WEIGHT = sum(RULE_WEIGHTS.values())     # 24

WEEK52 = 252                    # 52周≈252个交易日(与 s9_stage2 一致)
VOL_WINDOW = 60                 # R7 年化波动率窗口(与 factors.py VOLATILITY 因子 60日 DASTD 惯例一致)
PE_HIST_MIN_YEARS = 3           # R3"自身可得历史(≥3年)"门槛
PE_HIST_LOOKBACK_YEARS = 10     # PE历史取数上限(避免全表扫描,10年足够覆盖≥3年门槛判定)


# ======================================================================
# 批量数据获取(整池一次 SQL,组内 pandas 处理,不逐股循环查库)
# ======================================================================
def _to_date_str(d):
    return str(d)[:10]


def _cal_start(date_str, days):
    """date_str 往前推 days 个自然日(不依赖 trade_calendar 表;daily_bar 本就只存交易日行,
    500自然日足以覆盖 WEEK52/VOL_WINDOW 所需的交易日窗口)。"""
    d = datetime.strptime(_to_date_str(date_str), "%Y-%m-%d") - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _minus_year(date_str, years):
    """同 fundamental.py 的 _minus_year(本文件自成一体,不跨模块引用私有函数)。"""
    y, m, d = map(int, _to_date_str(date_str).split("-"))
    try:
        return f"{y - years:04d}-{m:02d}-{d:02d}"
    except ValueError:
        return f"{y - years:04d}-{m:02d}-01"


def _price_stats(closes, vol_window):
    """单只标的的收盘价序列(升序,已后复权) → 年化波动率/12-1月动量/52周高低点。"""
    n = len(closes)
    stat = {"close": closes[-1] if n else None}
    if n >= vol_window + 1:
        recent = closes[-(vol_window + 1):]
        rets = [recent[i] / recent[i - 1] - 1 for i in range(1, len(recent)) if recent[i - 1]]
        stat["ann_vol"] = pstdev(rets) * (252 ** 0.5) if len(rets) > 1 else None
    else:
        stat["ann_vol"] = None
    if n >= 252 and closes[-252]:
        stat["mom_12_1"] = closes[-21] / closes[-252] - 1
    else:
        stat["mom_12_1"] = None
    if n >= WEEK52:
        w = closes[-WEEK52:]
        stat["high52"] = max(w)
        stat["low52"] = min(w)
    else:
        stat["high52"] = None
        stat["low52"] = None
    return stat


def _bulk_price_stats(conn, codes, date, vol_window=VOL_WINDOW):
    """批量取 codes 截至 date 的后复权收盘价,派生年化波动率(vol_window日)/12-1月动量/52周高低点。
    一次 SQL 覆盖全池(code IN (...)),组内用 pandas groupby 计算,非逐股查库。"""
    if not codes:
        return pd.DataFrame()
    date = _to_date_str(date)
    start = _cal_start(date, 500)
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, trade_date, close, adj_factor FROM daily_bar "
        f"WHERE code IN ({placeholders}) AND trade_date BETWEEN ? AND ? "
        f"ORDER BY code, trade_date", (*codes, start, date)).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["code", "trade_date", "close", "adj_factor"])
    df["adj_close"] = df["close"] * df["adj_factor"].fillna(1.0)
    out = {}
    for code, g in df.groupby("code"):
        closes = g.sort_values("trade_date")["adj_close"].tolist()
        out[code] = _price_stats(closes, vol_window)
    return pd.DataFrame.from_dict(out, orient="index")


def _bulk_fundamental(conn, codes, date):
    """批量最新 PE/股息率(截至date) + 自身≥PE_HIST_MIN_YEARS年历史PE中位数(R3,防未来:
    全部窗口截至date)。一次 SQL 取最新截面(自join取每code最大trade_date) + 一次 SQL 取历史PE序列。"""
    if not codes:
        return pd.DataFrame()
    date = _to_date_str(date)
    placeholders = ",".join("?" for _ in codes)
    latest_rows = conn.execute(
        f"SELECT f.code, f.pe, f.dividend_yield FROM fundamental f "
        f"INNER JOIN (SELECT code, MAX(trade_date) AS mx FROM fundamental "
        f"WHERE code IN ({placeholders}) AND trade_date<=? GROUP BY code) g "
        f"ON f.code=g.code AND f.trade_date=g.mx", (*codes, date)).fetchall()
    latest = {r[0]: {"pe": r[1], "dividend_yield": r[2]} for r in latest_rows}

    lo = _minus_year(date, PE_HIST_LOOKBACK_YEARS)
    hist_rows = conn.execute(
        f"SELECT code, trade_date, pe FROM fundamental WHERE code IN ({placeholders}) "
        f"AND trade_date BETWEEN ? AND ? AND pe IS NOT NULL", (*codes, lo, date)).fetchall()
    hist = {}
    for code, td, pe in hist_rows:
        hist.setdefault(code, []).append((td, pe))

    out = {}
    dt = datetime.strptime(date, "%Y-%m-%d")
    for code in codes:
        rec = latest.get(code, {})
        series = hist.get(code, [])
        pe_median_val = None
        if series:
            series.sort(key=lambda x: x[0])
            span_years = (dt - datetime.strptime(series[0][0][:10], "%Y-%m-%d")).days / 365.25
            if span_years >= PE_HIST_MIN_YEARS:
                pe_median_val = median([p for _, p in series])
        out[code] = {"pe": rec.get("pe"), "dividend_yield": rec.get("dividend_yield"),
                     "pe_median": pe_median_val}
    return pd.DataFrame.from_dict(out, orient="index")


def _bulk_annual(conn, codes, date):
    """批量取 codes 截至 date(pub_date<=date,防未来函数核心)的全部年报,一次 SQL。
    返回 dict{code: [(stat_year, roe, net_profit), ...]}(按 stat_year 降序,最新一期在前)。"""
    if not codes:
        return {}
    date = _to_date_str(date)
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, stat_year, roe, net_profit FROM stock_annual "
        f"WHERE code IN ({placeholders}) AND pub_date IS NOT NULL AND pub_date<>'' AND pub_date<=? "
        f"ORDER BY code, stat_year DESC", (*codes, date)).fetchall()
    out = {}
    for code, sy, roe, netp in rows:
        out.setdefault(code, []).append((sy, roe, netp))
    return out


# ======================================================================
# 规则判定(R1-R9)
# ======================================================================
def _consecutive_roe(yrs, n, threshold):
    """最新 n 期年报(已按 pub_date≤信号日过滤、降序)ROE 全部>threshold,且 stat_year 连续无缺年。"""
    if len(yrs) < n:
        return False
    top = yrs[:n]
    if not all(r[1] is not None and r[1] > threshold for r in top):
        return False
    years = [r[0] for r in top]
    return (max(years) - min(years)) == n - 1


def _consecutive_profit(yrs, n):
    """最新 n 期年报净利润全部>0,且 stat_year 连续无缺年。"""
    if len(yrs) < n:
        return False
    top = yrs[:n]
    if not all(r[2] is not None and r[2] > 0 for r in top):
        return False
    years = [r[0] for r in top]
    return (max(years) - min(years)) == n - 1


def _profit_cagr_positive(yrs, n):
    """净利润 n 年复合增长>0。等价简化(声明差异):两端皆正时 CAGR>0 ⇔ 末期>首期,不做分数次幂
    计算;由亏转盈(首期<=0、末期>0)视为正增长;末期<=0 视为不通过。年份不连续同样不通过。"""
    if len(yrs) < n:
        return False
    top = yrs[:n]
    profits = [r[2] for r in top]
    if any(p is None for p in profits):
        return False
    years = [r[0] for r in top]
    if (max(years) - min(years)) != n - 1:
        return False
    first, last = profits[-1], profits[0]      # profits[0]=最新一期, profits[-1]=n期中最早一期
    if last <= 0:
        return False
    if first <= 0:
        return True
    return last > first


def _r3_pass(f):
    """返回值显式 bool() 化:f 来自 pandas Series(fund.loc[code]),比较结果原生是 numpy.bool_,
    显式转换保证本模块"_xx_pass 谓词函数返回 Python bool"的稳定契约。"""
    if f is None:
        return False
    pe, med = f.get("pe"), f.get("pe_median")
    if pe is None or pd.isna(pe) or pe <= 0:
        return False
    if med is None or pd.isna(med):
        return False
    return bool(pe < med)


def _r6_pass(f):
    if f is None:
        return False
    dy = f.get("dividend_yield")
    if dy is None or pd.isna(dy):
        return False
    return bool(dy > 0.02)


def _r9_pass(b):
    close, high52, low52 = b.get("close"), b.get("high52"), b.get("low52")
    if close is None or high52 is None or low52 is None or pd.isna(close) or pd.isna(high52) \
            or pd.isna(low52) or high52 <= 0 or low52 <= 0:
        return False
    drawdown = 1 - close / high52
    above_low = close / low52 - 1
    return bool(drawdown < 0.25 and above_low > 0.30)


def _score_one(code, bars, fund, annual):
    """返回 (score, passes_dict) 或 None(无价格数据不可评分)。"""
    if code not in bars.index:
        return None
    b = bars.loc[code]
    f = fund.loc[code] if (fund is not None and code in fund.index) else None
    yrs = annual.get(code, [])

    passes = {
        "R1": _consecutive_roe(yrs, 5, 0.15),
        "R2": _consecutive_roe(yrs, 3, 0.10),
        "R3": _r3_pass(f),
        "R4": _consecutive_profit(yrs, 5),
        "R5": _profit_cagr_positive(yrs, 5),
        "R6": _r6_pass(f),
        "R7": bool(pd.notna(b.get("vol_pct_rank")) and b["vol_pct_rank"] <= 0.5),
        "R8": bool(pd.notna(b.get("mom_12_1")) and b["mom_12_1"] > 0),
        "R9": _r9_pass(b),
    }
    roe_score = RULE_WEIGHTS["R1"] if passes["R1"] else (RULE_WEIGHTS["R2"] if passes["R2"] else 0)
    score = roe_score + sum(RULE_WEIGHTS[k] for k in ("R3", "R4", "R5", "R6", "R7", "R8", "R9")
                             if passes[k])
    return score, passes


def score_pool(conn, codes, date, vol_window=VOL_WINDOW):
    """整池打分入口(供策略与测试共用,整池向量化)。返回 (scores dict, passes dict, bars DataFrame)。"""
    bars = _bulk_price_stats(conn, codes, date, vol_window)
    if bars.empty:
        return {}, {}, bars
    fund = _bulk_fundamental(conn, codes, date)
    annual = _bulk_annual(conn, codes, date)
    if "ann_vol" in bars.columns:
        bars["vol_pct_rank"] = bars["ann_vol"].rank(pct=True, na_option="bottom")
    scores, passes = {}, {}
    for code in codes:
        res = _score_one(code, bars, fund, annual)
        if res is None:
            continue
        scores[code], passes[code] = res
    return scores, passes, bars


# ======================================================================
# 策略
# ======================================================================
class S8ValueChecklist(BaseStrategy):
    """S8 价值质量清单(月频,沪深300池)。generate_orders 只做编排,规则判定/批量取数见上方
    模块级函数(可独立单测)。类结构/资金自适应/首建仓与置换逻辑仿照 s1_dividend.py/s6_sector.py。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []

        hold_n = self.params.get("hold_n", 10)
        keep_n = self.params.get("keep_n", 15)           # 卖出缓冲带:掉出前keep_n才卖,降低换手
        max_per_industry = self.params.get("max_per_industry", 2)
        vol_window = self.params.get("vol_window", VOL_WINDOW)
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)
        # 市场分/宏观敞口统一由 risk 层 _exposure_mult 处理(单一权威,避免双重缩放)

        pool = ctx.members(POOL_INDEX, date)
        if not pool:
            return []
        held = set(account.positions.keys())
        codes = list(dict.fromkeys(list(pool) + list(held)))   # 池∪持仓,防持仓掉出指数后无法评估卖出
        # —— 新闻/公告/动态守卫(全量接入) ——
        _ban_n, _ = news_guard.guard_candidates(date, codes, ctx.conn, self.config)
        _ind_of = factors.get_industry(ctx.conn, codes)
        _ban_i = news_guard.guard_industry(date, codes, ctx.conn, self.config, _ind_of)
        _ban_s = {c for c in codes if news_guard.structural_ban(date, c, ctx)[0]}
        _banned = _ban_n | _ban_i | _ban_s
        if _banned:
            codes = [c for c in codes if c not in _banned]
        tradable = set(common.main_board_universe(ctx, pool, self.config, date))  # 买入候选限主板宇宙(手册)
        if not tradable and not held:
            return []

        scores, passes, bars = score_pool(ctx.conn, codes, date, vol_window)
        scores = {c: s for c, s in scores.items() if c in tradable}   # 买入候选限可交易标的
        passes = {c: passes[c] for c in scores}

        industry_map = factors.get_industry(ctx.conn, codes)

        ranked = sorted(scores, key=lambda c: scores[c], reverse=True)
        full_rank = {c: i + 1 for i, c in enumerate(ranked)}

        industry_count, target = {}, []
        for code in ranked:
            ind = industry_map.get(code) or "未知"
            if industry_count.get(ind, 0) >= max_per_industry:
                continue
            target.append(code)
            industry_count[ind] = industry_count.get(ind, 0) + 1
            if len(target) >= eff:
                break

        orders = []
        forced = news_guard.guard_holdings(date, held, ctx.conn, self.config)
        for code in held:
            if code in target and code not in forced:
                continue
            rank = full_rank.get(code)
            close = bars.loc[code, "close"] if code in bars.index else None
            low52 = bars.loc[code, "low52"] if code in bars.index else None
            breach = bool(close is not None and low52 is not None
                          and pd.notna(close) and pd.notna(low52) and close < low52 * 1.3)
            if rank is None or rank > keep_n or breach or code in forced:
                nm = ctx.name(code)
                if code in forced:
                    reason = f"清单8:{nm}新闻黑天鹅,同步清仓"
                elif breach:
                    reason = f"清单8:{nm}跌破52周低点×1.3止损线,卖出"
                elif rank is None:
                    reason = f"清单8:{nm}数据不足或不再满足入池条件,卖出"
                else:
                    reason = f"清单8:{nm}综合评分排名第{rank}掉出前{keep_n},卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))

        for code in target:
            if code not in held:
                nm = ctx.name(code)
                digest = "".join(f"{k}{'✓' if passes[code][k] else '✗'}" for k in RULE_WEIGHTS)
                reason = (f"清单8:买入{nm}(评分{scores[code]:.1f}/{TOTAL_WEIGHT}·{digest}"
                          f"·综合第{full_rank[code]}/{len(ranked)})")
                orders.append(Order(self.strategy_id, code, "buy", w, reason, date))
        return orders


# ======================================================================
# S8@v2 —— 高质量低回撤动量(替代原价值质量清单 s8_checklist@v1)
# 选股逻辑与原 R1-R9 清单不同:质量(ROE连续)+低回撤(1年最大回撤最小)+低波动
# +中期动量(12-1月>0 且 价>MA60/MA200) 复合打分,取前列等权。
# 风控(原清单缺失、导致熊/震荡市裸多头挨打的核心):
#   ① 宏观 regime 自适应:compute_market_regime 分数低→按比例降仓甚至空仓;
#   ② 持仓跟踪止损:自持有期高点回撤超 stop_pct 或破 MA60→清仓,直接压制单票回撤。
# ======================================================================
def _price_stats_v2(closes, vol_window, dd_window):
    """在 _price_stats 基础上追加:trailing 最大回撤(max_dd)、MA60/MA200。"""
    n = len(closes)
    stat = {"close": closes[-1] if n else None}
    if n >= vol_window + 1:
        recent = closes[-(vol_window + 1):]
        rets = [recent[i] / recent[i - 1] - 1 for i in range(1, len(recent)) if recent[i - 1]]
        stat["ann_vol"] = pstdev(rets) * (252 ** 0.5) if len(rets) > 1 else None
    else:
        stat["ann_vol"] = None
    if n >= 252 and closes[-252]:
        stat["mom_12_1"] = closes[-21] / closes[-252] - 1
    else:
        stat["mom_12_1"] = None
    if n >= WEEK52:
        w = closes[-WEEK52:]
        stat["high52"] = max(w)
        stat["low52"] = min(w)
    else:
        stat["high52"] = None
        stat["low52"] = None
    win = closes[-dd_window:] if n >= dd_window else closes
    peak = win[0] if win else None
    mdd = 0.0
    for p in win:
        if peak is None or p > peak:
            peak = p
        if peak and peak > 0:
            dd = (peak - p) / peak
            if dd > mdd:
                mdd = dd
    stat["max_dd"] = mdd if win else None
    stat["ma60"] = float(sum(closes[-60:]) / 60) if n >= 60 else None
    stat["ma200"] = float(sum(closes[-200:]) / 200) if n >= 200 else None
    return stat


def _bulk_price_stats_v2(conn, codes, date, vol_window=VOL_WINDOW, dd_window=252):
    if not codes:
        return pd.DataFrame()
    date = _to_date_str(date)
    start = _cal_start(date, 500)
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, trade_date, close, adj_factor FROM daily_bar "
        f"WHERE code IN ({placeholders}) AND trade_date BETWEEN ? AND ? "
        f"ORDER BY code, trade_date", (*codes, start, date)).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["code", "trade_date", "close", "adj_factor"])
    df["adj_close"] = df["close"] * df["adj_factor"].fillna(1.0)
    out = {}
    for code, g in df.groupby("code"):
        closes = g.sort_values("trade_date")["adj_close"].tolist()
        out[code] = _price_stats_v2(closes, vol_window, dd_window)
    return pd.DataFrame.from_dict(out, orient="index")


def score_pool_v2(conn, codes, date, params):
    """S8@v2 打分:质量+低回撤+低波动+动量+趋势 复合。返回 (scores, bars)。"""
    bars = _bulk_price_stats_v2(conn, codes, date, params.get("vol_window", VOL_WINDOW),
                                params.get("dd_window", 252))
    if bars.empty:
        return {}, bars
    fund = _bulk_fundamental(conn, codes, date)
    annual = _bulk_annual(conn, codes, date)
    if "ann_vol" in bars.columns:
        bars["vol_pct_rank"] = bars["ann_vol"].rank(pct=True, na_option="bottom")
        bars["dd_pct_rank"] = bars["max_dd"].rank(pct=True, na_option="bottom")
    mom_min = params.get("mom_min", 0.05)
    roe_years = params.get("roe_years", 3)
    roe_min = params.get("roe_min", 0.10)
    pe_cap = params.get("pe_cap", 40)
    w = params.get("weights", {})
    w_q, w_dd, w_vol, w_mom, w_tr = (w.get("quality", 0.20), w.get("lowdd", 0.30),
                                     w.get("lowvol", 0.15), w.get("momentum", 0.25), w.get("trend", 0.10))
    scores = {}
    for code in codes:
        if code not in bars.index:
            continue
        b = bars.loc[code]
        # 注意:fund 是 DataFrame(index=code),必须用 .loc[code] 取行;误用 .get(code) 会取到"列名为 code 的列"→ None
        f = fund.loc[code] if code in fund.index else None
        yrs = annual.get(code, [])
        if not _consecutive_roe(yrs, roe_years, roe_min):
            continue
        if not _consecutive_profit(yrs, roe_years):
            continue
        pe = f.get("pe") if f is not None else None
        if pe is None or pd.isna(pe) or pe <= 0 or pe > pe_cap:
            continue
        mom = b.get("mom_12_1")
        if mom is None or pd.isna(mom) or mom < mom_min:
            continue
        close = b.get("close")
        ma60 = b.get("ma60")
        if close is None or ma60 is None or pd.isna(close) or pd.isna(ma60) or close < ma60:
            continue
        dd_s = 1.0 - (b["dd_pct_rank"] if pd.notna(b.get("dd_pct_rank")) else 0.5)
        vol_s = 1.0 - (b["vol_pct_rank"] if pd.notna(b.get("vol_pct_rank")) else 0.5)
        mom_s = max(0.0, min(1.0, (mom - mom_min) / 0.25))
        ma200 = b.get("ma200")
        trend_s = (0.5 if (ma200 is not None and pd.notna(ma200) and close >= ma200) else 0.0) + 0.5
        roe0 = yrs[0][1] if yrs and yrs[0][1] is not None else roe_min
        q_s = max(0.0, min(1.0, (roe0 - roe_min) / 0.15))
        scores[code] = w_q * q_s + w_dd * dd_s + w_vol * vol_s + w_mom * mom_s + w_tr * trend_s
    return scores, bars


class S8LowDrawdown(BaseStrategy):
    """S8@v2 高质量低回撤动量。generate_orders 编排:宏观regime降仓 + 复合打分选股
    + 持有期跟踪止损(自高点回撤/破MA60)。详见模块顶部说明。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []

        p = self.params
        hold_n = p.get("hold_n", 10)
        keep_n = p.get("keep_n", 12)
        max_per_industry = p.get("max_per_industry", 2)
        min_avg_amount = p.get("min_avg_amount", 30000000)
        stop_pct = p.get("stop_pct", 0.10)

        eff0 = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff0)

        # —— 宏观 regime 自适应降仓 ——
        eff_ratio = 1.0
        try:
            reg = macro.compute_market_regime(date, conn=ctx.conn)
            rscore = reg.get("score", 50)
            eff_ratio = 1.0 if rscore >= 60 else (0.6 if rscore >= 40 else 0.25)
        except Exception as e:
            log.warning("regime 失败:%s", e)
            eff_ratio = 0.6
        eff = max(1, int(round(eff0 * eff_ratio)))
        if eff_ratio < 1.0:
            w = common.target_weight(eff)

        pool = ctx.members(POOL_INDEX, date)
        if not pool:
            return []
        held = set(account.positions.keys())
        codes = list(dict.fromkeys(list(pool) + list(held)))

        # —— 新闻/公告/动态守卫 ——
        _ban_n, _ = news_guard.guard_candidates(date, codes, ctx.conn, self.config)
        _ind_of = factors.get_industry(ctx.conn, codes)
        _ban_i = news_guard.guard_industry(date, codes, ctx.conn, self.config, _ind_of)
        _ban_s = {c for c in codes if news_guard.structural_ban(date, c, ctx)[0]}
        _banned = _ban_n | _ban_i | _ban_s
        if _banned:
            codes = [c for c in codes if c not in _banned]

        tradable = set(common.main_board_universe(ctx, pool, self.config, date))
        if not tradable and not held:
            return []

        scores, bars = score_pool_v2(ctx.conn, codes, date, p)
        scores = {c: s for c, s in scores.items() if c in tradable}

        industry_map = factors.get_industry(ctx.conn, codes)
        ranked = sorted(scores, key=lambda c: scores[c], reverse=True)
        full_rank = {c: i + 1 for i, c in enumerate(ranked)}

        industry_count, target = {}, []
        for code in ranked:
            ind = industry_map.get(code) or "未知"
            if industry_count.get(ind, 0) >= max_per_industry:
                continue
            target.append(code)
            industry_count[ind] = industry_count.get(ind, 0) + 1
            if len(target) >= eff:
                break

        orders = []
        forced = news_guard.guard_holdings(date, held, ctx.conn, self.config)
        for code in held:
            if code in target and code not in forced:
                continue
            b = bars.loc[code] if code in bars.index else None
            close = b.get("close") if b is not None else None
            pos = account.positions[code]
            peak = pos.highest_close if pos.highest_close else pos.avg_cost
            breach = bool(close is not None and peak and peak > 0 and close < peak * (1 - stop_pct))
            ma60 = b.get("ma60") if b is not None else None
            ma_break = bool(close is not None and ma60 is not None and pd.notna(ma60) and close < ma60)
            low52 = b.get("low52") if b is not None else None
            breach52 = bool(close is not None and low52 is not None and pd.notna(close)
                            and pd.notna(low52) and close < low52 * 1.3)
            rank = full_rank.get(code)
            if rank is None or rank > keep_n or breach or ma_break or breach52 or code in forced:
                nm = ctx.name(code)
                if code in forced:
                    reason = f"低回撤8:{nm}新闻黑天鹅,清仓"
                elif breach:
                    reason = f"低回撤8:{nm}自高点回撤>{(stop_pct*100):.0f}%止损"
                elif ma_break:
                    reason = f"低回撤8:{nm}跌破MA60止损"
                elif breach52:
                    reason = f"低回撤8:{nm}跌破52周低点×1.3止损"
                elif rank is None:
                    reason = f"低回撤8:{nm}不再满足入池,卖出"
                else:
                    reason = f"低回撤8:{nm}评分第{rank}掉出前{keep_n},卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))

        for code in target:
            if code not in held:
                nm = ctx.name(code)
                b = bars.loc[code]
                reason = (f"低回撤8:买入{nm}(评分{scores[code]:.2f}·动量{(b.get('mom_12_1') or 0)*100:.0f}%·"
                          f"最大回撤{(b.get('max_dd') or 0)*100:.0f}%·波动{b.get('ann_vol') or 0:.1f})")
                orders.append(Order(self.strategy_id, code, "buy", w, reason, date))
        return orders
