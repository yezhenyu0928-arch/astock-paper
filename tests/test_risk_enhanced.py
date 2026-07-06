# -*- coding: utf-8 -*-
"""测试增强风控模块(risk_enhanced.py)。
覆盖: 阶梯熔断触发/冷却、相关性计算、波动率自适应。
纯计算测试,无需数据库或行情数据。
"""
import os, sys, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
except Exception:
    pass

import numpy as np
import risk_enhanced as re
from risk_enhanced import LadderRiskManager, CorrelationRiskManager, VolatilityAdaptiveRisk


# ----------------------------------------------------------------
# Test 1: 阶梯熔断——5%回撤不触发
# ----------------------------------------------------------------
def test_ladder_no_trigger():
    mgr = LadderRiskManager()
    orders, alerts = mgr.check_drawdown("s1_test", 95, 100, "2024-01-15")
    assert len(orders) == 0, f"5%回撤不应触发: {orders}"
    assert len(alerts) == 0
    print("[PASS] 5% drawdown → no trigger")


# ----------------------------------------------------------------
# Test 2: 阶梯熔断——10%触发减仓50%
# ----------------------------------------------------------------
def test_ladder_reduce():
    mgr = LadderRiskManager()
    orders, alerts = mgr.check_drawdown("s1_test", 89.5, 100, "2024-01-15")
    assert len(orders) > 0, "10%回撤应触发减仓"
    assert any("减仓" in a or "reduce" in str(o).lower() for a in alerts for o in orders), \
        f"10%应触发减仓动作: alerts={alerts}"
    assert mgr.cutoff_state["s1_test"]["last_level"] >= 0
    print(f"[PASS] 10.5% drawdown → reduce alerts={alerts}")
    mgr.reset_state()


# ----------------------------------------------------------------
# Test 3: 阶梯熔断——15%触发全清仓
# ----------------------------------------------------------------
def test_ladder_clear():
    mgr = LadderRiskManager()
    orders, alerts = mgr.check_drawdown("s1_test", 84, 100, "2024-01-15")
    assert len(orders) > 0, "15%回撤应触发清仓"
    assert any("清仓" in a or "clear" in str(o).lower() for a in alerts for o in orders)
    print(f"[PASS] 16% drawdown → clear alerts={alerts}")
    mgr.reset_state()


# ----------------------------------------------------------------
# Test 4: 阶梯熔断——冷却期不再重复触发
# ----------------------------------------------------------------
def test_ladder_cooldown():
    mgr = LadderRiskManager()
    # 第一次触发
    mgr.check_drawdown("s1_test", 89, 100, "2024-01-15")
    level_after = mgr.cutoff_state["s1_test"]["last_level"]
    cooldown_until = mgr.cutoff_state["s1_test"]["cooldown_until"]

    # 同一天再次调用→冷却期内,无新订单
    orders, alerts = mgr.check_drawdown("s1_test", 85, 100, cooldown_until)
    assert len(orders) == 0, f"冷却期内不应重复触发: {orders}"
    print(f"[PASS] cooldown until={cooldown_until}")
    mgr.reset_state()


# ----------------------------------------------------------------
# Test 5: 阶梯熔断——逐级升级(先减仓后清仓)
# ----------------------------------------------------------------
def test_ladder_escalation():
    mgr = LadderRiskManager()
    # 第1天: 10%触发减仓(实际回撤10.5%)
    orders1, alerts1 = mgr.check_drawdown("s1_test", 89.5, 100, "2024-01-15")
    assert len(orders1) > 0, f"10%回撤应触发: orders={orders1}"
    # 模拟经过冷却期
    mgr.reset_state("s1_test")
    # 第2天: 15%触发清仓(更高级别)
    orders2, alerts2 = mgr.check_drawdown("s1_test", 84, 100, "2024-01-16")
    assert len(orders2) > 0, "15%回撤应触发"
    # 验证两个动作不同级别
    assert mgr.cutoff_state.get("s1_test", {}).get("last_level", -1) >= 1, \
        "last_level应升级"
    print(f"[PASS] escalation: alerts1={alerts1}, alerts2={alerts2}")
    mgr.reset_state()


# ----------------------------------------------------------------
# Test 6: 策略相关性矩阵
# ----------------------------------------------------------------
def test_correlation_matrix():
    mgr = CorrelationRiskManager()
    # 构造两条高度正相关的净值序列
    s1 = [100.0, 101.0, 102.5, 103.0, 101.5, 104.0, 105.0,
          106.0, 105.5, 107.0, 108.0, 109.0, 108.5, 110.0, 111.0]
    s2 = [100.0, 101.2, 102.8, 103.2, 101.8, 104.3, 105.2,
          106.3, 105.8, 107.2, 108.3, 109.2, 108.8, 110.3, 111.2]
    # 构造一条反向的
    s3 = [100.0, 99.0, 98.0, 97.5, 98.5, 97.0, 96.0,
          95.0, 95.5, 94.0, 93.0, 92.0, 92.5, 91.0, 90.0]

    returns = mgr.calculate_strategy_returns({"s1": s1, "s2": s2, "s3": s3})
    assert len(returns) >= 2

    corr = mgr.compute_correlation_matrix(returns)
    assert ("s1", "s2") in corr or ("s2", "s1") in corr
    s1_s2_corr = corr.get(("s1", "s2")) or corr.get(("s2", "s1"))
    assert s1_s2_corr is not None
    assert s1_s2_corr > 0.9, f"s1-s2应高度正相关,实际: {s1_s2_corr:.3f}"
    print(f"[PASS] s1-s2 correlation={s1_s2_corr:.3f}")

    alerts = mgr.check_correlation_risk(returns)
    assert len(alerts) > 0, "高相关应触发告警"
    print(f"[PASS] correlation alerts: {len(alerts)}")


# ----------------------------------------------------------------
# Test 7: 波动率自适应——高波动收紧
# ----------------------------------------------------------------
def test_volatility_adaptive_tighten():
    risk = VolatilityAdaptiveRisk()
    # 高波动 40%
    adjusted = risk.adjust_thresholds(0.40)
    base_thresholds = risk.base_thresholds
    for key in base_thresholds:
        if key == "strategy_max_drawdown":
            assert adjusted[key] < base_thresholds[key], \
                f"高波动时{key}应收紧: {adjusted[key]} vs {base_thresholds[key]}"
    print(f"[PASS] high vol thresholds: {adjusted}")


def test_volatility_adaptive_loosen():
    risk = VolatilityAdaptiveRisk()
    # 低波动 10%
    adjusted = risk.adjust_thresholds(0.10)
    base_thresholds = risk.base_thresholds
    for key in base_thresholds:
        if key == "strategy_max_drawdown":
            assert adjusted[key] > base_thresholds[key], \
                f"低波动时{key}应放宽: {adjusted[key]} vs {base_thresholds[key]}"
    print(f"[PASS] low vol thresholds: {adjusted}")


# ----------------------------------------------------------------
# Test 8: 波动率计算
# ----------------------------------------------------------------
def test_market_volatility():
    risk = VolatilityAdaptiveRisk()
    # 构造稳定序列→低波动
    stable = [1.0 + i * 0.001 for i in range(50)]  # 日涨0.1%
    vol = risk.calculate_market_volatility(stable, window=20)
    assert 0 < vol < 0.10, f"稳定序列波动应<10%: {vol:.1%}"
    print(f"[PASS] stable vol={vol:.1%}")


# ================================================================
def _run_all():
    fns = [
        test_ladder_no_trigger,
        test_ladder_reduce,
        test_ladder_clear,
        test_ladder_cooldown,
        test_ladder_escalation,
        test_correlation_matrix,
        test_volatility_adaptive_tighten,
        test_volatility_adaptive_loosen,
        test_market_volatility,
    ]
    ok = 0
    for fn in fns:
        try:
            fn()
            ok += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[FAIL] {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n风险增强测试: {ok}/{len(fns)} 通过")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
