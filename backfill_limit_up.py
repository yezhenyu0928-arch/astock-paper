# -*- coding: utf-8 -*-
"""预计算 limit_up_count 表: 每只票每个交易日 近20日/近60日 涨停次数。
涨停判定(相对前收): 主板>=9.5%, 创业板/科创板>=19.5%, 北交>=29.5%。
预计算后 mf_core 直接查表, 回测秒级响应, 无需每调仓重算 800万行。
"""
import db
import time
from collections import deque

W20, W60 = 20, 60


def _thr(code):
    if code.startswith(("sz300", "sz301", "sh688")):
        return 0.195
    if code.startswith("bj"):
        return 0.295
    return 0.095


def main():
    conn = db.get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS limit_up_count(
            code TEXT, trade_date TEXT, lu20 INTEGER, lu60 INTEGER,
            PRIMARY KEY(code, trade_date))"""
    )
    cur = conn.execute(
        "SELECT code, trade_date, close FROM daily_bar WHERE close>0 ORDER BY code, trade_date"
    )
    prev_close = {}
    hist = {}
    out = []
    batch = 0
    t0 = time.time()
    for code, td, close in cur:
        pc = prev_close.get(code)
        is_lu = 0
        if pc and pc > 0:
            pct = close / pc - 1
            if pct >= _thr(code):
                is_lu = 1
        h = hist.setdefault(code, deque())
        h.append(is_lu)
        if len(h) > W60:
            h.popleft()
        s20 = sum(list(h)[-W20:]) if len(h) > W20 else sum(h)
        s60 = sum(h)
        out.append((code, td, s20, s60))
        prev_close[code] = close
        batch += 1
        if batch >= 500000:
            conn.executemany(
                "INSERT OR REPLACE INTO limit_up_count VALUES(?,?,?,?)", out
            )
            conn.commit()
            out = []
            batch = 0
    if out:
        conn.executemany(
            "INSERT OR REPLACE INTO limit_up_count VALUES(?,?,?,?)", out
        )
        conn.commit()
    print("limit_up_count DONE rows committed, cost %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
