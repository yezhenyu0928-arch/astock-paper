# -*- coding: utf-8 -*-
"""交易日历(SPEC 模块1 的 calendar.py)。
⚠ 文件名故意用 trade_calendar 而非 calendar,避免遮蔽 Python 标准库 calendar
   (pandas/akshare 内部 import calendar,同名文件会连锁崩溃)。函数签名与 SPEC 完全一致。
数据来自 trade_calendar 表(仅开市日,is_open=1)。内存缓存 + bisect 加速回测循环。
"""
import bisect
from datetime import datetime
from db import get_conn
import util

_days = None  # 升序交易日字符串列表


def reset_cache():
    global _days
    _days = None


def _ensure(conn=None):
    global _days
    if _days is not None:
        return _days
    own = conn is None
    if own:
        conn = get_conn()
    rows = conn.execute(
        "SELECT cal_date FROM trade_calendar WHERE is_open=1 ORDER BY cal_date"
    ).fetchall()
    _days = [r[0] for r in rows]
    if own:
        conn.close()
    return _days


def trade_days(start, end):
    """[start,end] 区间内的交易日列表(升序)。"""
    s, e = util.to_date_str(start), util.to_date_str(end)
    days = _ensure()
    return [d for d in days if s <= d <= e]


def is_trade_day(date) -> bool:
    d = util.to_date_str(date)
    days = _ensure()
    i = bisect.bisect_left(days, d)
    return i < len(days) and days[i] == d


def prev_trade_day(date, n: int = 1) -> str:
    """date 之前(严格<)第 n 个交易日。"""
    d = util.to_date_str(date)
    days = _ensure()
    i = bisect.bisect_left(days, d)   # days[i] >= d;之前的都 < d
    j = i - n
    if j < 0:
        return days[0] if days else d
    return days[j]


def next_trade_day(date, n: int = 1) -> str:
    """date 之后(严格>)第 n 个交易日。"""
    d = util.to_date_str(date)
    days = _ensure()
    i = bisect.bisect_right(days, d)  # days[i] > d
    j = i + n - 1
    if j >= len(days):
        return days[-1] if days else d
    return days[j]


def _same_period(a: str, b: str, mode: str) -> bool:
    da = datetime.strptime(a, "%Y-%m-%d").date()
    db = datetime.strptime(b, "%Y-%m-%d").date()
    if mode == "week":
        ya, wa, _ = da.isocalendar()
        yb, wb, _ = db.isocalendar()
        return (ya, wa) == (yb, wb)
    return (da.year, da.month) == (db.year, db.month)


def last_trade_day_of_week(date) -> bool:
    """date 是否为其所在自然周的最后一个交易日(周内最后)。"""
    d = util.to_date_str(date)
    if not is_trade_day(d):
        return False
    nxt = next_trade_day(d)
    if nxt == d:
        return True
    return not _same_period(d, nxt, "week")


def last_trade_day_of_month(date) -> bool:
    """date 是否为其所在自然月的最后一个交易日。"""
    d = util.to_date_str(date)
    if not is_trade_day(d):
        return False
    nxt = next_trade_day(d)
    if nxt == d:
        return True
    return not _same_period(d, nxt, "month")
