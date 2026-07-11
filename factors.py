# -*- coding: utf-8 -*-
"""因子库（卡I）—— 全项目唯一因子计算模块。

实现 10 个风格因子 + 4 个风险因子的计算、处理流水线（去极值→标准化→正交化）
以及风险模型所需暴露矩阵接口。遵循 MSCI Barra CNE6 / Axioma V4 方法论，
按本项目免费数据现实裁剪，差异在代码注释中声明。

Conventions（与 OPTIMIZE_V3.md 第三节一致）:
- MAD winsorize: median ± 5 * 1.4826 * MAD
- 标准化: 市值加权均值, 等权标准差
- 正交化: Gram-Schmidt 顺序 BETA→SIZE→VALUE→...→EARNINGS_YIELD
- 所有 price_t 为后复权价 = close * adj_factor
- 市场代理: sh510300 ETF 后复权收益
"""
import logging
import functools
import numpy as np
import pandas as pd

import util
from db import get_conn

log = logging.getLogger("factors")

# ── 常数 ──────────────────────────────────────────────────────────────
MARKET_PROXY = "sh510300"                    # 市场收益代理 ETF
SW_LEVEL1_INDUSTRIES = [                     # 申万一级行业(31 个)
    "银行", "非银金融", "房地产", "建筑装饰", "建筑材料",
    "交通运输", "公用事业", "环保", "钢铁", "有色金属",
    "基础化工", "石油石化", "煤炭", "农林牧渔", "食品饮料",
    "医药生物", "家用电器", "纺织服装", "轻工制造", "商贸零售",
    "社会服务", "传媒", "通信", "计算机", "电子",
    "机械设备", "电力设备", "国防军工", "汽车", "美容护理", "综合",
]
RISK_FACTORS = ["size", "beta", "momentum", "resvol", "liquidity", "btop"]
"""风险模型暴露矩阵使用的六个风格风险因子（仿 CNE6 公开清单）。"""
ALPHA_DESCRIPTORS = ["dtop", "etop", "roe", "egro", "vern", "strev"]
"""Alpha 描述符列表（已标准化），供策略层调用，方向=原始方向。"""

N_MAD = 5                                    # MAD 去极值倍数
STYLE_FACTOR_NAMES = [
    "BETA", "SIZE", "VALUE", "MOMENTUM", "VOLATILITY",
    "QUALITY", "GROWTH", "LIQUIDITY", "LEVERAGE", "EARNINGS_YIELD",
]
ORTHO_ORDER = STYLE_FACTOR_NAMES             # Gram-Schmidt 顺序


# ======================================================================
# 1. 预处理函数
# ======================================================================
def winsorize_mad(s: pd.Series, n: float = N_MAD) -> pd.Series:
    """MAD 去极值。上下界 = median ± n × 1.4826 × MAD。
    MAD = median(|x - median(x)|)。越界值截断到边界。
    Axioma / Barra 标准做法。"""
    s = s.copy().astype(float)                     # 确保 float 避免 int64 clip 错误
    med = s.median()
    mad = (s - med).abs().median()
    if mad < 1e-12:                                # 全相等序列
        return s
    scale = n * 1.4826 * mad
    lo, hi = med - scale, med + scale
    s.clip(lower=lo, upper=hi, inplace=True)
    return s


def standardize(s: pd.Series, cap_weight: pd.Series = None) -> pd.Series:
    """Barra 式截面标准化：z = (x - μ_w) / σ_eq。
    其中 μ_w = 市值加权均值, σ_eq = 等权标准差。
    若 cap_weight 未提供则退化为普通 z-score。
    健壮性修复: cap_weight 常来自 fund_df(索引=请求的全池代码), 而部分调用方(如
    compute_momentum/compute_volatility/compute_liquidity)的 s 索引来自 ret_df.columns
    (仅当日实际有行情数据的子集,池内新股/停牌股会被自然剔除)。两者索引不一致时
    `cap_weight[valid]` 直接布尔索引会抛 "Unalignable boolean Series provided as
    indexer",被上层 try/except 吞掉后整因子退化为全 NaN(级联拖垮 orthogonalize 之后的
    因子)。先按 s.index reindex 对齐,缺失的权重不应让整条均值计算失败,回退为其余
    有效权重的中位数(仍是"按市值加权"的近似,而非静默退化成等权)。"""
    s = s.copy()
    valid = s.notna()
    if valid.sum() < 3:
        return s * np.nan
    cw = None
    if cap_weight is not None:
        cw = cap_weight.reindex(s.index)
        if cw.isna().any():
            cw = cw.fillna(cw.median()) if cw.notna().any() else None
    mu = np.average(s[valid], weights=cw[valid] if cw is not None else None)
    sigma = s[valid].std(ddof=0)             # 等权标准差（总体）
    if sigma < 1e-12:
        return s * 0.0
    s[valid] = (s[valid] - mu) / sigma
    s[~valid] = 0.0                           # 缺失填 0（池中性）
    return s


def orthogonalize(y: pd.Series, X: pd.DataFrame, w: pd.Series = None) -> pd.Series:
    """Gram-Schmidt 正交化: y 对 X 各列依次回归取残差。
    w = √市值（WLS 权重）。返回残差后再标准化。
    健壮性(卡L.1修复): 先剔除 X 中全 NaN / 有效样本<5 的自变量列。否则任一全 NaN 自变量
    (如短历史下 RAW 即失效的 MOMENTUM)会令 X.notna().all(axis=1) 全 False → valid 全空 →
    当前因子被拖成 NaN,并沿 Gram-Schmidt 顺序级联拖垮其后所有因子(exposures.json 早期采样
    7 列全 NaN 的根因)。剔除坏列后正交化只依赖有效基,长历史下无全 NaN 列故行为完全不变。"""
    if X is not None and X.shape[1] > 0:
        X = X[[c for c in X.columns if X[c].notna().sum() >= 5]]
    if X is None or X.shape[1] == 0:
        return y.copy()                          # 无有效自变量可正交化,保留原值(外层再标准化)
    valid = y.notna() & X.notna().all(axis=1)
    if valid.sum() < 5:
        return y * np.nan
    y_v = y[valid].values
    X_v = X[valid].values
    w_v = w[valid].values if w is not None else np.ones(valid.sum())
    w_sqrt = np.sqrt(w_v)
    # WLS: 对 y 和 X 各列乘以 √w
    y_w = y_v * w_sqrt
    X_w = X_v * w_sqrt.reshape(-1, 1)
    try:
        coef, *_ = np.linalg.lstsq(X_w, y_w, rcond=None)
    except np.linalg.LinAlgError:
        return y * np.nan
    resid = y_v - X_v @ coef
    res = pd.Series(np.nan, index=y.index)
    res.loc[valid] = resid
    return res


def composite(z_df: pd.DataFrame, weights: dict) -> pd.Series:
    """加权复合描述符。缺失某描述符时按可得描述符权重重归一，结果再标准化。
    weights: {col_name: weight}，方向"越大越好"由调用方通过负权重表达。"""
    cols = list(weights.keys())
    avail = [c for c in cols if c in z_df.columns and z_df[c].notna().any()]
    if not avail:
        return pd.Series(np.nan, index=z_df.index)
    total_w = sum(abs(weights[c]) for c in avail)
    if total_w < 1e-12:
        return pd.Series(0.0, index=z_df.index)
    score = pd.Series(0.0, index=z_df.index)
    for c in avail:
        w = weights[c] / total_w * len(avail)   # 归一后权重点数
        score += w * z_df[c].fillna(z_df[c].median())
    return standardize(score)                    # 结果再标准化


def winsorize(factor: pd.Series, method: str = "mad", n_mad: int = 5) -> pd.Series:
    """MAD 去极值（兼容接口）。"""
    if method == "mad":
        return winsorize_mad(factor, n=n_mad)
    raise ValueError(f"Unknown winsorize method: {method}")


# ======================================================================
# 2. 数据获取辅助函数（批量 SQL，无逐股循环）
# ======================================================================
def _pool_bars(conn, codes, date, lookback=300):
    """批量获取 codes 最近 lookback 个交易日（截至 date）的日线数据。
    返回 DataFrame index=date, columns=MultiIndex(code, (close*adj, volume, amount))。"""
    date = util.to_date_str(date)
    # 先获得日期区间
    cal = conn.execute(
        "SELECT cal_date FROM trade_calendar WHERE is_open=1 AND cal_date<=? "
        "ORDER BY cal_date DESC LIMIT ?", (date, lookback * 2)
    ).fetchall()
    if not cal:
        return None, None
    start_date = cal[-1][0]
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, trade_date, close, adj_factor, volume, amount, is_suspended "
        f"FROM daily_bar WHERE code IN ({placeholders}) AND trade_date BETWEEN ? AND ? "
        f"ORDER BY code, trade_date", (*codes, start_date, date)
    ).fetchall()
    if not rows:
        return None, None
    df = pd.DataFrame(rows, columns=["code", "trade_date", "close", "adj_factor",
                                      "volume", "amount", "is_suspended"])
    df["adj_close"] = df["close"] * df["adj_factor"].fillna(1.0)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    # pivot -> wide: index=trade_date, columns=code, values=adj_close, amount
    close_pivot = df.pivot_table(index="trade_date", columns="code", values="adj_close", aggfunc="first")
    amount_pivot = df.pivot_table(index="trade_date", columns="code", values="amount", aggfunc="first")
    amt_pivot = df.pivot_table(index="trade_date", columns="code", values="volume", aggfunc="first")
    return close_pivot, amount_pivot


def _pool_fundamental(conn, codes, date):
    """批量获取 codes 截至 date 最近一条基本面截面。
    返回 DataFrame(index=code, columns=pe,pb,market_cap,dividend_yield)。"""
    date = util.to_date_str(date)
    rows = []
    for code in codes:
        r = conn.execute(
            "SELECT pe, pb, market_cap, dividend_yield FROM fundamental "
            "WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
            (code, date)
        ).fetchone()
        if r:
            rows.append((code, r[0], r[1], r[2], r[3]))
    if not rows:
        return pd.DataFrame(index=codes, columns=["pe", "pb", "market_cap", "dividend_yield"])
    df = pd.DataFrame(rows, columns=["code", "pe", "pb", "market_cap", "dividend_yield"]).set_index("code")
    return df.reindex(codes)


def _pool_annual(conn, codes, date):
    """批量获取 codes 截至 date 的最近 5 期年报数据。
    返回 DataFrame(index=code, columns=roe,net_profit)。(取最新一期)"""
    date = util.to_date_str(date)
    rows = []
    for code in codes:
        data = conn.execute(
            "SELECT roe, net_profit, stat_year FROM stock_annual "
            "WHERE code=? AND pub_date IS NOT NULL AND pub_date<=? "
            "ORDER BY stat_year DESC LIMIT 5",
            (code, date)
        ).fetchall()
        if data:
            rows.append((code, data[0][0], data[0][1], [r[2] for r in data],
                         [r[1] for r in data if r[1] is not None]))
    if not rows:
        return pd.DataFrame(index=codes, columns=["roe", "net_profit"])
    out = pd.DataFrame(rows, columns=["code", "roe", "net_profit", "stat_years", "profit_series"])
    return out.set_index("code").reindex(codes)


def _pool_market_bar(conn, date, lookback=300):
    """获取市场代理 sh510300 的后复权收盘价序列。
    返回 Series(index=date, values=adj_close)。"""
    date = util.to_date_str(date)
    cal = conn.execute(
        "SELECT cal_date FROM trade_calendar WHERE is_open=1 AND cal_date<=? "
        "ORDER BY cal_date DESC LIMIT ?", (date, lookback * 2)
    ).fetchall()
    if not cal:
        return None
    start = cal[-1][0]
    rows = conn.execute(
        "SELECT trade_date, close, adj_factor FROM daily_bar "
        "WHERE code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (MARKET_PROXY, start, date)
    ).fetchall()
    if not rows:
        return None
    dates = [r[0] for r in rows]
    prices = [r[1] * (r[2] or 1.0) for r in rows]
    return pd.Series(prices, index=pd.to_datetime(dates))


def _compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Prices DataFrame(t x codes) → 日收益率 DataFrame。"""
    return prices.pct_change().dropna(how="all")


def get_industry(conn, codes):
    """从 stock_industry 表或 industry.csv 获取股票行业映射。
    返回 dict {code: industry_name}，未找到为 None。"""
    ind_map = {}
    # 先查表
    try:
        rows = conn.execute(
            f"SELECT code, industry FROM stock_industry WHERE code IN ({','.join('?' for _ in codes)})",
            codes
        ).fetchall()
        ind_map.update({r[0]: r[1] for r in rows})
    except Exception:
        pass
    # 剩余从 CSV 补
    import os
    csv_path = os.path.join(os.path.dirname(__file__), "industry.csv")
    if os.path.exists(csv_path):
        csv_df = pd.read_csv(csv_path)
        for _, r in csv_df.iterrows():
            c = r["code"]
            if c not in ind_map:
                ind_map[c] = r["industry_name"]
    return {c: ind_map.get(c) for c in codes}


def ensure_industry(conn=None):
    """确保 stock_industry 表存在并刷新（若表空或数据过旧）。
    新建 stock_industry 表（扩展表，不动 schema.sql）。
    若 baostock 可取则动态刷新，失败保留旧数据并 warning。"""
    from db import ensure_table
    ddl = """
    CREATE TABLE IF NOT EXISTS stock_industry (
      code TEXT PRIMARY KEY,
      industry TEXT,
      update_date TEXT
    );"""
    ensure_table(ddl, conn=conn)
    own = conn is None
    if own:
        conn = get_conn()
    try:
        # 检查是否需刷新
        need_refresh = False
        row = conn.execute("SELECT count(*) FROM stock_industry").fetchone()
        if row[0] == 0:
            need_refresh = True
        else:
            max_date = conn.execute("SELECT max(update_date) FROM stock_industry").fetchone()[0]
            if max_date:
                from datetime import datetime, timedelta
                max_dt = datetime.strptime(max_date[:10], "%Y-%m-%d")
                if (datetime.now() - max_dt) > timedelta(days=30):
                    need_refresh = True
        if need_refresh:
            _refresh_industry(conn)
    except Exception as e:
        log.warning("ensure_industry: 刷新行业数据失败, 保留旧表: %s", e)
    finally:
        if own:
            conn.close()


def _refresh_industry(conn):
    """从 baostock 刷新股票行业数据。"""
    try:
        import baostock as bs
        bs.login()
        rs = bs.query_stock_industry()
        rows = []
        while (rs.error_code == "0") and rs.next():
            r = rs.get_row_data()  # code, industry, updateDate
            if r and r[0] and r[1]:
                rows.append((util.with_prefix(r[0]), r[1], r[2] if len(r) > 2 else ""))
        bs.logout()
        if rows:
            conn.execute("DELETE FROM stock_industry")
            conn.executemany(
                "INSERT OR REPLACE INTO stock_industry (code, industry, update_date) VALUES (?,?,?)",
                [(r[0], r[1], r[2]) for r in rows]
            )
            conn.commit()
            log.info("刷新行业数据: %d 条", len(rows))
    except Exception as e:
        log.warning("refresh_industry 失败: %s", e)


# ======================================================================
# 3. 风格因子计算（单日截面，批量向量化）
# ======================================================================
def compute_beta(ret_df: pd.DataFrame, mkt_ret: pd.Series, cap_weight: pd.Series) -> pd.Series:
    """BETA: 60日滚动 beta vs 市场（sh510300）。
    60日窗口内超额收益 e = r - rm 对 rm 做线性回归，取斜率。
    说明: 60 日短窗适用于本项目数据有限的情况，与 CNE6 的 504/252 不同。"""
    dates = ret_df.index
    lookback = min(60, len(dates))
    recent = ret_df.iloc[-lookback:]
    mkt = mkt_ret.reindex(recent.index).dropna()
    codes = recent.columns
    beta_s = pd.Series(np.nan, index=codes)
    for code in codes:
        r = recent[code].dropna()
        common = r.index.intersection(mkt.index)
        if len(common) < 20:
            continue
        re = r[common].values
        rm = mkt[common].values
        # 超额收益
        e = re - rm
        # 回归: e = alpha + beta * rm + eps
        A = np.vstack([np.ones(len(rm)), rm]).T
        try:
            coef, *_ = np.linalg.lstsq(A, e, rcond=None)
            beta_s[code] = coef[1]
        except np.linalg.LinAlgError:
            continue
    return beta_s


def compute_size(fund_df: pd.DataFrame, cap_weight: pd.Series) -> pd.Series:
    """SIZE: ln(总市值) + ln(流通市值近似)。
    本项目仅有总市值(baostock market_cap)，流通市值以 total_market_cap 近似。
    差异声明: 无真实流通股本，ln(float_cap) 以 ln(market_cap) 替代。"""
    mc = fund_df["market_cap"].copy()
    valid = mc.notna() & (mc > 0)
    size = pd.Series(np.nan, index=mc.index)
    size[valid] = np.log(mc[valid])
    return size


def compute_value(fund_df: pd.DataFrame, cap_weight: pd.Series) -> pd.DataFrame:
    """VALUE: 1/PE(TTM) + 1/PB + 股息率，等权复合。"""
    pe, pb, dy = fund_df["pe"].copy(), fund_df["pb"].copy(), fund_df["dividend_yield"].copy()
    ep = pd.Series(np.nan, index=pe.index)
    bp = pd.Series(np.nan, index=pb.index)
    ep_valid = pe.notna() & (pe.abs() > 1e-12)
    bp_valid = pb.notna() & (pb.abs() > 1e-12)
    ep[ep_valid] = 1.0 / pe[ep_valid]
    bp[bp_valid] = 1.0 / pb[bp_valid]
    # 标准化后复合
    z_ep = winsorize_mad(standardize(ep, cap_weight))
    z_bp = winsorize_mad(standardize(bp, cap_weight))
    z_dy = winsorize_mad(standardize(dy, cap_weight))
    df = pd.DataFrame({"EP": z_ep, "BP": z_bp, "DY": z_dy})
    return composite(df, {"EP": 1.0, "BP": 1.0, "DY": 1.0})


def compute_momentum(ret_df: pd.DataFrame, mkt_ret: pd.Series, cap_weight: pd.Series) -> pd.Series:
    """MOMENTUM: RSTR(12-1月动量) + 6月动量 + 3月动量，加权复合。
    RSTR = 过去252日(剔除最近21日)累积超额收益，指数衰减半衰期126日。
    简化: 不做 CNE6 的 11 日滞后平均，声明差异。"""
    dates = ret_df.index
    if len(dates) < 63:
        return pd.Series(np.nan, index=ret_df.columns)
    now = len(dates)
    # 12-1月动量: [-252, -21]
    lo12 = max(0, now - 252)
    lo6 = max(0, now - 126)
    lo3 = max(0, now - 63)
    skip = max(0, now - 21)
    codes = ret_df.columns
    rstr_s = pd.Series(np.nan, index=codes)
    mom6_s = pd.Series(np.nan, index=codes)
    mom3_s = pd.Series(np.nan, index=codes)
    for code in codes:
        r = ret_df[code].dropna()
        common = r.index.intersection(mkt_ret.index)
        if len(common) < 63:
            continue
        re = r[common].values
        rm = mkt_ret[common].values
        excess = re - rm
        # RSTR: 指数衰减加权累积超额收益
        n_rstr = min(len(excess), 252 - 21)
        if n_rstr >= 63:
            exc_rstr = excess[-n_rstr - 21:-21] if len(excess) > 21 + n_rstr else excess[:n_rstr]
            half_life = 126
            w = np.array([2 ** (-(len(exc_rstr) - 1 - i) / half_life)
                          for i in range(len(exc_rstr))])
            w /= w.sum()
            rstr_s[code] = np.dot(exc_rstr, w)
        # 6月动量: 最近126日累计收益
        if len(re) >= 63:
            mom6_s[code] = np.prod(1 + re[-63:]) - 1
        # 3月动量: 最近63日累计收益
        if len(re) >= 21:
            mom3_s[code] = np.prod(1 + re[-21:]) - 1
    rstr_z = winsorize_mad(standardize(rstr_s, cap_weight))
    mom6_z = winsorize_mad(standardize(mom6_s, cap_weight))
    mom3_z = winsorize_mad(standardize(mom3_s, cap_weight))
    df = pd.DataFrame({"RSTR": rstr_z, "MOM6": mom6_z, "MOM3": mom3_z})
    return composite(df, {"RSTR": 2.0, "MOM6": 1.0, "MOM3": 0.5})


def compute_volatility(ret_df: pd.DataFrame, prices_df: pd.DataFrame, cap_weight: pd.Series) -> pd.Series:
    """VOLATILITY: 60日日收益标准差 + 60日ATR/收盘价，复合。
    简化: 以日收益标准差（DASTD）为主，无 CMRA（月累计收益范围）。"""
    lookback = min(60, len(ret_df))
    recent = ret_df.iloc[-lookback:]
    prices = prices_df.iloc[-lookback:]
    codes = ret_df.columns
    dastd_s = pd.Series(np.nan, index=codes)
    atr_s = pd.Series(np.nan, index=codes)
    for code in codes:
        r = recent[code].dropna()
        if len(r) < 20:
            continue
        dastd_s[code] = r.std(ddof=0)
        p = prices[code].dropna()
        if len(p) >= 2:
            # ATR = 过去60日日内振幅均值（用HL替代简化）
            # 简化: 因无H/L数据, 用|Δclose|近似振幅
            p_diff = p.diff().abs().mean()
            atr_s[code] = p_diff / p.iloc[-1] if p.iloc[-1] > 0 else np.nan
    dastd_z = winsorize_mad(standardize(dastd_s, cap_weight))
    atr_z = winsorize_mad(standardize(atr_s, cap_weight))
    df = pd.DataFrame({"DASTD": dastd_z, "ATR": atr_z})
    return composite(df, {"DASTD": 1.0, "ATR": 1.0})


def compute_quality(fund_df: pd.DataFrame, annual_df: pd.DataFrame, cap_weight: pd.Series) -> pd.Series:
    """QUALITY: ROE - 资产负债率（负向）。
    简化: 无毛利率数据，只用 ROE - 杠杆 proxy。
    leverage proxy: 1/PB 可近似资产负债比（高PB→低杠杆），见 Barra 惯例。
    Score = z(ROE) - z(1/PB)。"""
    roe = annual_df["roe"].copy()
    pb = fund_df["pb"].copy()
    bp = pd.Series(np.nan, index=pb.index)
    bp_valid = pb.notna() & (pb.abs() > 1e-12)
    bp[bp_valid] = 1.0 / pb[bp_valid]
    z_roe = winsorize_mad(standardize(roe, cap_weight))
    z_bp = winsorize_mad(standardize(bp, cap_weight))
    # ROE 正向，BP（账面市值比, 杠杆反向代理）正向但取负
    df = pd.DataFrame({"ROE": z_roe, "LEV_PROXY": -z_bp})
    return composite(df, {"ROE": 1.0, "LEV_PROXY": 0.5})


def compute_growth(annual_df: pd.DataFrame, cap_weight: pd.Series) -> pd.Series:
    """GROWTH: 营收增长率 + 利润增长率，等权。
    简化: 仅有 net_profit 5年序列，无营收数据。用净利润增长率近似。
    EGRO = 最近 5 期 net_profit 对时间回归斜率 / mean(|net_profit|)。"""
    codes = annual_df.index
    egro_s = pd.Series(np.nan, index=codes)
    for code in codes:
        row = annual_df.loc[code]
        profits = row.get("profit_series")
        if profits is None or not isinstance(profits, list) or len(profits) < 3:
            continue
        arr = np.array([p for p in profits if p is not None and np.isfinite(p)])
        if len(arr) < 3:
            continue
        t = np.arange(len(arr))
        mean_abs = np.mean(np.abs(arr))
        if mean_abs < 1e-12:
            continue
        try:
            slope, _ = np.polyfit(t, arr, 1)
            egro_s[code] = slope / mean_abs
        except np.linalg.LinAlgError:
            continue
    return winsorize_mad(standardize(egro_s, cap_weight))


def compute_liquidity(ret_df: pd.DataFrame, volume_df: pd.DataFrame,
                       amount_df: pd.DataFrame, fund_df: pd.DataFrame,
                       cap_weight: pd.Series) -> pd.Series:
    """LIQUIDITY: 20日换手率均值（对数化）。
    换手率 turn% = amount × 100 / market_cap。
    STOM = ln(Σ 最近21日 turn)。"""
    codes = ret_df.columns
    mc = fund_df["market_cap"]
    stom_s = pd.Series(np.nan, index=codes)
    for code in codes:
        if code not in amount_df.columns or code not in mc.index:
            continue
        amt = amount_df[code].dropna()
        mcv = mc.loc[code]
        if mcv is None or mcv <= 0 or len(amt) < 5:
            continue
        # 最近21日换手率之和
        recent_amt = amt.iloc[-21:]
        turnover = recent_amt * 100.0 / mcv
        sum_t = turnover.sum()
        if sum_t > 0:
            stom_s[code] = np.log(sum_t)
    return winsorize_mad(standardize(stom_s, cap_weight))


def compute_leverage(fund_df: pd.DataFrame, cap_weight: pd.Series) -> pd.Series:
    """LEVERAGE: 资产负债率近似 + 有息负债率近似。
    简化: 无资产负债表，用 1 - 1/PB 近似资产负债率（高PB→低杠杆），
    等权复合一个杠杆代理。
    差异声明: 无法获取真实资产负债率与有息负债率，本因子为近似代理。"""
    pb = fund_df["pb"].copy()
    # leverage proxy = 1 - 1/PB (当 PB>1 时为正)
    lev = pd.Series(np.nan, index=pb.index)
    valid = pb.notna() & (pb > 0)
    lev[valid] = 1.0 - 1.0 / pb[valid]
    return winsorize_mad(standardize(lev, cap_weight))


def compute_earnings_yield(fund_df: pd.DataFrame, cap_weight: pd.Series) -> pd.Series:
    """EARNINGS_YIELD: E/P（盈利收益率）= 1 / PE(TTM)。
    保留负值（亏损股 ETOP 为负），pe=0/缺失 → NaN。"""
    pe = fund_df["pe"].copy()
    ep = pd.Series(np.nan, index=pe.index)
    valid = pe.notna() & (pe.abs() > 1e-12)
    ep[valid] = 1.0 / pe[valid]
    return winsorize_mad(standardize(ep, cap_weight))


# ── 因子调度表 ────────────────────────────────────────────────────────
_STYLE_COMPUTERS = {
    "BETA": compute_beta,
    "SIZE": compute_size,
    "VALUE": compute_value,
    "MOMENTUM": compute_momentum,
    "VOLATILITY": compute_volatility,
    "QUALITY": compute_quality,
    "GROWTH": compute_growth,
    "LIQUIDITY": compute_liquidity,
    "LEVERAGE": compute_leverage,
    "EARNINGS_YIELD": compute_earnings_yield,
}


# ======================================================================
# 4. 流水线：pipeline 连接因子计算→预处理→正交化
# ======================================================================
def pipeline(factors_dict: dict) -> pd.DataFrame:
    """因子处理流水线：
    1) MAD 去极值
    2) 截面标准化（z-score）
    3) Gram-Schmidt 正交化（顺序 BETA → SIZE → ... → EARNINGS_YIELD）
    返回 DataFrame, columns=因子名, index=股票代码。"""
    order = ORTHO_ORDER
    df = pd.DataFrame(factors_dict)
    # 1) 去极值
    for col in df.columns:
        if col in df:
            df[col] = winsorize_mad(df[col])
    # 2) 标准化
    for col in df.columns:
        if col in df:
            df[col] = standardize(df[col])
    # 3) 正交化（Gram-Schmidt）
    ortho = pd.DataFrame(index=df.index)
    for i, col in enumerate(order):
        if col not in df.columns:
            continue
        if i == 0:
            ortho[col] = df[col]
        else:
            prev = [c for c in order[:i] if c in df.columns]
            if prev:
                ortho[col] = orthogonalize(df[col], ortho[prev])
            else:
                ortho[col] = df[col]
        ortho[col] = standardize(ortho[col])
    return ortho


# ======================================================================
# 5. 核心 API
# ======================================================================
def compute_style_factors(codes, date, conn=None) -> pd.DataFrame:
    """计算全部 10 个风格因子。
    返回 DataFrame(index=code, columns=STYLE_FACTOR_NAMES)。"""
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)
    codes = [util.with_prefix(c) if c[:2] not in ("sh", "sz", "bj") else c for c in codes]
    valid_codes = [c for c in codes if not util.is_bj(c)]

    # 1. 拉数据
    prices, amounts = _pool_bars(conn, valid_codes, date, lookback=300)
    if prices is None or prices.shape[1] == 0:
        if own:
            conn.close()
        return pd.DataFrame(index=codes)
    fund_df = _pool_fundamental(conn, valid_codes, date)
    annual_df = _pool_annual(conn, valid_codes, date)

    # 2. 日收益率
    ret_df = _compute_returns(prices)
    # 市场收益率
    mkt_prices = _pool_market_bar(conn, date, lookback=300)
    mkt_ret = None
    if mkt_prices is not None and len(mkt_prices) > 1:
        mkt_ret = mkt_prices.pct_change().dropna()

    # 3. 市值权重
    cap_weight = fund_df["market_cap"].copy()

    # 4. 计算各因子
    result = {}
    for name, fn in _STYLE_COMPUTERS.items():
        try:
            if name == "BETA":
                if mkt_ret is not None:
                    result[name] = fn(ret_df, mkt_ret, cap_weight)
            elif name == "SIZE":
                result[name] = fn(fund_df, cap_weight)
            elif name == "VALUE":
                result[name] = fn(fund_df, cap_weight)
            elif name == "MOMENTUM":
                if mkt_ret is not None:
                    result[name] = fn(ret_df, mkt_ret, cap_weight)
            elif name == "VOLATILITY":
                result[name] = fn(ret_df, prices, cap_weight)
            elif name == "QUALITY":
                result[name] = fn(fund_df, annual_df, cap_weight)
            elif name == "GROWTH":
                result[name] = fn(annual_df, cap_weight)
            elif name == "LIQUIDITY":
                result[name] = fn(ret_df, prices, amounts, fund_df, cap_weight)
            elif name == "LEVERAGE":
                result[name] = fn(fund_df, cap_weight)
            elif name == "EARNINGS_YIELD":
                result[name] = fn(fund_df, cap_weight)
        except Exception as e:
            log.warning("因子 %s 计算失败: %s", name, e)
            result[name] = pd.Series(np.nan, index=valid_codes)

    if own:
        conn.close()

    df = pd.DataFrame(result, index=valid_codes)
    df.index.name = "code"
    return df


def compute_risk_factors(codes, date, conn=None) -> pd.DataFrame:
    """计算 4 个风险因子。
    - MARKET: CSI300 日收益率（用 sh510300）
    - INDUSTRY: 申万一级行业哑变量
    - STYLE: 以上 10 个风格因子
    - MACRO: 利率变化 + PMI 意外（简化版本，本项目仅占位）

    返回 DataFrame(index=code, columns=list of risk factor names)。"""
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)
    codes = [util.with_prefix(c) if c[:2] not in ("sh", "sz", "bj") else c for c in codes]

    # 风格因子暴露
    style_exp = compute_style_factors(codes, date, conn=conn)
    if style_exp.empty:
        if own:
            conn.close()
        return pd.DataFrame()

    # 行业哑变量
    industry_map = get_industry(conn, codes)
    ind_dummies = pd.get_dummies(pd.Series(industry_map, name="industry").fillna("未知"))
    # 与 style_exp 对齐
    ind_dummies = ind_dummies.reindex(style_exp.index, fill_value=0)

    # 市场因子（常数列，表示对市场的敏感度均为1）
    market_col = pd.Series(1.0, index=style_exp.index)

    # MACRO 因子占位（简化: 无真实宏观数据，填0）
    macro_col = pd.Series(0.0, index=style_exp.index)

    result = pd.concat([
        pd.DataFrame({"MARKET": market_col}, index=style_exp.index),
        ind_dummies.add_prefix("IND_"),
        style_exp.add_prefix("STYLE_"),
        pd.DataFrame({"MACRO": macro_col}, index=style_exp.index),
    ], axis=1)

    if own:
        conn.close()
    return result


_FACTOR_EXPOSURE_CACHE = {}
"""进程级 LRU 缓存 {(date, frozenset(pool)): DataFrame}。"""


def compute_factor_exposures(codes, date, conn=None, use_cache=True) -> pd.DataFrame:
    """返回风格因子暴露 DataFrame（pipeline 处理后的 10 个因子）。
    可作为策略因子评分的直接输入。
    支持 use_cache=True 进行进程级缓存。"""
    date = util.to_date_str(date)
    pool = tuple(sorted(set(codes)))
    if use_cache:
        key = (date, pool)
        if key in _FACTOR_EXPOSURE_CACHE:
            return _FACTOR_EXPOSURE_CACHE[key]
    raw = compute_style_factors(codes, date, conn=conn)
    if raw.empty:
        return raw
    # pipeline 处理
    result = pipeline(raw.to_dict(orient="series"))
    if use_cache:
        key = (date, pool)
        _FACTOR_EXPOSURE_CACHE[key] = result
        if len(_FACTOR_EXPOSURE_CACHE) > 32:
            _FACTOR_EXPOSURE_CACHE.pop(next(iter(_FACTOR_EXPOSURE_CACHE)))
    return result


def compute_exposures(conn, date, pool=None, use_cache=True):
    """符合 OPTIMIZE_V3.md 第三节规范的暴露矩阵（蓝图 API）。
    返回 DataFrame(index=code, columns=RISK_FACTORS+ALPHA_DESCRIPTORS+['lncap_raw','market_cap','industry'])。"""
    date = util.to_date_str(date)
    if pool is None:
        pool = [r[0] for r in conn.execute(
            "SELECT code FROM index_members WHERE index_code='sh000300' AND in_date<=? "
            "AND (out_date IS NULL OR out_date>?)", (date, date)).fetchall()]
    # 基础数据
    style_df = compute_style_factors(pool, date, conn=conn)
    if style_df.empty:
        return pd.DataFrame()
    fund_df = _pool_fundamental(conn, pool, date)
    industry_map = get_industry(conn, pool)

    # 构建风险因子列
    # 注意: 蓝图 RISK_FACTORS = ["size","beta","momentum","resvol","liquidity","btop"]
    # 我们映射:
    # size -> SIZE (取负, 因为cn清单是大市值正向, barra模型里size通常是正向的)
    # beta -> BETA
    # momentum -> MOMENTUM
    # resvol -> VOLATILITY (正相关, 高波动=高风险)
    # liquidity -> LIQUIDITY (低换手=低流动性风险)
    # btop -> BOOK-TO-PRICE = 1/PB
    result = pd.DataFrame(index=style_df.index)
    result["size"] = style_df["SIZE"] if "SIZE" in style_df else np.nan
    result["beta"] = style_df["BETA"] if "BETA" in style_df else np.nan
    result["momentum"] = style_df["MOMENTUM"] if "MOMENTUM" in style_df else np.nan
    result["resvol"] = style_df["VOLATILITY"] if "VOLATILITY" in style_df else np.nan
    result["liquidity"] = style_df["LIQUIDITY"] if "LIQUIDITY" in style_df else np.nan
    # btop: 1/PB 标准化
    pb = fund_df["pb"].copy()
    bp = pd.Series(np.nan, index=pb.index)
    valid = pb.notna() & (pb > 0)
    bp[valid] = 1.0 / pb[valid]
    result["btop"] = winsorize_mad(standardize(bp, fund_df["market_cap"]))
    # Alpha 描述符
    for col in ALPHA_DESCRIPTORS:
        result[col] = 0.0  # 占位, 策略层再替换
    # 附加列
    result["lncap_raw"] = np.log(fund_df["market_cap"].clip(lower=1e6))
    result["market_cap"] = fund_df["market_cap"]
    result["industry"] = pd.Series(industry_map)
    return result


# ======================================================================
# 6. 兼容接口: factor_score (供策略 score_stocks 调用)
# ======================================================================
def factor_score(context) -> pd.Series:
    """供策略调用的统一入口。
    返回 pd.Series(score, index=code)，范围标准化到 -1~1。
    context 需有 conn, date, members() 等属性。
    具体使用方式: 各策略在 generate_orders 中按权重组合各因子 z 分。"""
    date = context.date
    conn = context.conn
    pool = context.members("sh000300", date)
    exposures = compute_factor_exposures(pool, date, conn=conn)
    # 返回第一个因子得分作为默认（策略应自行组合）
    if exposures.empty:
        return pd.Series(dtype=float)
    return exposures.iloc[:, 0].fillna(0).clip(-3, 3) / 3.0


# ======================================================================
# 7. 简易自检
# ======================================================================
def _self_test():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s|%(name)s|%(message)s")
    conn = get_conn()
    date = "2026-07-03"
    pool = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE trade_date=? LIMIT 50", (date,)).fetchall()]
    if not pool:
        pool = [r[0] for r in conn.execute(
            "SELECT code FROM index_members WHERE index_code='sh000300' AND in_date<=? "
            "AND (out_date IS NULL OR out_date>?) LIMIT 10", (date, date)).fetchall()]
    log.info("测试池: %d 只", len(pool))
    if len(pool) > 5:
        exp = compute_factor_exposures(pool[:10], date, conn=conn)
        log.info("Exposures 形状: %s", exp.shape)
        log.info("列: %s", list(exp.columns))
        log.info("前 3 行:\n%s", exp.head(3))
    conn.close()


if __name__ == "__main__":
    _self_test()
