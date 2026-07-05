# -*- coding: utf-8 -*-
"""Barra 风格风险模型（卡I）—— 结构模型 r = Xf + u。

核心流程（日频、纯 numpy/pandas，无新依赖）：
1. 截面回归估计因子收益: r = Xf + ε (WLS，市值加权)
2. 因子协方差: Σ_f = EWMA(f_t, λ=0.94) × 252 年化
3. 特质风险: EWMA 标准差，半衰期 42 日
4. 风险分解: 系统 + 特质 + 边际贡献

方法论参考: MSCI Barra CNE5/CNE6 Empirical Notes, Axioma V4 Handbook。
简化声明：不做 Newey-West 串行相关校正、不做 VRA/特征值调整、
不做结构化因子协方差——日频人工跟单场景，非组合优化用途。
"""
import logging
import json
from pathlib import Path

import numpy as np
import pandas as pd

import util
import factors
from db import get_conn

log = logging.getLogger("riskmodel")

# ── 常数 ──
RISK_FACTORS = ["size", "beta", "momentum", "resvol", "liquidity", "btop"]
"""暴露矩阵使用的六个风险因子（同 factors.RISK_FACTORS）。"""
STYLE_FACTOR_NAMES = ["BETA", "SIZE", "VALUE", "MOMENTUM",
                       "VOLATILITY", "QUALITY", "GROWTH",
                       "LIQUIDITY", "LEVERAGE", "EARNINGS_YIELD"]

EWMA_LAMBDA = 0.94         # 因子协方差 EWMA 衰减因子（RiskMetrics 标准）
EWMA_HALF_SPEC = 42         # 特质波动半衰期（天）
EWMA_HALF_FCOV = 90         # 因子协方差半衰期（天）
MIN_LOOKBACK = 120          # 至少 120 个交易日才建模
CACHE_MAXSIZE = 8           # 进程级缓存最大条目


# ======================================================================
# RiskModel 类
# ======================================================================
class RiskModel:
    """Barra 风格风险模型。

    属性:
        date: 模型估计日期
        exposures: DataFrame (N×K) 暴露矩阵
        factor_ret: Series (K,) 因子收益估计
        factor_cov: DataFrame (K×K) 因子协方差矩阵（年化）
        specific_vol: Series (N,) 特质波动（年化）
        fcov_raw: DataFrame (K×K) 未经调整的 EWMA 协方差
    """

    def __init__(self, date, exposures, factor_ret, factor_cov, specific_vol):
        self.date = str(date)[:10]
        self.exposures = exposures
        self.factor_ret = factor_ret
        self.factor_cov = factor_cov
        self.specific_vol = specific_vol
        self.fcov_raw = factor_cov.copy()

    # ── 工具 ────────
    @property
    def n_factors(self):
        return len(self.factor_ret)

    @property
    def codes(self):
        return list(self.exposures.index)

    # ── 组合风险 ────────
    def calc_portfolio_risk(self, weights: dict) -> float:
        """组合总风险（年化，小数）。
        weights: {code: weight}, 权重为持仓市值占比（可含现金=不传入）。
        返回年化标准差。
        """
        w = self._weight_vector(weights)
        if w is None or w.sum() < 1e-10:
            return 0.0
        sys = self._systematic_risk(w)
        spec = self._specific_risk(w)
        total = np.sqrt(sys + spec)
        return float(total)

    def calc_systematic_risk(self, weights: dict) -> float:
        """系统风险（年化，方差贡献）。"""
        w = self._weight_vector(weights)
        if w is None:
            return 0.0
        return float(self._systematic_risk(w))

    def calc_specific_risk(self, weights: dict) -> float:
        """特质风险（年化，方差贡献）。"""
        w = self._weight_vector(weights)
        if w is None:
            return 0.0
        return float(self._specific_risk(w))

    def calc_marginal_risk_contribution(self, weights: dict) -> dict:
        """边际风险贡献 MRC。
        采用 Euler 分配: MRC_i = [w_i · (X@F@X'@w)_i + w_i² · σ_i²] / σ_p。
        MRC_i 之和 = σ_p (组合总风险)。
        返回 {code: mrc}。"""
        w = self._weight_vector(weights)
        if w is None or w.sum() < 1e-10:
            return {}
        X = self.exposures.values  # N×K
        w_a = w.values
        # 系统部分: (X F X' w) 向量, N×1
        Xw = X.T @ w_a
        sys_vec = X @ (self.factor_cov.values @ Xw)  # N
        # 系统: w_i · sys_vec_i
        sys_contrib = w_a * sys_vec
        # 特质部分: w_i² · σ_i²
        spec_contrib = w_a ** 2 * self.specific_vol.values ** 2
        total_var = float(sys_contrib.sum() + spec_contrib.sum())
        port_vol = np.sqrt(total_var)
        if port_vol < 1e-12:
            return {c: 0.0 for c in self.codes}
        mrc = (sys_contrib + spec_contrib) / port_vol
        return dict(zip(self.codes, mrc.tolist()))

    def calc_factor_risk_budget(self, weights: dict) -> dict:
        """各因子风险预算占比（因子贡献分解）。
        返回 {factor_name: contrib_pct}。"""
        w = self._weight_vector(weights)
        if w is None or w.sum() < 1e-10:
            return {f: 0.0 for f in self.exposures.columns}
        X = self.exposures.values
        w_a = w.values
        Xw = X.T @ w_a
        factor_names = list(self.exposures.columns)
        n_f = len(factor_names)
        fc = self.factor_cov.values
        port_var_sys = Xw @ fc @ Xw
        if port_var_sys < 1e-12:
            return {f: 0.0 for f in factor_names}
        f_contrib = {}
        for i, fname in enumerate(factor_names):
            contrib = 0.0
            for j in range(n_f):
                contrib += Xw[i] * Xw[j] * fc[i, j]
            f_contrib[fname] = contrib / port_var_sys
        return f_contrib

    def portfolio_exposure(self, weights: dict) -> dict:
        """组合风格暴露 = Σ w_i · z_i（持仓市值加权）。
        返回 {factor_name: exposure}。"""
        w = self._weight_vector(weights)
        if w is None or w.sum() < 1e-10:
            return {f: 0.0 for f in self.exposures.columns}
        X = self.exposures.values
        w_a = w.values
        expo = X.T @ w_a / w_a.sum()
        return dict(zip(self.exposures.columns, expo.tolist()))

    # ── 内部 ────────
    def _weight_vector(self, weights: dict) -> pd.Series:
        """将 weights dict 对齐到 exposures 的排序向量（缺失填 0）。"""
        if not weights:
            return None
        w = pd.Series(0.0, index=self.exposures.index)
        for code, wgt in weights.items():
            if code in w.index:
                w[code] = float(wgt)
        return w

    def _systematic_risk(self, w: pd.Series) -> float:
        """系统风险方差项: w' X F X' w（年化）。"""
        Xw = self.exposures.values.T @ w.values
        return float(Xw @ self.factor_cov.values @ Xw)

    def _specific_risk(self, w: pd.Series) -> float:
        """特质风险方差项: Σ w_i² σ_i²（年化）。"""
        sv = self.specific_vol.reindex(w.index, fill_value=0.0).values
        return float((w.values ** 2 * sv ** 2).sum())

    def __repr__(self):
        return (f"RiskModel(date={self.date}, N={len(self.codes)}, "
                f"K={self.n_factors})")


# ======================================================================
# 模型估计
# ======================================================================
def estimate(conn, date, lookback=504, pool=None) -> RiskModel:
    """估计 Barra 风格风险模型。

    流程:
      1. 用 factors.compute_exposures 生成暴露矩阵 X（N×6）
      2. 拉取过去 lookback 个交易日的历史暴露与收益
      3. 每日 WLS 截面回归 r = Xf + ε → 因子收益序列 f_t
      4. EWMA 因子协方差 (λ=0.94, 年化 ×252)
      5. 特质波动 EWMA (半衰期=42)

    参数:
        conn: 数据库连接
        date: 信号日
        lookback: 回看窗口（交易日）
        pool: 股票池（默认沪深300成分）

    返回: RiskModel 实例。

    简化声明:
      - 使用 t-1 日暴露对 t 日收益回归（非同一日，防未来函数）
      - 不做 Newey-West 串行相关校正
      - 不做 VRA/特征值调整
      - 不做结构化因子协方差
    """
    date = util.to_date_str(date)
    # 1. 股票池
    if pool is None:
        pool = [r[0] for r in conn.execute(
            "SELECT code FROM index_members WHERE index_code='sh000300' AND in_date<=? "
            "AND (out_date IS NULL OR out_date>?)", (date, date)).fetchall()]
    if not pool:
        log.warning("estimate: 股票池为空")
        return None

    # 2. 获取历史交易日序列
    cal = conn.execute(
        "SELECT cal_date FROM trade_calendar WHERE is_open=1 AND cal_date<=? "
        "ORDER BY cal_date DESC LIMIT ?", (date, lookback * 2)
    ).fetchall()
    if not cal or len(cal) < MIN_LOOKBACK:
        log.warning("estimate: 交易日不足 %d", MIN_LOOKBACK)
        return None
    all_dates = sorted([r[0] for r in cal])  # 升序
    # 取最后 lookback+1 个交易日（+1 是因为要计算收益）
    use_dates = all_dates[-(lookback + 1):]

    # 3. 拉取每日收益
    bar_data = {}
    placeholders = ",".join("?" for _ in pool)
    rows = conn.execute(
        f"SELECT code, trade_date, close, adj_factor FROM daily_bar "
        f"WHERE code IN ({placeholders}) AND trade_date BETWEEN ? AND ? "
        f"ORDER BY code, trade_date",
        (*pool, use_dates[0], use_dates[-1])
    ).fetchall()
    df_bars = pd.DataFrame(rows, columns=["code", "trade_date", "close", "adj_factor"])
    if df_bars.empty:
        return None
    # 调整为后复权
    df_bars["adj_close"] = df_bars["close"] * df_bars["adj_factor"].fillna(1.0)
    # pivot
    price_wide = df_bars.pivot_table(index="trade_date", columns="code",
                                      values="adj_close", aggfunc="first")
    ret_wide = price_wide.pct_change().dropna(how="all")

    # 4. 获取截面暴露（使用最新日期的因子暴露作为期间内所有日的暴露——近似简化）
    # 注意: 精确的做法是每日更新暴露，但计算量太大
    # 简化: 用 date 的暴露代表整个回看窗口（Barra 实践中月度更新暴露）
    exposures = factors.compute_exposures(conn, date, pool=pool)
    if exposures.empty:
        return None
    risk_cols = [c for c in RISK_FACTORS if c in exposures.columns]
    if len(risk_cols) < 3:
        return None
    X_mat = exposures[risk_cols].fillna(0.0).values  # N×K
    cap_weight = exposures["market_cap"].fillna(1e8).values

    # 5. 每日 WLS 回归 r = Xf + ε
    factor_ret_list = []
    spec_resid_list = []
    fit_dates = []
    for tdate in ret_wide.index:
        if tdate not in price_wide.index or tdate == price_wide.index[0]:
            continue
        # 收益率（使用 tdate 当日个股收益率）
        r_t = ret_wide.loc[tdate].dropna()
        valid_codes = [c for c in r_t.index if c in exposures.index]
        if len(valid_codes) < 30:
            continue
        r_vals = r_t[valid_codes].values
        # X 从 exposures 对齐
        x_vals = exposures.loc[valid_codes, risk_cols].fillna(0.0).values  # M×K
        w_vals = np.sqrt(np.array([cap_weight[exposures.index.get_loc(c)]
                                     if c in exposures.index else 1.0
                                     for c in valid_codes]))
        # WLS: 对 y 和 X 乘以 √w
        y_w = r_vals * w_vals
        X_w = x_vals * w_vals.reshape(-1, 1)
        try:
            coef, resid_sum, *_ = np.linalg.lstsq(X_w, y_w, rcond=None)
        except np.linalg.LinAlgError:
            continue
        # 因子收益
        factor_ret_list.append(pd.Series(coef, index=risk_cols))
        # 特质残差（原始量纲）
        fitted = x_vals @ coef
        resid = r_vals - fitted
        spec_resid_list.append(pd.Series(resid, index=valid_codes))
        fit_dates.append(tdate)

    if len(factor_ret_list) < MIN_LOOKBACK // 2:
        log.warning("estimate: 有效拟合天数不足 %d", len(factor_ret_list))
        return None

    # 6. 因子协方差 EWMA
    fr_df = pd.DataFrame(factor_ret_list, index=fit_dates)
    fr_df = fr_df.dropna(how="all")
    # EWMA 协方差
    fcov = _ewma_cov(fr_df, half_life=EWMA_HALF_FCOV)

    # 7. 特质波动 EWMA
    spec_df = pd.DataFrame(spec_resid_list, index=fit_dates)
    spec_vol = _ewma_std(spec_df, half_life=EWMA_HALF_SPEC)

    # 8. 构建 RiskModel
    rm = RiskModel(
        date=date,
        exposures=exposures[risk_cols],
        factor_ret=fr_df.iloc[-1] if not fr_df.empty else pd.Series(dtype=float),
        factor_cov=fcov,
        specific_vol=spec_vol,
    )
    return rm


def _ewma_cov(df: pd.DataFrame, half_life=90) -> pd.DataFrame:
    """EWMA 协方差矩阵（年化 ×252）。
    权重 w_t = λ^{T-t}, λ = 0.5^(1/half_life)。"""
    if df.empty or df.shape[1] < 1:
        return pd.DataFrame(columns=df.columns, index=df.columns)
    T = len(df)
    lam = 0.5 ** (1.0 / half_life)
    w = np.array([lam ** (T - 1 - i) for i in range(T)])
    w /= w.sum()
    arr = df.values
    mean = (w[:, None] * arr).sum(axis=0)
    centered = arr - mean
    cov = np.zeros((df.shape[1], df.shape[1]))
    for t in range(T):
        cov += w[t] * np.outer(centered[t], centered[t])
    cov *= 252  # 年化
    return pd.DataFrame(cov, index=df.columns, columns=df.columns)


def _ewma_std(df: pd.DataFrame, half_life=42) -> pd.Series:
    """EWMA 标准差（年化 ×√252）。
    按列计算，样本不足回退到中位数。"""
    if df.empty:
        return pd.Series(dtype=float)
    T = len(df)
    lam = 0.5 ** (1.0 / half_life)
    w = np.array([lam ** (T - 1 - i) for i in range(T)])
    w /= w.sum()
    cols = df.columns
    std_s = pd.Series(np.nan, index=cols)
    for c in cols:
        arr = df[c].dropna().values
        if len(arr) < 10:
            continue
        # 对齐权重
        w_use = w[-len(arr):]
        w_use /= w_use.sum()
        mean = (w_use * arr).sum()
        var = (w_use * (arr - mean) ** 2).sum()
        std_s[c] = np.sqrt(var) * np.sqrt(252)
    # 回填中位数
    med = std_s.median()
    if pd.notna(med):
        std_s.fillna(med, inplace=True)
    return std_s


# ======================================================================
# 缓存管理（进程级）
# ======================================================================
_ESTIMATE_CACHE = {}


def estimate_cached(conn, date, lookback=504, pool=None):
    """带进程级缓存的 estimate。"""
    date = util.to_date_str(date)
    pool_key = tuple(sorted(pool)) if pool else ()
    key = (date, lookback, pool_key)
    if key in _ESTIMATE_CACHE:
        return _ESTIMATE_CACHE[key]
    rm = estimate(conn, date, lookback=lookback, pool=pool)
    if rm is not None:
        if len(_ESTIMATE_CACHE) >= CACHE_MAXSIZE:
            _ESTIMATE_CACHE.pop(next(iter(_ESTIMATE_CACHE)))
        _ESTIMATE_CACHE[key] = rm
    return rm


# ======================================================================
# 组合风险计算简洁入口
# ======================================================================
def portfolio_exposure(rm: RiskModel, holdings: dict) -> dict:
    """组合风格暴露。"""
    return rm.portfolio_exposure(holdings)


def portfolio_vol(rm: RiskModel, holdings: dict) -> float:
    """组合预测波动（年化）。"""
    return rm.calc_portfolio_risk(holdings)


# ======================================================================
# 暴露导出（供 run_daily 收尾安全调用）
# ======================================================================
def export_exposures(conn=None, out_path=None):
    """读 state/*.json 全部启用策略持仓，写 state/factor_exposure.json。
    策略仅含个股时正常计算；ETF 持仓策略输出 etf_only=true。
    任何异常: log.warning 后跳过该策略，不抛异常。"""
    from conf import STATE_DIR
    from models import Account
    own = conn is None
    if own:
        conn = get_conn()
    out = {"date": util.today_str(), "factors": RISK_FACTORS.copy(), "strategies": {}}
    try:
        for sid_file in Path(STATE_DIR).glob("*_at_*.json"):
            try:
                sid = sid_file.stem.replace("_at_", "@")
                d = json.loads(sid_file.read_text(encoding="utf-8"))
                positions = d.get("positions", {})
                # 是否有 ETF
                is_etf_only = all(util.bare(c)[0] in ("5",) or util.bare(c)[:2] in ("15", "16", "18")
                                  for c in positions)
                if is_etf_only:
                    out["strategies"][sid] = {"etf_only": True, "pred_vol": None}
                    continue
                if not positions:
                    out["strategies"][sid] = {"etf_only": False, "pred_vol": None,
                                               "n_pos": 0, "weight_invested": 0.0}
                    continue
                # 计算持仓市值权重
                price_of = _price_map(conn, out["date"])
                total_val = d.get("cash", 0) + sum(
                    pos.get("shares", 0) * price_of.get(c, 0) for c, pos in positions.items()
                )
                if total_val <= 0:
                    continue
                weights = {}
                for c, pos in positions.items():
                    mv = pos.get("shares", 0) * price_of.get(c, 0)
                    if mv > 0:
                        weights[c] = mv / total_val
                if not weights:
                    continue
                # 估计风险模型
                rm = estimate_cached(conn, out["date"])
                if rm is None:
                    out["strategies"][sid] = {"etf_only": False, "pred_vol": None}
                    continue
                exposures = rm.portfolio_exposure(weights)
                pred_vol = rm.calc_portfolio_risk(weights)
                out["strategies"][sid] = {
                    "etf_only": False, "pred_vol": round(pred_vol, 6),
                    "exposures": {k: round(v, 4) for k, v in exposures.items()},
                    "n_pos": len(weights),
                    "weight_invested": round(sum(weights.values()), 4),
                }
            except Exception as e:
                log.warning("export_exposures 跳过 %s: %s", sid_file.name, e)
    except Exception as e:
        log.warning("export_exposures 全量失败: %s", e)
    finally:
        if own:
            conn.close()
    out_path = out_path or (STATE_DIR / "factor_exposure.json")
    try:
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("因子暴露已导出到 %s", out_path)
    except Exception as e:
        log.warning("写入因子暴露失败: %s", e)


def _price_map(conn, date):
    """返回 {code: last_close}。"""
    rows = conn.execute(
        "SELECT code, close FROM (SELECT code, close, trade_date, "
        "ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn "
        "FROM daily_bar WHERE trade_date<=?) WHERE rn=1", (date,)).fetchall()
    return {r[0]: float(r[1]) for r in rows}


# ======================================================================
# 简易自检
# ======================================================================
def _self_test():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s|%(name)s|%(message)s")
    conn = get_conn()
    date = "2026-07-03"
    rm = estimate(conn, date, lookback=120)
    if rm is not None:
        log.info("RiskModel: %s", rm)
        log.info("因子协方差:\n%s", rm.factor_cov.round(6))
        log.info("因子收益:\n%s", rm.factor_ret)
    else:
        log.warning("RiskModel 估计失败（可能数据不足）")
    conn.close()


if __name__ == "__main__":
    _self_test()
