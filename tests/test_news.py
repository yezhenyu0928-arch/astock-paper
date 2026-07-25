# -*- coding: utf-8 -*-
"""消息面验收(SPEC_NEWS N7)。合成 news_raw,免联网。临时库隔离。
覆盖:去重入库/市场分/黑天鹅持仓强卖/市场分-2→敞口0(买单全拦)。
可直接运行:python tests/test_news.py"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import news_adapter as na
import news_engine as ne
from models import Account, Position, Order
import pandas as pd

TODAY = "2026-07-03"


def _conn():
    p = Path(tempfile.mkdtemp(prefix="news_")) / "n.sqlite"
    c = db.get_conn(p)
    db.init_db(c)
    na.ensure()
    # ensure 用默认库,这里手动在临时库建表
    c.executescript(na.NEWS_DDL)
    c.commit()
    return c


class Ctx:
    def __init__(self, conn):
        self.conn = conn


def test_dedup():
    c = _conn()
    df = pd.DataFrame([{"ts": TODAY + " 09:30", "title": "央行降准0.5个百分点", "content": "", "source": "cls"}])
    assert na.store_news(df, conn=c) == 1
    assert na.store_news(df, conn=c) == 0        # 重复不入
    n = c.execute("SELECT count(*) FROM news_raw").fetchone()[0]
    assert n == 1
    print("[PASS] test_dedup")


def test_market_score():
    c = _conn()
    rows = [{"ts": TODAY, "title": "某地区地缘冲突升级,市场恐慌", "content": "", "source": "cls"},
            {"ts": TODAY, "title": "央行宣布降准", "content": "", "source": "cls"}]
    na.store_news(pd.DataFrame(rows), conn=c)
    score, ev = ne.scan_market(TODAY, conn=c)
    assert score == -1, f"地缘冲突(-2)+降准(+1)=-1,实际{score}"   # clip 后
    assert len(ev) >= 2
    print(f"[PASS] test_market_score score={score}")


def test_blackswan_sell():
    c = _conn()
    # 持仓个股新闻含"立案调查"→黑天鹅
    df = pd.DataFrame([{"ts": TODAY, "title": "XX公司被立案调查", "content": "证监会立案调查",
                        "source": "em", "code": "sz000099"}])
    na.store_news(df, conn=c)
    # 直接喂 scan_holdings 用的 fetch_stock_news 走网络,改为构造:直接测 blackswan 逻辑
    acct = Account("s3_ma_trend@v1", 100000, 50000,
                   positions={"sz000099": Position("sz000099", 1000, 10, "2026-06-01")})
    # monkeypatch fetch_stock_news 返回本地构造
    na.fetch_stock_news = lambda code, days=3: df if code == "sz000099" else pd.DataFrame()
    sells, warns = ne.blackswan_sells(TODAY, {"s3_ma_trend@v1": acct},
                                      {"news_layer": {"enabled": True}}, conn=c)
    assert any(o.side == "sell" and o.weight == 0 and "黑天鹅" in o.reason for o in sells), sells
    print("[PASS] test_blackswan_sell")


def test_exposure_mult_freeze():
    c = _conn()
    na.store_signal(TODAY, "market", -2, "L0", ["地缘冲突升级"], conn=c)
    cfg = {"news_layer": {"enabled": True, "exposure_map": {-2: 0.0, -1: 0.5, 0: 1.0, 1: 1.0, 2: 1.0}}}
    mult = ne.market_exposure_mult(TODAY, Ctx(c), cfg)
    assert mult == 0.0, f"市场分-2应敞口0,实际{mult}"
    # 接入 risk:buy 单应被 exposure=0 拦掉
    import risk
    st = {"s2_etf@v1": {"account": Account("s2_etf@v1", 100000, 100000), "highest_nav": 1.0}}
    o = Order("s2_etf@v1", "sh510500", "buy", 0.98, "x", TODAY)

    class Ctx2(Ctx):
        def raw_close(self, code): return 6.0
        def avg_amount(self, code, n): return 1e9
    kept = risk.post_check(TODAY, Ctx2(c), [o], st, {**cfg,
              "risk": {"max_position_pct": 0.15, "min_avg_amount": 5e7,
                       "stop_loss": {"trend": 0.08, "rotation": 0.12}}})
    assert kept == [], f"市场分-2应拦掉买单,实际{kept}"
    print("[PASS] test_exposure_mult_freeze (市场分-2→买单全拦)")


def _run_all():
    fns = [test_dedup, test_market_score, test_blackswan_sell, test_exposure_mult_freeze]
    ok = 0
    for fn in fns:
        try:
            fn(); ok += 1
        except Exception as e:
            import traceback
            print(f"[FAIL] {fn.__name__}: {e}"); traceback.print_exc()
    print(f"\n消息面测试: {ok}/{len(fns)} 通过")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
