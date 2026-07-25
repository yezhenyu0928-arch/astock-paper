# -*- coding: utf-8 -*-
"""M1 验收测试(SPEC 模块1)。可直接运行:python tests/test_m1.py
覆盖:①双源日线取数+列名/单位 ②交易日历判断 ③删库某日后 check 能 FAIL ④adj_factor。
需联网(真实数据源)。"""
import os
import sys
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

import db
import data
import data_adapter as da
import trade_calendar as cal


def setup_module(_=None):
    db.init_db()
    data.update_calendar()


def test_calendar_judgement():
    """交易日历:周末非交易日、工作日交易日、前后交易日、周/月末边界。"""
    assert cal.is_trade_day("2026-06-06") is False        # 周六
    assert cal.is_trade_day("2026-06-08") is True         # 周一
    assert cal.prev_trade_day("2026-06-08") == "2026-06-05"
    assert cal.next_trade_day("2026-06-08") == "2026-06-09"
    assert cal.last_trade_day_of_week("2026-06-05") is True    # 周五
    assert cal.last_trade_day_of_week("2026-06-08") is False   # 周一
    assert cal.last_trade_day_of_month("2026-06-30") is True
    print("[PASS] test_calendar_judgement")


def test_fetch_daily_etf_sina():
    """ETF 走 Sina 全历史:列齐、volume 单位=股(量×价≈额)。"""
    df = da.fetch_daily("sh510300", "2018-01-01", "2026-07-03")
    need = {"code", "trade_date", "open", "high", "low", "close", "volume",
            "amount", "adj_factor", "is_suspended", "limit_up", "limit_down", "source"}
    assert need.issubset(set(df.columns)), df.columns
    assert len(df) > 1500, f"ETF 历史过短:{len(df)}"       # 2018→今应>1500个交易日
    row = df.iloc[-1]
    ratio = row["volume"] * row["close"] / row["amount"]
    assert 0.9 < ratio < 1.1, f"量价单位异常 ratio={ratio}"  # 单位=股才≈1
    print(f"[PASS] test_fetch_daily_etf_sina rows={len(df)} source={row['source']} ratio={ratio:.3f}")


def test_fetch_daily_stock_baostock():
    """个股走 baostock:能取数、老股 adj_factor 明显>1(后复权累积)。"""
    df = da.fetch_daily("sz000001", "2026-06-01", "2026-06-30")
    assert not df.empty and len(df) >= 15
    assert df["adj_factor"].iloc[-1] > 1.0            # 平安银行1991上市,累计复权因子远大于1
    print(f"[PASS] test_fetch_daily_stock_baostock source={df['source'].iloc[-1]} adj={df['adj_factor'].iloc[-1]:.1f}")


def test_check_fail_on_missing():
    """删库中某日 → check 抛 DataCheckError;复原。"""
    code = "sh510300"
    conn = db.get_conn()
    df = da.fetch_daily(code, "2026-06-20", "2026-07-03")
    da.upsert(df, "daily_bar", conn=conn); conn.commit()
    last = conn.execute("SELECT max(trade_date) FROM daily_bar WHERE code=?", (code,)).fetchone()[0]
    # 正常应通过(仅可交易标的)
    data.check(last, tradable_codes=[code], index_codes=[], conn=conn)
    # 删该日
    conn.execute("DELETE FROM daily_bar WHERE code=? AND trade_date=?", (code, last)); conn.commit()
    failed = False
    try:
        data.check(last, tradable_codes=[code], index_codes=[], conn=conn)
    except data.DataCheckError:
        failed = True
    # 复原
    da.upsert(da.fetch_daily(code, last, last), "daily_bar", conn=conn); conn.commit(); conn.close()
    assert failed, "删数据后 check 未能 FAIL"
    print("[PASS] test_check_fail_on_missing")


def _run_all():
    setup_module()
    fns = [test_calendar_judgement, test_fetch_daily_etf_sina,
           test_fetch_daily_stock_baostock, test_check_fail_on_missing]
    ok = 0
    for fn in fns:
        try:
            fn(); ok += 1
        except Exception as e:
            print(f"[FAIL] {fn.__name__}: {type(e).__name__}: {e}")
    da.bs_logout()
    print(f"\nM1 测试: {ok}/{len(fns)} 通过")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
