# -*- coding: utf-8 -*-
"""基本面补数(SPEC 模块8/P7)。fundamental 表:code,trade_date,pe,pb,market_cap,dividend_yield。
- 个股 PE/PB/流通市值:baostock(经 data_adapter)。
- 股息率:近12月每股现金分红 / 当日收盘(用 dividend 表 + daily_bar,无需市值)。
- 指数PE分位(S5):指数滚动PE(乐咕乐股)存入 fundamental(code=指数码),按10年分位。
⚠ 历史深度/幸存者偏差:S1/S4/S5 回测起点相应后移,报告须注明。
"""
import logging
import pandas as pd

import util
import data_adapter as da
from db import get_conn, ensure_table

log = logging.getLogger("fundamental")

FUND_DDL = """
CREATE TABLE IF NOT EXISTS fundamental (
  code TEXT NOT NULL, trade_date TEXT NOT NULL,
  pe REAL, pb REAL, market_cap REAL, dividend_yield REAL,
  PRIMARY KEY(code, trade_date));
CREATE INDEX IF NOT EXISTS idx_fund_date ON fundamental(trade_date);
"""

INDEX_PE_NAME = {"sh000300": "沪深300", "sh000905": "中证500", "sh000906": "中证800"}


def ensure():
    ensure_table(FUND_DDL)


def _div_yield_map(conn, code):
    """{trade_date: 近12月每股现金分红}——按除权日累加,前向填充到每个交易日。"""
    divs = conn.execute("SELECT ex_date, cash_per_share FROM dividend WHERE code=? ORDER BY ex_date",
                        (code,)).fetchall()
    return [(r[0], r[1] or 0) for r in divs]


def update_stock_fundamental(codes, conn=None, start=None, end=None):
    """更新个股 PE/PB/市值/股息率。增量:从库内该code最大fundamental日期次日起。"""
    ensure()
    own = conn is None
    if own:
        conn = get_conn()
    end = end or util.today_str()
    n_total = 0
    for code in codes:
        code = util.with_prefix(code) if code[:2] not in ("sh", "sz", "bj") else code
        if da.is_etf_code(code) or util.is_bj(code):
            continue
        mx = conn.execute("SELECT max(trade_date) FROM fundamental WHERE code=?", (code,)).fetchone()[0]
        s = start or (util.to_date_str(mx) if mx else da.DEFAULT_START)
        df = da.fetch_stock_fundamental(code, s, end)
        if df is None or df.empty:
            continue
        # 股息率 = 近12月每股现金分红之和 / 当日收盘(收盘取自 daily_bar)
        divs = _div_yield_map(conn, code)
        df = _fill_div_yield_from_db(conn, code, df, divs)
        cols = ["code", "trade_date", "pe", "pb", "market_cap", "dividend_yield"]
        for c in cols:
            if c not in df:
                df[c] = None
        n_total += da.upsert(df[cols], "fundamental", conn=conn)
    if own:
        conn.close()
    return n_total


def _fill_div_yield_from_db(conn, code, df, divs):
    """股息率 = 近12月每股现金分红 / 当日收盘。一次性载入该股全部收盘价(避开 SQLite 变量上限)。"""
    closes = dict(conn.execute("SELECT trade_date, close FROM daily_bar WHERE code=?", (code,)).fetchall())

    def dy(d):
        c = closes.get(d)
        if not c or not divs:
            return 0.0
        lo = _minus_year(d)
        return round(sum(x for ex, x in divs if lo < ex <= d) / c, 6)
    df = df.copy()
    df["dividend_yield"] = df["trade_date"].map(dy)
    return df


def update_index_pe(index_code="sh000300", conn=None):
    """指数滚动PE 存入 fundamental(code=指数码, pe列)。供 S5 PE分位。"""
    ensure()
    own = conn is None
    if own:
        conn = get_conn()
    name = INDEX_PE_NAME.get(index_code, "沪深300")
    df = da.fetch_index_pe(name)
    if df is None or df.empty:
        if own:
            conn.close()
        return 0
    df["code"] = index_code
    df["pb"] = None
    df["market_cap"] = None
    df["dividend_yield"] = None
    n = da.upsert(df[["code", "trade_date", "pe", "pb", "market_cap", "dividend_yield"]],
                  "fundamental", conn=conn)
    if own:
        conn.close()
    return n


def index_pe_percentile(index_code, date, years=10, conn=None):
    """指数当日滚动PE 在近 years 年内的分位(0~1)。数据不足返回 None。"""
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)
    lo = _minus_year(date, years)
    rows = conn.execute("SELECT pe FROM fundamental WHERE code=? AND trade_date BETWEEN ? AND ? AND pe IS NOT NULL",
                        (index_code, lo, date)).fetchall()
    cur = conn.execute("SELECT pe FROM fundamental WHERE code=? AND trade_date<=? AND pe IS NOT NULL "
                       "ORDER BY trade_date DESC LIMIT 1", (index_code, date)).fetchone()
    if own:
        conn.close()
    if not rows or cur is None or len(rows) < 250:      # 不足1年不给分位
        return None
    pes = [r[0] for r in rows]
    c = cur[0]
    return round(sum(1 for p in pes if p <= c) / len(pes), 4)


def get_fundamental(code, date, conn):
    """截至 date 最近一条基本面(防未来函数)。返回 dict 或 None。"""
    r = conn.execute("SELECT pe,pb,market_cap,dividend_yield FROM fundamental "
                     "WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
                     (code, util.to_date_str(date))).fetchone()
    if r is None:
        return None
    return {"pe": r[0], "pb": r[1], "market_cap": r[2], "dividend_yield": r[3]}


def _minus_year(date, years=1):
    y, m, d = map(int, date.split("-"))
    return f"{y-years:04d}-{m:02d}-{d:02d}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure()
    conn = get_conn()
    print("index pe rows:", update_index_pe("sh000300", conn=conn))
    print("pctile 沪深300 today:", index_pe_percentile("sh000300", util.today_str(), conn=conn))
    print("fund rows 000001:", update_stock_fundamental(["sz000001"], conn=conn))
    print("get_fundamental 000001:", get_fundamental("sz000001", util.today_str(), conn))
    conn.close()
    da.bs_logout()
