# -*- coding: utf-8 -*-
"""测试自适应滑点模块(slippage_adaptive.py)。
覆盖: 基础滑点、成交量调整、波动率调整、时间加权、大单拆分。
纯计算测试,无需行情数据。
"""
import os, sys, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
except Exception:
    pass

from slippage_adaptive import (
    AdaptiveSlippageModel, MarketCondition, OrderSlicer, TimeWeightedFillProbability,
    get_adaptive_slippage, calculate_filled_shares
)


# ----------------------------------------------------------------
# Test 1: ETF 基础滑点较低
# ----------------------------------------------------------------
def test_etf_baseline():
    model = AdaptiveSlippageModel()
    mc = MarketCondition(
        avg_daily_volume=1e7, avg_daily_amount=5e8,
        volatility_20d=0.18, avg_spread_pct=0.0003, current_price=3.5
    )
    s = model.estimate_slippage("sh510300", 50000, mc, "normal")
    assert s < 0.002, f"ETF small order slippage should be low, got {s:.4f}"
    print(f"[PASS] ETF baseline slippage={s:.4f} ({s:.2%})")


# ----------------------------------------------------------------
# Test 2: 大订单额外滑点
# ----------------------------------------------------------------
def test_large_order_penalty():
    model = AdaptiveSlippageModel()
    mc = MarketCondition(
        avg_daily_volume=1e6, avg_daily_amount=1e7,  # 日均 1000 万成交额
        volatility_20d=0.20, avg_spread_pct=0.001, current_price=10.0
    )
    small_s = model.estimate_slippage("sz000001", 10000, mc, "normal")
    large_s = model.estimate_slippage("sz000001", 500000, mc, "normal")  # 5% of daily amount
    assert large_s > small_s, f"large order should have more slippage: {large_s:.4f} vs {small_s:.4f}"
    print(f"[PASS] small={small_s:.4f} large={large_s:.4f}")


# ----------------------------------------------------------------
# Test 3: 高波动增加滑点
# ----------------------------------------------------------------
def test_high_vol_penalty():
    model = AdaptiveSlippageModel()
    mc_normal = MarketCondition(
        avg_daily_volume=1e7, avg_daily_amount=1e8,
        volatility_20d=0.15, avg_spread_pct=0.001, current_price=10.0
    )
    mc_high = MarketCondition(
        avg_daily_volume=1e7, avg_daily_amount=1e8,
        volatility_20d=0.45, avg_spread_pct=0.001, current_price=10.0
    )
    s_normal = model.estimate_slippage("sz000001", 50000, mc_normal, "normal")
    s_high = model.estimate_slippage("sz000001", 50000, mc_high, "normal")
    assert s_high > s_normal, f"high vol should increase slippage: {s_high:.4f} vs {s_normal:.4f}"
    print(f"[PASS] normal vol={s_normal:.4f} high vol={s_high:.4f}")


# ----------------------------------------------------------------
# Test 4: 收盘时段滑点更大
# ----------------------------------------------------------------
def test_closing_time_penalty():
    model = AdaptiveSlippageModel()
    mc = MarketCondition(
        avg_daily_volume=1e7, avg_daily_amount=1e8,
        volatility_20d=0.20, avg_spread_pct=0.001, current_price=10.0
    )
    s_norm = model.estimate_slippage("sh510300", 50000, mc, "normal")
    s_close = model.estimate_slippage("sh510300", 50000, mc, "closing")
    assert s_close > s_norm, f"closing time should have more slippage: {s_close:.4f} vs {s_norm:.4f}"
    print(f"[PASS] normal={s_norm:.4f} closing={s_close:.4f}")


# ----------------------------------------------------------------
# Test 5: 滑点上限
# ----------------------------------------------------------------
def test_slippage_capped():
    model = AdaptiveSlippageModel()
    # 极端条件
    mc = MarketCondition(
        avg_daily_volume=10000, avg_daily_amount=1e5,
        volatility_20d=1.0, avg_spread_pct=0.01, current_price=10.0
    )
    s = model.estimate_slippage("sz000001", 100000, mc, "closing")
    assert s <= 0.02, f"stock max slippage should be ≤2%, got {s:.4f}"
    print(f"[PASS] capped at {s:.4f}")


# ----------------------------------------------------------------
# Test 6: 大单拆分
# ----------------------------------------------------------------
def test_order_slicer():
    slicer = OrderSlicer()
    # 总15000股,日均10000股→参与率150%,应拆分
    assert slicer.should_slice(15000, 150000), "应触发拆分"
    slices = slicer.slice_order(15000, "sz000001", 10000, 150000, days=3)
    assert len(slices) > 0
    total = sum(s["shares"] for s in slices)
    assert total >= 15000, f"总股数应≥15000: {total}"
    print(f"[PASS] order sliced into {len(slices)} days: {[s['shares'] for s in slices]}")


def test_order_no_slice():
    slicer = OrderSlicer()
    # 100股,日均10000股→参与率1%,不拆分
    assert not slicer.should_slice(100, 150000), "小单不应拆分"
    print("[PASS] small order not sliced")


# ----------------------------------------------------------------
# Test 7: 时间加权成交概率
# ----------------------------------------------------------------
def test_time_fill_prob():
    tf = TimeWeightedFillProbability()
    p_open = tf.get_fill_probability("10:00")
    p_close = tf.get_fill_probability("14:50")
    p_end = tf.get_fill_probability("15:00")
    assert p_open > p_close, f"10:00 prob > 14:50 prob: {p_open} vs {p_close}"
    assert p_close > p_end, f"14:50 prob > 15:00 prob: {p_close} vs {p_end}"

    # ETF 成交概率高于个股
    p_etf_close = tf.get_fill_probability("14:50", is_etf=True)
    p_stock_close = tf.get_fill_probability("14:50", is_etf=False)
    assert p_etf_close > p_stock_close, f"ETF prob > stock prob: {p_etf_close} vs {p_stock_close}"
    print(f"[PASS] 10:00={p_open:.2f} 14:50={p_close:.2f} 15:00={p_end:.2f} ETF={p_etf_close:.2f}")


# ----------------------------------------------------------------
# Test 8: 成交量截断
# ----------------------------------------------------------------
def test_filled_shares():
    # 每天上限=日均*2%, 1000股, 日均50000→上限1000, 全部成交
    filled = calculate_filled_shares(1000, "sz000001", 50000, days=1)
    assert filled == 1000, f"小单应全部成交: {filled}"
    print(f"[PASS] small shares filled={filled}")

    # 50000股, 日均10000→单日上限=200, 3天=600
    filled = calculate_filled_shares(50000, "sz000001", 10000, days=3)
    expected_max = int(10000 * 0.02 * 3)  # 600 股
    assert filled == expected_max, \
        f"大单应截断至{expected_max},实际: {filled}"
    print(f"[PASS] large shares capped: {filled}")


# ================================================================
def _run_all():
    fns = [
        test_etf_baseline,
        test_large_order_penalty,
        test_high_vol_penalty,
        test_closing_time_penalty,
        test_slippage_capped,
        test_order_slicer,
        test_order_no_slice,
        test_time_fill_prob,
        test_filled_shares,
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
    print(f"\n滑点模型测试: {ok}/{len(fns)} 通过")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
