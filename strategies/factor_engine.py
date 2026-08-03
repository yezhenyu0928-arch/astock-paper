# -*- coding: utf-8 -*-
"""重写的因子处理引擎(race_v2 专用)。

区别于旧 mf_core(直接全市场排名、无去极值/中性化、valuation=value 重复、动量不跳月):
  - 标准因子管线: 原始因子 → MAD去极值 → 缺失置NaN → 行业中性化 → 市值中性化(可选) → 秩/百分位标准化
  - 因子定义基于 factor_diagnostics 实证结论(A股):
      · 1月反转 最强(ICIR-0.39)  · 低波/低流动 有效(ICIR 0.39/0.59)
      · PB价值(bm) 有独立Alpha(IC 0.046, 与规模秩相关0.00)  · 规模 弱(近年小盘降温)
      · 动量/SUE/high52 全灭 → 弱化或弃用
  - 市值缺失 → NaN(排末尾), 不再赋0拿最优小市值排名
  - momentum 严格 12-1(跳过最近1月)
所有取数经 ctx(<=signal_date) 防未来函数。复用 factors.py 的 winsorize_mad/standardize 与批量取数。
"""
import logging
import math
import numpy as np
import pandas as pd

import util
import factors as _fac
from db import get_conn

log = logging.getLogger("factor_engine")


# ======================================================================
# 1. 标准化因子处理管线
# ======================================================================
def neutralize_industry(s: pd.Series, ind_map: dict, how: str = "demean") -> pd.Series:
    """行业中性化: 对每个行业内标准化(去行业均值)或去行业均值+除以行业标准差。
    how='demean' 去均值; how='zscore' 行业内 z-score(处理行业规模差异)。
    无行业映射的票(缺失)保留原值。返回同索引 Series。"""
    s = s.copy()
    groups = {}
    for code in s.index:
        ind = (ind_map or {}).get(code)
        if ind:
            groups.setdefault(ind, []).append(code)
    for ind, codes in groups.items():
        if len(codes) < 2:
            continue
        vals = s[codes].astype(float)
        m = vals.mean()
        if how == "zscore":
            sd = vals.std(ddof=0)
            if sd > 1e-12:
                s[codes] = (vals - m) / sd
            else:
                s[codes] = vals - m
        else:
            s[codes] = vals - m
    return s


def neutralize_cap(s: pd.Series, log_cap: pd.Series, drop: bool = False) -> pd.Series:
    """市值中性化: 对 log(市值) 回归取残差。
    当 s 与市值高度相关(如规模/流动因子)时用, 提取独立于规模的 Alpha。
    drop=True 时缺失市值票也剔除; 否则缺失市值保留原值。"""
    valid = s.notna() & log_cap.notna()
    if valid.sum() < 10:
        return s.copy()
    x = log_cap[valid].values
    y = s[valid].values
    # 一元线性回归 y = a + b*x, 残差
    xm, ym = x.mean(), y.mean()
    b = np.dot(x - xm, y - ym) / (np.dot(x - xm, x - xm) + 1e-12)
    a = ym - b * xm
    resid = y - (a + b * x)
    out = pd.Series(np.nan, index=s.index)
    out[valid] = resid
    if not drop:
        out[s.isna()] = np.nan
        out[log_cap.isna() & s.notna()] = s[log_cap.isna() & s.notna()]
    return out


def percentile_rank(s: pd.Series) -> pd.Series:
    """百分位/秩标准化: 转为 0~1 均匀分布。缺失保持 NaN。"""
    s = s.copy()
    valid = s.notna()
    if valid.sum() < 2:
        return s
    r = s[valid].rank(method="average")
    s[valid] = (r - 1) / (r.max() - 1) if r.max() > 1 else r
    return s


def process(raw: pd.Series, ind_map: dict = None, log_cap: pd.Series = None,
            ind_neutral: bool = True, cap_neutral: bool = False) -> pd.Series:
    """完整因子管线: 去极值 → 行业中性化(可选) → 市值中性化(可选) → 百分位标准化。
    cap_neutral 与 ind_neutral 顺序: 先行业后市值(市值中性化通常最后做, 或在行业内做)。
    返回 0~1 标准化 Series(缺失 NaN, 排末尾)。"""
    s = _fac.winsorize_mad(raw.copy())
    if ind_neutral and ind_map:
        s = neutralize_industry(s, ind_map, how="demean")
    if cap_neutral and log_cap is not None:
        s = neutralize_cap(s, log_cap, drop=False)
    return percentile_rank(s)


# ======================================================================
# 2. 原始因子计算(全池截面, 批量取数)
# ======================================================================
# 持久化 bar 缓存(回测跨月复用): conn id -> {code: [(date, adj_close, amount), ...]}
_BAR_CACHE = {}


def _cache_bars(conn, codes):
    """一次性载入 codes 全历史 bars 到缓存(回测提速关键: 避免每月重查全池)。"""
    cid = id(conn)
    cache = _BAR_CACHE.get(cid)
    if cache is None:
        cache = {}
        _BAR_CACHE[cid] = cache
    miss = [c for c in codes if c not in cache]
    if not miss:
        return
    ph = ",".join("?" for _ in miss)
    rows = conn.execute(
        f"SELECT code, trade_date, close, adj_factor, amount FROM daily_bar "
        f"WHERE code IN ({ph}) ORDER BY code, trade_date", miss).fetchall()
    for code, dt, close, adj, amount in rows:
        cache.setdefault(code, []).append(
            (dt, (close or 0) * (adj or 1.0), amount))


def _pool_fundamental_batch(conn, codes, date):
    """批量取 codes 截至 date 最近一条基本面(单条SQL, 提速回测)。"""
    date = util.to_date_str(date)
    if not codes:
        return pd.DataFrame(index=[], columns=["pe", "pb", "market_cap", "dividend_yield"])
    # 优化: 先取所有 code<=date 的基本面, 再取每条 code 最后一条
    ph = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, trade_date, pe, pb, market_cap, dividend_yield FROM fundamental "
        f"WHERE code IN ({ph}) AND trade_date<=? ORDER BY code, trade_date",
        (*codes, date)).fetchall()
    out = {}
    for code, dt, pe, pb, mc, dy in rows:
        out[code] = (pe, pb, mc, dy)   # 最后一条覆盖(ORDER BY date 升序)
    vals = [out.get(c, (None, None, None, None)) for c in codes]
    df = pd.DataFrame(vals, index=codes, columns=["pe", "pb", "market_cap", "dividend_yield"])
    df.index.name = "code"
    return df


def _pool_annual_batch(conn, codes, date):
    """批量取 codes 截至 date 最近一期年报(单条SQL, 提速回测)。"""
    date = util.to_date_str(date)
    if not codes:
        return pd.DataFrame(index=[], columns=["roe", "net_profit"])
    ph = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, stat_year, roe, net_profit FROM stock_annual "
        f"WHERE code IN ({ph}) AND pub_date IS NOT NULL AND pub_date<>'' AND pub_date<=? "
        f"ORDER BY code, stat_year",
        (*codes, date)).fetchall()
    out = {}
    for code, sy, roe, np_ in rows:
        out[code] = (roe, np_)   # 最后一条=最新年报(ORDER BY stat_year 升序)
    vals = []
    for c in codes:
        v = out.get(c)
        vals.append(v if v is not None else (None, None))
    df = pd.DataFrame(vals, index=codes, columns=["roe", "net_profit"])
    df.index.name = "code"
    return df


def _pool_ctx(conn, codes, date, lookback=300):
    """批量取 codes 截至 date 的日线 + 基本面 + 年报截面。
    日线走持久化缓存(跨月复用), 基本面/年报走批量SQL。"""
    _cache_bars(conn, codes)
    cache = _BAR_CACHE[id(conn)]
    cal = conn.execute(
        "SELECT cal_date FROM trade_calendar WHERE is_open=1 AND cal_date<=? "
        "ORDER BY cal_date DESC LIMIT ?", (str(date)[:10], lookback * 2)).fetchall()
    if not cal:
        return None, None, None, None
    start_date = cal[-1][0]
    out_dates = [r[0] for r in cal[::-1]]
    close_grid, amt_grid = {}, {}
    for code in codes:
        rows = cache.get(code, [])
        d_close = {dt: c for dt, c, _ in rows if start_date <= dt <= str(date)[:10]}
        d_amt = {dt: a for dt, _, a in rows if start_date <= dt <= str(date)[:10]}
        if d_close:
            close_grid[code] = [d_close.get(dt) for dt in out_dates]
            amt_grid[code] = [d_amt.get(dt) for dt in out_dates]
    if not close_grid:
        return None, None, None, None
    close_pivot = pd.DataFrame(close_grid, index=out_dates)
    amount_pivot = pd.DataFrame(amt_grid, index=out_dates)
    fund_df = _pool_fundamental_batch(conn, codes, date)
    annual_df = _pool_annual_batch(conn, codes, date)
    return close_pivot, amount_pivot, fund_df, annual_df


def clear_bar_cache():
    _BAR_CACHE.clear()


def _daily_returns(close_pivot: pd.DataFrame) -> pd.DataFrame:
    """日收益率矩阵(逐列)。"""
    return close_pivot.pct_change(fill_method=None)


def factor_returns(close_pivot: pd.DataFrame, n: int = 21) -> pd.Series:
    """过去 n 个交易日累计收益(截面, 用于反转/动量)。
    n=21 用于1月反转; 注意此式 = close[-1]/close[-(n+1)]-1(含最近1日)。
    """
    if close_pivot is None or len(close_pivot) < n + 1:
        return pd.Series(index=close_pivot.columns if close_pivot is not None else [])
    return close_pivot.iloc[-1] / close_pivot.iloc[-(n + 1)] - 1


def factor_mom12_1(close_pivot: pd.DataFrame, skip: int = 21) -> pd.Series:
    """12-1月动量(跳过最近1月): close[-22]/close[-253]-1。
    《因子投资》3.5 标准定义, 消除短期反转噪声。"""
    if close_pivot is None or len(close_pivot) < 253:
        return pd.Series(index=close_pivot.columns if close_pivot is not None else [])
    return close_pivot.iloc[-(skip + 1)] / close_pivot.iloc[-253] - 1


def factor_vol(close_pivot: pd.DataFrame, n: int = 60) -> pd.Series:
    """n 日日收益年化波动率(截面)。"""
    if close_pivot is None or len(close_pivot) < 10:
        return pd.Series(index=close_pivot.columns if close_pivot is not None else [])
    ret = _daily_returns(close_pivot).iloc[-n:]
    return ret.std(ddof=1) * math.sqrt(252)


def factor_amount(amount_pivot: pd.DataFrame, n: int = 20) -> pd.Series:
    """n 日日均成交额(截面)。"""
    if amount_pivot is None or len(amount_pivot) < 5:
        return pd.Series(index=amount_pivot.columns if amount_pivot is not None else [])
    return amount_pivot.iloc[-n:].mean()


def factor_pe(fund_df: pd.DataFrame) -> pd.Series:
    """PE(原始方向: 越低越好)。缺失 NaN。"""
    pe = fund_df.get("pe", pd.Series(index=fund_df.index, dtype=float))
    return pe.where(pe > 0)


def factor_pb(fund_df: pd.DataFrame) -> pd.Series:
    """PB(原始方向: 越低越好)。缺失 NaN。"""
    pb = fund_df.get("pb", pd.Series(index=fund_df.index, dtype=float))
    return pb.where(pb > 0)


def factor_ep(fund_df: pd.DataFrame, annual_df: pd.DataFrame) -> pd.Series:
    """EP = 近一年净利 / 市值(方向: 越高越好)。缺失 NaN。"""
    mc = fund_df.get("market_cap")
    np_ = annual_df.get("net_profit")
    if mc is None or np_ is None:
        return pd.Series(index=fund_df.index, dtype=float)
    ep = (np_ / mc).where((np_ > 0) & (mc > 0))
    return ep


def factor_roe(annual_df: pd.DataFrame) -> pd.Series:
    """ROE(年报, 方向: 越高越好)。缺失 NaN。"""
    roe = annual_df.get("roe", pd.Series(index=annual_df.index, dtype=float))
    return roe


def factor_logcap(fund_df: pd.DataFrame) -> pd.Series:
    """log(总市值)。缺失 NaN(不再赋0)。"""
    mc = fund_df.get("market_cap")
    if mc is None:
        return pd.Series(index=fund_df.index, dtype=float)
    logcap = mc.map(lambda x: math.log(x) if x and x > 0 else np.nan)
    return logcap


def factor_growth(conn, codes, date, mode="yoy"):
    """盈利增长因子: profit_q 单季净利同比增速(方向: 越高越好, 缺失NaN)。
    mode='yoy':  最新单季净利 / 上年同期 - 1(同比增速)
    mode='accel': 同比增速的加速度 = YoY_t - YoY_{t-1}(增速在加快=成长Alpha)
    严格按 pub_date<=date 过滤(防未来函数)。全市场覆盖(profit_q 有11385只)。
    """
    date = util.to_date_str(date)
    if not codes:
        return pd.Series(index=[], dtype=float)
    ph = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, stat_date, net_profit FROM profit_q "
        f"WHERE code IN ({ph}) AND pub_date IS NOT NULL AND pub_date<>'' AND pub_date<=? "
        f"ORDER BY code, stat_date", (*codes, date)).fetchall()
    by = {}
    for code, sd, np0 in rows:
        by.setdefault(code, []).append((sd, np0))
    out = {}
    for code, seq in by.items():
        seq = [(sd, np) for sd, np in seq if np is not None and np != 0]
        if len(seq) < 2:
            out[code] = None
            continue
        # 按季度同比: 取最新一季 vs 1年前同季
        last_sd = seq[-1][0]
        year_ago = f"{int(last_sd[:4])-1}{last_sd[4:]}"
        prev = None
        for sd, np0 in seq:
            if sd <= year_ago:
                prev = np0
        cur = seq[-1][1]
        if prev is None or prev == 0:
            out[code] = None
            continue
        yoy = cur / prev - 1
        if mode == "accel" and len(seq) >= 3:
            # 再往前一季的同比
            prev2 = None
            prev_sd = seq[-2][0]
            year_ago2 = f"{int(prev_sd[:4])-1}{prev_sd[4:]}"
            for sd, np0 in seq:
                if sd <= year_ago2:
                    prev2 = np0
            if prev2 is not None and prev2 != 0:
                yoy_prev = seq[-2][1] / prev2 - 1
                out[code] = yoy - yoy_prev
            else:
                out[code] = yoy
        else:
            out[code] = yoy
    return pd.Series(out, index=codes, dtype=float)


# ======================================================================
# 3. 复合因子组合
# ======================================================================
def composite_scores(codes, date, conn, weights, ind_neutral=True, cap_neutral_after=None):
    """按 weights 计算复合选股分(0~1, 越高越好), 行业/市值中性化。

    weights: {factor_name: weight}, 因子方向统一为"越大越好"由调用方用负权表达。
    可用因子: 'rev1m'(反转,负=1月涨幅取负), 'size'(规模, 负=小市值),
              'vol'(低波, 负=波动), 'amount'(低流动, 负=成交额),
              'bm'(PB价值, 负=高PB低分), 'ep'(盈利市值比), 'roe'(质量),
              'mom12_1'(动量, 负=反转), 'pe'(低PE)
    cap_neutral_after: 最后整体对市值中性化(用于去掉规模风格, 如价值/质量策略)。
    """
    close_pivot, amount_pivot, fund_df, annual_df = _pool_ctx(conn, codes, date)
    if close_pivot is None or len(close_pivot.columns) == 0:
        return pd.Series(index=codes)
    logcap = factor_logcap(fund_df)
    ind_map = _fac.get_industry(conn, list(codes))

    # 计算各原始因子
    raw = {}
    if "rev1m" in weights:
        raw["rev1m"] = factor_returns(close_pivot, 21)
    if "mom12_1" in weights:
        raw["mom12_1"] = factor_mom12_1(close_pivot, 21)   # 12-1(跳过最近1月)
    if "mom6" in weights:
        raw["mom6"] = factor_returns(close_pivot, 126)
    if "vol" in weights:
        raw["vol"] = factor_vol(close_pivot, 60)
    if "amount" in weights:
        raw["amount"] = factor_amount(amount_pivot, 20)
    if "bm" in weights:
        raw["bm"] = factor_pb(fund_df)   # 原始=PB(低好), 负权表达低PB
    if "pe" in weights:
        raw["pe"] = factor_pe(fund_df)
    if "ep" in weights:
        raw["ep"] = factor_ep(fund_df, annual_df)
    if "roe" in weights:
        raw["roe"] = factor_roe(annual_df)
    if "growth" in weights:
        raw["growth"] = factor_growth(conn, list(codes), date, mode="accel")
    if "size" in weights:
        raw["size"] = logcap

    # 各因子标准化处理(方向统一为"越大越好")
    z = pd.DataFrame(index=codes)
    for name, w in weights.items():
        if name not in raw:
            continue
        s = raw[name]
        # 方向修正: 负权=原始值越低越好 → 取负后排名(负值越大=原始越小)
        if w < 0:
            s = -s
            z[name] = process(s, ind_map, logcap, ind_neutral=ind_neutral)
        else:
            z[name] = process(s, ind_map, logcap, ind_neutral=ind_neutral)

    # 合成: 各因子百分位×权重归一 → 0~1
    avail = [c for c in z.columns if z[c].notna().any()]
    if not avail:
        return pd.Series(np.nan, index=codes)
    total_w = sum(abs(weights[c]) for c in avail)
    if total_w < 1e-12:
        return pd.Series(0.5, index=codes)
    score = pd.Series(0.0, index=codes)
    for c in avail:
        w = abs(weights[c]) / total_w * len(avail)   # 归一
        score += w * z[c].fillna(z[c].median())
    score = percentile_rank(score)

    # 可选: 最终对市值中性化(提取纯Alpha, 去掉规模暴露)
    if cap_neutral_after and logcap is not None:
        score = neutralize_cap(score, logcap, drop=False)
        score = percentile_rank(score)
    return score


# ======================================================================
# 4. 工具
# ======================================================================
def adaptive_stop_pct(close_pivot: pd.DataFrame, code: str, k: float = 2.0,
                      min_stop: float = 0.10, max_stop: float = 0.35) -> float:
    """个股波动率自适应止损: stop = clip(vol_60 * k, min_stop, max_stop)。
    低波动票止损紧(防回撤), 高波动票止损宽(防噪声扫损)。"""
    try:
        vol = factor_vol(close_pivot, 60).get(code)
        if vol and vol > 0:
            return min(max_stop, max(min_stop, vol * k))
    except Exception:
        pass
    return min_stop


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    conn = get_conn()
    from datetime import date as _d
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM index_members WHERE index_code='mainboard' AND in_date<=? LIMIT 20",
        (str(_d.today()),)).fetchall()]
    sc = composite_scores(codes, str(_d.today()), conn,
                          {"rev1m": -0.4, "bm": -0.3, "vol": -0.3})
    print(sc.sort_values(ascending=False).head(10))
    conn.close()
