# -*- coding: utf-8 -*-
"""通用工具:时区、代码格式、金额/手数取整、涨跌停兜底。
代码统一格式:小写前缀+6位,如 sh510300 / sz000001(SPEC 模块0)。
⚠ 时区:一切"今天"必须用 Asia/Shanghai(Actions 是 UTC)。
"""
import math
from datetime import datetime, date
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def today_str() -> str:
    return now_cn().strftime("%Y-%m-%d")


def to_date_str(d) -> str:
    """把 date/datetime/str 统一成 'YYYY-MM-DD'。"""
    if isinstance(d, str):
        return d[:10]
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, date):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


# ---------------- 代码格式 ----------------
def bare(code: str) -> str:
    """去前缀取6位数字:sh510300 -> 510300。"""
    code = str(code).strip()
    if len(code) >= 2 and code[:2].isalpha():
        return code[2:]
    return code


def market(code: str) -> str:
    """返回 'sh'/'sz'/'bj'。带前缀直接读;裸码按规则判断。"""
    code = str(code).strip().lower()
    if len(code) >= 2 and code[:2] in ("sh", "sz", "bj"):
        return code[:2]
    return _guess_market(bare(code))


def _guess_market(six: str) -> str:
    """按股票/ETF 语境判断(注:指数 000xxx 需显式带 sh 前缀,不走此函数)。"""
    if not six:
        return "sh"
    if six[:3] == "920" or six[0] in ("4", "8"):   # 北交所
        return "bj"
    if six[0] in ("6", "5", "9"):                   # 沪市股票/ETF/B股
        return "sh"
    if six[0] in ("0", "3", "1", "2"):              # 深市股票/ETF/B股
        return "sz"
    return "sh"


def with_prefix(six: str) -> str:
    """裸6位 -> 带前缀。用于把 akshare 返回的裸码标准化。"""
    six = bare(six)
    return _guess_market(six) + six


def is_bj(code: str) -> bool:
    return market(code) == "bj"


def is_star_or_chinext(code: str) -> bool:
    """科创板688 / 创业板300/301 -> 涨跌停 ±20%。"""
    six = bare(code)
    return six[:3] in ("688", "689", "300", "301")


def limit_pct(code: str, is_st: bool = False) -> float:
    """涨跌停幅度。ST=5%,科创/创业=20%,主板=10%。北交所30%(本系统排除)。"""
    if is_st:
        return 0.05
    if is_bj(code):
        return 0.30
    if is_star_or_chinext(code):
        return 0.20
    return 0.10


def price_limits(prev_close: float, code: str, is_st: bool = False):
    """涨跌停价兜底:前收×(1±pct),四舍五入到分。"""
    pct = limit_pct(code, is_st)
    up = r2(prev_close * (1 + pct))
    down = r2(prev_close * (1 - pct))
    return up, down


# ---------------- 数值 ----------------
def r2(x) -> float:
    """金额统一 round 到 2 位。"""
    try:
        return round(float(x) + 1e-9, 2)
    except (TypeError, ValueError):
        return 0.0


def floor100(shares) -> int:
    """向下取整到 100 股(1手)。"""
    try:
        return int(math.floor(float(shares) / 100.0)) * 100
    except (TypeError, ValueError):
        return 0
