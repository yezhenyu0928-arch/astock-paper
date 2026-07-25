# -*- coding: utf-8 -*-
"""风控验收(SPEC 模块4 完整版)。合成 states + MockCtx,免联网。
覆盖:回撤熔断(清仓+重置)/大盘冻结/单票上限(个股削,ETF豁免)/流动性删单/止损。
可直接运行:python tests/test_risk.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import risk
from models import Account, Position


class MockCtx:
    def __init__(self, date="2026-07-03", market=None, closes=None, amounts=None):
        self.date = date
        self._market = market or [10.0] * 21          # sh510300 近21日收盘
        self._closes = closes or {}
        self._amounts = amounts or {}

    def close(self, code, n):
        if code == risk.MARKET_PROXY:
            return self._market[-n:]
        return self._closes.get(code, [10.0] * n)[-n:]

    def raw_close(self, code):
        return self._closes.get(code, [10.0])[-1]

    def avg_amount(self, code, n):
        return self._amounts.get(code, 1e9)


CFG = {"risk": {"strategy_max_drawdown": 0.15, "max_position_pct": 0.15,
                "stop_loss": {"trend": 0.08, "rotation": 0.12},
                "market_freeze": {"day_drop": 0.03, "m20_drop": 0.10}, "min_avg_amount": 50000000},
       "news_layer": {"enabled": False}}


def _state(sid, nav=1.0, peak=1.0, positions=None, frozen=False, cash=100000):
    acct = Account(strategy_id=sid, init_capital=100000, cash=cash,
                   positions=positions or {}, frozen=frozen, nav=nav)
    return {sid: {"account": acct, "highest_nav": peak, "nav_history": []}}


def test_drawdown_circuit_breaker():
    st = _state("s3_ma_trend@v1", nav=0.83, peak=1.0,
                positions={"sz000099": Position("sz000099", 1000, 10.0, "2026-06-01")})
    pre = risk.pre_check("2026-07-03", MockCtx(), st, CFG)
    acct = st["s3_ma_trend@v1"]["account"]
    assert acct.frozen is True, "回撤17%应熔断"
    assert any(o.side == "sell" and o.weight == 0 for o in pre["forced_orders"]), "应生成清仓单"
    assert any("熔断" in a for a in pre["alerts"])
    # 再次 pre_check(已清仓)→重置峰值+解冻
    acct.positions.clear(); acct.nav = 0.83
    pre2 = risk.pre_check("2026-07-06", MockCtx(), st, CFG)
    assert acct.frozen is False and st["s3_ma_trend@v1"]["highest_nav"] == 0.83, "应重置并解冻"
    print("[PASS] test_drawdown_circuit_breaker (熔断+清仓+重置参赛)")


def test_market_freeze():
    mkt = [10.0] * 20 + [9.6]                          # 今日 -4% > 3%
    pre = risk.pre_check("2026-07-03", MockCtx(market=mkt), _state("s2_etf@v1"), CFG)
    assert pre["market_frozen"] is True
    print("[PASS] test_market_freeze")


def test_post_market_frozen_drops_buys():
    from models import Order
    st = _state("s2_etf@v1")
    orders = [Order("s2_etf@v1", "sh510500", "buy", 0.98, "x", "2026-07-03")]
    kept = risk.post_check("2026-07-03", MockCtx(), orders, st, CFG, market_frozen=True)
    assert kept == [], "大盘冻结应删所有买单"
    print("[PASS] test_post_market_frozen_drops_buys")


def test_position_cap_stock_vs_etf():
    from models import Order
    st = _state("s4_smallcap@v1")
    ctx = MockCtx(closes={"sz000099": [10.0], "sh510500": [6.0]})
    # 个股 buy weight 0.5 → 削到 15%
    o_stock = Order("s4_smallcap@v1", "sz000099", "buy", 0.5, "x", "2026-07-03")
    kept = risk.post_check("2026-07-03", ctx, [o_stock], st, CFG)
    assert abs(kept[0].weight - 0.15) < 1e-6, f"个股应削到15%,实际{kept[0].weight}"
    # ETF buy weight 0.98 → 不削(豁免)
    st2 = _state("s2_etf@v1")
    o_etf = Order("s2_etf@v1", "sh510500", "buy", 0.98, "x", "2026-07-03")
    kept2 = risk.post_check("2026-07-03", ctx, [o_etf], st2, CFG)
    assert abs(kept2[0].weight - 0.98) < 1e-6, f"ETF不应削,实际{kept2[0].weight}"
    print("[PASS] test_position_cap_stock_vs_etf")


def test_liquidity_drop():
    from models import Order
    st = _state("s4_smallcap@v1")
    ctx = MockCtx(amounts={"sz000099": 1e7})           # 日均1千万 < 5千万门槛
    o = Order("s4_smallcap@v1", "sz000099", "buy", 0.05, "x", "2026-07-03")
    kept = risk.post_check("2026-07-03", ctx, [o], st, CFG)
    assert kept == [], "低流动性个股买单应删"
    print("[PASS] test_liquidity_drop")


def test_stop_loss():
    st = _state("s3_ma_trend@v1",
                positions={"sz000099": Position("sz000099", 1000, 10.0, "2026-06-01")})
    ctx = MockCtx(closes={"sz000099": [9.0]})          # 现价9,浮亏10% > trend 8%
    kept = risk.post_check("2026-07-03", ctx, [], st, CFG)
    assert any(o.side == "sell" and o.weight == 0 and "止损" in o.reason for o in kept), kept
    print("[PASS] test_stop_loss")


def _run_all():
    fns = [test_drawdown_circuit_breaker, test_market_freeze, test_post_market_frozen_drops_buys,
           test_position_cap_stock_vs_etf, test_liquidity_drop, test_stop_loss]
    ok = 0
    for fn in fns:
        try:
            fn(); ok += 1
        except Exception as e:
            import traceback
            print(f"[FAIL] {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\nrisk 测试: {ok}/{len(fns)} 通过")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
