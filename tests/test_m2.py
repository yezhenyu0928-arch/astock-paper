# -*- coding: utf-8 -*-
"""M2 引擎验收(SPEC 模块2 + SPEC_FILL F1/F2)。合成数据注入临时库,确定性、免联网。
可直接运行:python tests/test_m2.py
覆盖:最低佣金5元/T+1拦截/涨停买单cancelled/跌停卖单deferred次日成交/现金红利入账/
     同日settle幂等/滑点p=1%=base+20bp/部分成交cut/最低单笔跳过/单手门槛剔除。"""
import os
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import conf
import engine as E
import trade_calendar as cal
from models import Order, Position

D0, D1, D2, D3 = "2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01"

CFG = {
    "user": {"capital": 100000},
    "costs": {"commission_rate": 0.00025, "commission_min": 5, "stamp_tax_sell": 0.0005,
              "slippage": {"etf": 0.0005, "stock": 0.0015}},
    "risk": {"strategy_max_drawdown": 0.15, "max_position_pct": 0.15,
             "stop_loss": {"trend": 0.08, "rotation": 0.12},
             "market_freeze": {"day_drop": 0.03, "m20_drop": 0.10}, "min_avg_amount": 50000000},
    "custom": {"min_order_amount": 5000, "min_ticket": 8000,
               "open_frac": {"stock": 0.08, "etf": 0.10},
               "impact_k": {"stock": 0.0020, "etf": 0.0008}, "risk_override": {}},
    "strategies": {"test@v1": True},
    "news_layer": {"enabled": False},
}
_tmp = None


def setup():
    global _tmp
    cal._ensure()                       # 用真实库日历
    _tmp = Path(tempfile.mkdtemp(prefix="m2_"))
    conf.STATE_DIR = _tmp
    E.conf.STATE_DIR = _tmp
    E.TRADE_LOG = _tmp / "trade_log.csv"


def teardown():
    if _tmp and _tmp.exists():
        shutil.rmtree(_tmp, ignore_errors=True)


def new_engine():
    """每个用例独立子目录:隔离 state/*.json 与 trade_log.csv,防止状态泄漏。"""
    sub = _tmp / os.urandom(4).hex()
    sub.mkdir()
    conf.STATE_DIR = sub
    E.conf.STATE_DIR = sub
    E.TRADE_LOG = sub / "trade_log.csv"
    conn = db.get_conn(sub / "db.sqlite")
    db.init_db(conn)
    e = E.Engine(config=dict(CFG), registry={}, conn=conn)
    e._trade_log_keys = set()
    return e, conn


def bar(conn, code, date, o, h, l, c, amount, lu, ld, susp=0):
    conn.execute(
        "INSERT OR REPLACE INTO daily_bar (code,trade_date,open,high,low,close,volume,amount,"
        "adj_factor,is_suspended,limit_up,limit_down,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, date, o, h, l, c, amount / c if c else 0, amount, 1.0, susp, lu, ld, "test"))
    conn.commit()


def pending(e, code, side, weight, sig=D1):
    o = Order(strategy_id="test@v1", code=code, side=side, weight=weight, reason="t", signal_date=sig)
    d = o.to_dict(); d["_defer"] = 0
    e.state["test@v1"]["pending"].append(d)


def approx(a, b, tol=0.02):
    return abs(a - b) <= tol


# ---------------- 用例 ----------------
def test_buy_min_commission():
    e, conn = new_engine()
    bar(conn, "sh510099", D2, 2.00, 2.05, 1.99, 2.02, 1e9, 2.20, 1.80)
    e.load_account("test@v1")
    pending(e, "sh510099", "buy", 0.1)
    rep = e.settle(D2)
    acct = e.load_account("test@v1")
    assert len(rep) == 1 and rep[0]["status"] == "filled", rep
    assert rep[0]["shares"] == 5000, rep[0]["shares"]
    assert approx(rep[0]["sim_price"], 2.0), rep[0]["sim_price"]
    assert rep[0]["fee"] == 5.0, rep[0]["fee"]          # 最低佣金5元
    assert "sh510099" in acct.positions
    print("[PASS] test_buy_min_commission shares=5000 fee=5")


def test_t1_block():
    e, conn = new_engine()
    bar(conn, "sz000099", D2, 10.0, 10.2, 9.9, 10.1, 5e8, 11.0, 9.0)
    acct = e.load_account("test@v1")
    acct.positions["sz000099"] = Position(code="sz000099", shares=1000, avg_cost=9.0, buy_date=D2)
    pending(e, "sz000099", "sell", 0.0)
    rep = e.settle(D2)
    assert rep == [], rep                                # T+1:当日买入不可卖
    assert e.load_account("test@v1").positions["sz000099"].shares == 1000
    assert len(e.state["test@v1"]["pending"]) == 1       # 仍挂起
    print("[PASS] test_t1_block")


def test_limit_up_buy_cancelled():
    e, conn = new_engine()
    bar(conn, "sh510099", D2, 2.20, 2.20, 2.20, 2.20, 1e9, 2.20, 1.80)  # 一字涨停
    e.load_account("test@v1")
    pending(e, "sh510099", "buy", 0.1)
    e.settle(D2)
    acct = e.load_account("test@v1")
    assert "sh510099" not in acct.positions
    # 校验 trade_log 记为 cancelled
    import csv
    rows = list(csv.DictReader(open(E.TRADE_LOG, encoding="utf-8")))
    assert any(r["status"] == "cancelled" for r in rows), rows
    print("[PASS] test_limit_up_buy_cancelled")


def test_limit_down_sell_defer_then_fill():
    e, conn = new_engine()
    acct = e.load_account("test@v1")
    acct.positions["sz000099"] = Position(code="sz000099", shares=1000, avg_cost=9.0, buy_date=D0)
    pending(e, "sz000099", "sell", 0.0)
    bar(conn, "sz000099", D2, 8.00, 8.00, 8.00, 8.00, 5e8, 9.6, 8.00)   # 一字跌停
    rep = e.settle(D2)
    assert rep == [] and len(e.state["test@v1"]["pending"]) == 1, "跌停应顺延"
    bar(conn, "sz000099", D3, 9.00, 9.2, 8.9, 9.1, 5e8, 9.9, 8.1)       # 次日正常
    rep = e.settle(D3)
    assert len(rep) == 1 and rep[0]["status"] == "filled", rep
    assert "sz000099" not in e.load_account("test@v1").positions
    print("[PASS] test_limit_down_sell_defer_then_fill")


def test_dividend_cash():
    e, conn = new_engine()
    conn.execute("INSERT OR REPLACE INTO dividend (code,ex_date,cash_per_share,shares_ratio) VALUES (?,?,?,?)",
                 ("sz000099", D2, 0.2, 0.0)); conn.commit()   # 10派2元→0.2/股
    bar(conn, "sz000099", D2, 10.0, 10.2, 9.9, 10.0, 5e8, 11.0, 9.0)
    acct = e.load_account("test@v1")
    acct.positions["sz000099"] = Position(code="sz000099", shares=1000, avg_cost=10.0, buy_date=D0)
    cash0 = acct.cash
    e.settle(D2)
    acct = e.load_account("test@v1")
    assert approx(acct.cash - cash0, 200.0, 0.01), acct.cash - cash0   # +1000×0.2
    # 再settle一次:幂等,不重复入账
    e.settle(D2)
    assert approx(e.load_account("test@v1").cash - cash0, 200.0, 0.01), "红利重复入账!"
    print("[PASS] test_dividend_cash +200 幂等")


def test_settle_idempotent():
    e, conn = new_engine()
    bar(conn, "sh510099", D2, 2.00, 2.05, 1.99, 2.02, 1e9, 2.20, 1.80)
    e.load_account("test@v1")
    pending(e, "sh510099", "buy", 0.1)
    rep1 = e.settle(D2)
    a1 = e.load_account("test@v1")
    snap = (round(a1.cash, 2), a1.positions["sh510099"].shares, round(a1.nav, 6))
    rep2 = e.settle(D2)                                   # 再跑
    a2 = e.load_account("test@v1")
    snap2 = (round(a2.cash, 2), a2.positions["sh510099"].shares, round(a2.nav, 6))
    assert rep2 == [] and snap == snap2, (snap, snap2)
    print("[PASS] test_settle_idempotent")


def test_slippage_formula():
    e, _ = new_engine()
    # 个股 p=1%: slip = base(0.0015) + impact_k(0.0020)*sqrt(1) = 0.0035(额外20bp)
    assert approx(e._slippage(0.01, False), 0.0035, 1e-6), e._slippage(0.01, False)
    assert approx(e._slippage(0.001, False), 0.0015, 1e-6)   # p<=0.5% → base
    # ETF p=1%: base(0.0005)+impact_k(0.0008) = 0.0013
    assert approx(e._slippage(0.01, True), 0.0013, 1e-6)
    print("[PASS] test_slippage_formula (个股 p=1% 额外20bp)")


def test_partial_cut_liquidity():
    e, conn = new_engine()
    bar(conn, "sh510099", D2, 2.00, 2.05, 1.99, 2.02, 1e5, 2.20, 1.80)  # amount 很小→p>2%
    e.load_account("test@v1")
    pending(e, "sh510099", "buy", 0.1)
    rep = e.settle(D2)
    assert len(rep) == 1 and rep[0]["status"] == "cut_liquidity", rep
    assert "流动性截断" in rep[0]["reason"], rep[0]["reason"]
    print("[PASS] test_partial_cut_liquidity")


def test_min_order_skip():
    e, conn = new_engine()
    bar(conn, "sh510099", D2, 2.00, 2.05, 1.99, 2.02, 1e9, 2.20, 1.80)
    e.load_account("test@v1")
    pending(e, "sh510099", "buy", 0.01)                  # 目标1000元 < 5000
    rep = e.settle(D2)
    assert rep == [], rep
    import csv
    rows = list(csv.DictReader(open(E.TRADE_LOG, encoding="utf-8")))
    assert any("最低单笔" in r["reason"] for r in rows), rows
    print("[PASS] test_min_order_skip")


def test_single_lot_threshold():
    e, conn = new_engine()
    bar(conn, "sz000088", D2, 200.0, 202.0, 198.0, 201.0, 5e8, 220.0, 180.0)  # 高价股
    e.load_account("test@v1")
    pending(e, "sz000088", "buy", 0.1)                   # 单手2万>总资产15%(1.5万)
    rep = e.settle(D2)
    assert rep == [], rep
    import csv
    rows = list(csv.DictReader(open(E.TRADE_LOG, encoding="utf-8")))
    assert any("单手" in r["reason"] for r in rows), rows
    print("[PASS] test_single_lot_threshold")


def _run_all():
    setup()
    fns = [test_buy_min_commission, test_t1_block, test_limit_up_buy_cancelled,
           test_limit_down_sell_defer_then_fill, test_dividend_cash, test_settle_idempotent,
           test_slippage_formula, test_partial_cut_liquidity, test_min_order_skip,
           test_single_lot_threshold]
    ok = 0
    for fn in fns:
        try:
            fn(); ok += 1
        except Exception as ex:
            import traceback
            print(f"[FAIL] {fn.__name__}: {type(ex).__name__}: {ex}")
            traceback.print_exc()
    teardown()
    print(f"\nM2 测试: {ok}/{len(fns)} 通过")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
