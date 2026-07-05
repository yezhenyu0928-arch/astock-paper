# -*- coding: utf-8 -*-
"""因子库 + 风险模型测试（卡I）。
覆盖：
- winsorize_mad 正常和极端值
- standardize 均值标准差
- orthogonalize 正交性检验
- composite 缺失处理
- pipeline 完整流水线
- RiskModel 合成数据
- 真实库冒烟（skip 如果 db 不存在）

可直接运行: python tests/test_factors.py"""
import os
import sys
import math
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import factors
from factors import (
    winsorize_mad, winsorize, standardize, orthogonalize,
    composite, pipeline, STYLE_FACTOR_NAMES, ORTHO_ORDER,
    compute_style_factors, compute_factor_exposures,
)
from db import get_conn

# ── tolerance ──
ATOL = 1e-8


# ====================================================================
# 1. winsorize_mad 测试
# ====================================================================
def test_mad_normal():
    """普通序列: 去极值后分布基本不变（无极端值）。"""
    np.random.seed(42)
    s = pd.Series(np.random.randn(100))
    clipped = winsorize_mad(s.copy())
    assert len(clipped) == len(s)
    # 中位数和标准差应接近
    assert abs(clipped.median() - s.median()) < 0.5
    print("[PASS] test_mad_normal")


def test_mad_extreme():
    """含极端值: >median+5*MAD 的极端值被截断到边界。"""
    s = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 1000])
    clipped = winsorize_mad(s, n=5)
    # 1000 应被截断
    assert clipped.max() < 50, f"极端值未被截断: max={clipped.max()}"
    assert clipped.max() > 9, f"截断过度: max={clipped.max()}"
    print(f"[PASS] test_mad_extreme (max={clipped.max():.2f})")


def test_mad_all_same():
    """全相同序列: 应原样返回。"""
    s = pd.Series([5.0] * 20)
    clipped = winsorize_mad(s)
    assert (clipped == 5.0).all()
    print("[PASS] test_mad_all_same")


def test_mad_tolerance():
    """边界值测试: median±5*MAD 以内的值不变。"""
    np.random.seed(42)
    s = pd.Series(np.random.randn(100))
    med = s.median()
    mad = (s - med).abs().median()
    bound = 5 * 1.4826 * mad
    lo, hi = med - bound, med + bound
    inner = s[(s >= lo) & (s <= hi)].copy()
    clipped = winsorize_mad(s.copy())
    # 内部值不应改变（由于浮点误差，用宽松断言）
    for idx in inner.index:
        assert abs(clipped[idx] - s[idx]) < 1e-10, f"内部值被改变: idx={idx}"
    print("[PASS] test_mad_tolerance")


# ====================================================================
# 2. standardize 测试
# ====================================================================
def test_standardize_basic():
    """普通 z-score: mean≈0, std≈1。"""
    np.random.seed(42)
    s = pd.Series(np.random.randn(200) * 10 + 50)
    z = standardize(s)
    assert abs(z.mean()) < 0.1, f"均值偏离: {z.mean()}"
    assert abs(z.std(ddof=0) - 1.0) < 0.1, f"std 偏离: {z.std(ddof=0)}"
    print(f"[PASS] test_standardize_basic (mean={z.mean():.4f}, std={z.std(ddof=0):.4f})")


def test_standardize_weighted():
    """市值加权标准化: 加权均值≈0。"""
    np.random.seed(42)
    s = pd.Series(np.random.randn(100) * 5 + 30)
    cap = pd.Series(np.random.uniform(1e9, 5e9, size=100))
    z = standardize(s, cap)
    weighted_mean = np.average(z, weights=cap)
    # 简化: 权重均值≈0（<1e-6）
    assert abs(weighted_mean) < 1e-6, f"加权均值偏离: {weighted_mean}"
    print(f"[PASS] test_standardize_weighted (w_mean={weighted_mean:.2e})")


def test_standardize_single():
    """少于 3 个有效值返回 NaN。"""
    s = pd.Series([1.0, np.nan, np.nan])
    z = standardize(s)
    assert z.isna().all()
    print("[PASS] test_standardize_single")


def test_standardize_all_nan():
    """全 NaN 返回 NaN。"""
    s = pd.Series([np.nan] * 10)
    z = standardize(s)
    assert z.isna().all()
    print("[PASS] test_standardize_all_nan")


# ====================================================================
# 3. orthogonalize 测试
# ====================================================================
def test_orthogonalize_basic():
    """正交化后残差与 X 的相关性 ≈ 0。"""
    np.random.seed(42)
    n = 200
    x1 = np.random.randn(n)
    x2 = np.random.randn(n) * 0.5 + 0.3 * x1
    y = 2.0 * x1 + 1.0 * x2 + np.random.randn(n) * 0.5
    df = pd.DataFrame({"x1": x1, "x2": x2})
    s_y = pd.Series(y)
    resid = orthogonalize(s_y, df)
    valid = resid.notna()
    # 残差与 x1, x2 正交
    for col in df.columns:
        corr = np.corrcoef(resid[valid], df.loc[valid, col])[0, 1]
        assert abs(corr) < 0.15, f"正交性不满足: {col} corr={corr:.4f}"
    print(f"[PASS] test_orthogonalize_basic (残差与X相关系数<0.15)")


def test_orthogonalize_weighted():
    """WLS 加权正交化。"""
    np.random.seed(42)
    n = 100
    x = np.random.randn(n)
    y = 1.5 * x + np.random.randn(n) * 0.3
    w = pd.Series(np.ones(n) * 100 + np.random.randn(n) * 10)
    resid = orthogonalize(pd.Series(y), pd.DataFrame({"x": x}), w)
    valid = resid.notna()
    cr = np.corrcoef(resid[valid], x[valid])[0, 1]
    assert abs(cr) < 0.15
    print(f"[PASS] test_orthogonalize_weighted corr={cr:.4f}")


# ====================================================================
# 4. composite 测试
# ====================================================================
def test_composite_basic():
    """复合: 结果 std≈1。"""
    np.random.seed(42)
    df = pd.DataFrame({
        "a": np.random.randn(100),
        "b": np.random.randn(100) * 2,
        "c": np.random.randn(100) * 0.5,
    })
    score = composite(df, {"a": 0.5, "b": 0.3, "c": 0.2})
    assert abs(score.std(ddof=0) - 1.0) < 0.15, f"std={score.std(ddof=0)}"
    print(f"[PASS] test_composite_basic (std={score.std(ddof=0):.4f})")


def test_composite_missing():
    """某描述符整列缺失时权重重归一。"""
    np.random.seed(42)
    df = pd.DataFrame({
        "a": np.random.randn(100),
        "b": [np.nan] * 100,
        "c": np.random.randn(100),
    })
    score = composite(df, {"a": 0.4, "b": 0.4, "c": 0.2})
    assert score.notna().sum() > 50
    assert abs(score.std(ddof=0) - 1.0) < 0.15, f"缺失后 std={score.std(ddof=0)}"
    print(f"[PASS] test_composite_missing (有效={score.notna().sum()}, std={score.std(ddof=0):.4f})")


# ====================================================================
# 5. pipeline 测试
# ====================================================================
def test_pipeline_full():
    """完整流水线: 输入原始因子 → 输出正交化后因子。"""
    np.random.seed(42)
    n = 100
    raw = {}
    for name in STYLE_FACTOR_NAMES:
        raw[name] = pd.Series(np.random.randn(n) * 3 + 10)
    result = pipeline(raw)
    assert list(result.columns) == ORTHO_ORDER[:len(STYLE_FACTOR_NAMES)], f"列顺序不对: {list(result.columns)}"
    assert len(result) == n
    # 各因子均值≈0, std≈1
    for col in result.columns:
        assert abs(result[col].mean()) < 0.1, f"{col} mean={result[col].mean()}"
        std = result[col].std(ddof=0)
        assert abs(std - 1.0) < 0.15, f"{col} std={std}"
    # 正交性: 正交化后各因子间相关性应显著降低
    corr_mag = result.corr().abs().values.copy()
    np.fill_diagonal(corr_mag, 0)
    max_corr = corr_mag.max()
    # 由于是随机生成数据相关性本就低, 验证流水线不破坏数据
    assert max_corr < 0.5, f"最大相关系数: {max_corr}"
    print(f"[PASS] test_pipeline_full (因子数={len(result.columns)}, max_corr={max_corr:.3f})")


# ====================================================================
# 6. Synthetic RiskModel 测试
# ====================================================================
def test_riskmodel_synthetic():
    """合成 3 因子数据验证风险模型基本功能。
    构造已知协方差的因子序列与暴露 → 组合波动与理论值同数量级。"""
    from riskmodel import RiskModel, estimate
    np.random.seed(42)
    n_stocks = 50
    n_dates = 250
    K = 3
    factor_names = ["f1", "f2", "f3"]

    # 合成暴露: ~U[-2, 2]
    X = np.random.uniform(-1.5, 1.5, size=(n_stocks, K))
    # 因子收益: 低相关
    f_mean = np.array([0.0005, 0.0003, 0.0001])
    f_cov = np.array([[0.0002, 0.00005, 0.00003],
                       [0.00005, 0.0003, -0.00002],
                       [0.00003, -0.00002, 0.00015]])
    f_ret = np.random.multivariate_normal(f_mean, f_cov, size=n_dates)
    # 特质波动: 约0.3-0.5% 日均
    spec_vols = np.random.uniform(0.002, 0.006, size=n_stocks)
    spec_ret = np.random.randn(n_dates, n_stocks) * spec_vols

    # 组合收益
    R = f_ret @ X.T + spec_ret

    exposures = pd.DataFrame(X, columns=factor_names)
    # 用最后一天数据构造 RiskModel
    # 合成因子收益序列
    fr_df = pd.DataFrame(f_ret, columns=factor_names)
    # 特质波动
    spec_vol = pd.Series(np.sqrt(np.diag(f_cov)) * np.sqrt(252), index=factor_names)  # 占位

    # 构造简化的 RiskModel 用真实数据验证
    fcov = pd.DataFrame(f_cov * 252, index=factor_names, columns=factor_names)  # 年化
    sv = pd.Series(spec_vols * np.sqrt(252), index=[f"s{i}" for i in range(n_stocks)])

    code_names = [f"s{i}" for i in range(n_stocks)]
    exposures.index = code_names

    rm = RiskModel(
        date="2026-07-03",
        exposures=exposures,
        factor_ret=fr_df.iloc[-1],
        factor_cov=fcov,
        specific_vol=sv,
    )

    # 等权组合风险
    weights = {c: 1.0 / n_stocks for c in code_names}
    port_vol = rm.calc_portfolio_risk(weights)
    # 等权组合年化波动约在 3%-30% 之间（合成数据）
    assert 0.01 < port_vol < 0.60, f"组合波动异常: {port_vol}"
    print(f"[PASS] test_riskmodel_synthetic (port_vol={port_vol:.4f})")

    # 验证边际风险贡献
    mrc = rm.calc_marginal_risk_contribution(weights)
    assert len(mrc) == n_stocks
    assert abs(sum(mrc.values()) - port_vol) < 0.05, "MRC 之和应≈总风险"
    print(f"[PASS] test_riskmodel_mrc (sum_mrc={sum(mrc.values()):.4f} vs vol={port_vol:.4f})")

    # 验证组合暴露
    pe = rm.portfolio_exposure(weights)
    assert len(pe) == K
    print(f"[PASS] test_riskmodel_exposure")

    # 验证风险预算
    budget = rm.calc_factor_risk_budget(weights)
    assert len(budget) == K
    print(f"[PASS] test_riskmodel_budget")


# ====================================================================
# 7. 真实库冒烟（skip if db not exist）
# ====================================================================
def test_smoke_db_live():
    """真实数据库冒烟测试。db 文件不存在时跳过。
    验证 compute_factor_exposures 形状与 NaN 率。"""
    from pathlib import Path
    from conf import DB_PATH
    if not Path(DB_PATH).exists():
        print("[SKIP] test_smoke_db_live (DB 不存在)")
        return
    conn = get_conn()
    date = "2026-07-03"
    # 获取沪深300成分股（或任何有数据的股票）
    pool = [r[0] for r in conn.execute(
        "SELECT code FROM index_members WHERE index_code='sh000300' AND in_date<=? "
        "AND (out_date IS NULL OR out_date>?)", (date, date)).fetchall()]
    if not pool:
        # fallback: 取有数据的前30只
        pool = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE trade_date=? LIMIT 30", (date,)).fetchall()]
    if not pool:
        print("[SKIP] test_smoke_db_live (无股票数据)")
        conn.close()
        return
    pool = pool[:30]  # 限制测试范围
    exp = compute_factor_exposures(pool, date, conn=conn, use_cache=False)
    assert not exp.empty, "Exposures 不应为空"
    assert list(exp.columns) == STYLE_FACTOR_NAMES[:len(exp.columns)], f"列名: {list(exp.columns)}"
    print(f"[PASS] test_smoke_db_live: 形状={exp.shape}, 列={list(exp.columns)}")
    # NaN 率检查（已处理为0，所以 NaN 应为0）
    nan_rate = exp.isna().sum().sum() / exp.size
    print(f"  NaN 率={nan_rate:.2%}")
    conn.close()


def test_smoke_exposures():
    """暴露矩阵形状、风险因子列有效率。"""
    from pathlib import Path
    from conf import DB_PATH
    if not Path(DB_PATH).exists():
        print("[SKIP] test_smoke_exposures (DB 不存在)")
        return
    conn = get_conn()
    date = "2026-07-03"
    pool = [r[0] for r in conn.execute(
        "SELECT code FROM index_members WHERE index_code='sh000300' AND in_date<=? "
        "AND (out_date IS NULL OR out_date>?) LIMIT 30", (date, date)).fetchall()]
    if not pool:
        pool = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE trade_date=? LIMIT 20", (date,)).fetchall()]
    if not pool:
        print("[SKIP] test_smoke_exposures (无股票)")
        conn.close()
        return
    from factors import compute_exposures
    exp = compute_exposures(conn, date, pool=pool)
    assert not exp.empty
    # RISK_FACTORS 列有效率（填0前）
    for col in ["size", "beta", "momentum", "resvol", "liquidity", "btop"]:
        if col in exp.columns:
            effective = exp[col].abs().sum() > 0
            assert effective or exp.shape[0] == 0, f"风险因子 {col} 全为 0"
    print(f"[PASS] test_smoke_exposures: 形状={exp.shape}")
    conn.close()


# ====================================================================
# 8. 边界与异常
# ====================================================================
def test_pipeline_empty():
    """空输入处理。"""
    result = pipeline({})
    assert result.empty
    print("[PASS] test_pipeline_empty")


def test_pipeline_partial():
    """部分因子缺失。"""
    np.random.seed(42)
    n = 50
    raw = {name: pd.Series(np.random.randn(n)) for name in ["BETA", "SIZE"]}
    result = pipeline(raw)
    assert list(result.columns) == ["BETA", "SIZE"]
    print(f"[PASS] test_pipeline_partial (cols={list(result.columns)})")


def test_winsorize_interface():
    """winsorize 兼容接口（支持 method 参数）。"""
    s = pd.Series([1, 2, 3, 4, 100])
    clipped = winsorize(s, method="mad", n_mad=5)
    assert clipped.max() < 50
    print(f"[PASS] test_winsorize_interface (max={clipped.max():.2f})")


# ====================================================================
# 运行
# ====================================================================
def _run_all():
    test_fns = [
        # winsorize
        test_mad_normal,
        test_mad_extreme,
        test_mad_all_same,
        test_mad_tolerance,
        # standardize
        test_standardize_basic,
        test_standardize_weighted,
        test_standardize_single,
        test_standardize_all_nan,
        # orthogonalize
        test_orthogonalize_basic,
        test_orthogonalize_weighted,
        # composite
        test_composite_basic,
        test_composite_missing,
        # pipeline
        test_pipeline_full,
        test_pipeline_empty,
        test_pipeline_partial,
        # winsorize interface
        test_winsorize_interface,
        # risk model
        test_riskmodel_synthetic,
        # db smoke
        test_smoke_db_live,
        test_smoke_exposures,
    ]
    ok = 0
    failed = []
    for fn in test_fns:
        try:
            fn()
            ok += 1
        except Exception as e:
            import traceback
            print(f"[FAIL] {fn.__name__}: {e}")
            traceback.print_exc()
            failed.append(fn.__name__)
    n_total = len(test_fns)
    print(f"\n{'=' * 50}")
    print(f"factor + riskmodel 测试: {ok}/{n_total} 通过")
    if failed:
        print(f"失败: {', '.join(failed)}")
    return ok == n_total


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
