# -*- coding: utf-8 -*-
"""宏观指标数据层(手册宏观择时7指标补全)。

补全手册宏观择时所需的「PMI / 社融 / 北向资金 / 融资余额」4 类外部数据,
通过 akshare 拉取并存入 macro_indicator 表(与 fundamental/news 同款的扩展表模式)。

设计要点(与 news_guard 一致的「只读数、不未来函数」原则):
- 数据在 backfill / 定时任务阶段拉取入库(需联网),回测/实盘时只读表内历史值,不实时联网、不泄漏未来。
- 每个指标的拉取独立 try/except:某接口失效只丢该指标,其余照常;表为空时上层 macro_score_7 优雅降级(缺失指标权重归零)。
- 表结构: macro_indicator(date TEXT, name TEXT, value REAL, PRIMARY KEY(date,name))

指标 name 约定:
  PMI            制造业采购经理指数(月, ~50)
  TSF_YOY        社会融资规模存量同比(% , 月)
  NORTHBOUND_NET 北向资金当日成交净买额(亿元, 日)
  MARGIN_BALANCE 沪深融资余额合计(亿元, 日)
"""
import logging
import re
from db import get_conn, ensure_table

log = logging.getLogger("macro_data")

MACRO_DDL = """
CREATE TABLE IF NOT EXISTS macro_indicator (
    date  TEXT NOT NULL,
    name  TEXT NOT NULL,
    value REAL,
    PRIMARY KEY (date, name)
);
CREATE INDEX IF NOT EXISTS idx_macro_ind ON macro_indicator(name, date);
"""


def ensure_macro_table(conn=None):
    own = conn is None
    if own:
        conn = get_conn()
    try:
        ensure_table(MACRO_DDL, conn=conn)
    except Exception as e:
        log.warning("macro_indicator 建表失败: %s", e)
    finally:
        if own:
            conn.close()


# 模块导入即建表(fundamental/news_adapter 同款模式)
try:
    ensure_macro_table()
except Exception:
    pass


def _norm_date_pmi(s):
    """'2026年06月份' -> '2026-06-01'"""
    m = re.search(r"(\d{4})年(\d{1,2})月", str(s))
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    return None


def _norm_date_dash(s):
    """'2000-03-01' / '2023-09-22' -> 同款字符串(校验格式)"""
    s = str(s).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    return None


def _norm_date_compact(s):
    """'20230922' -> '2023-09-22'"""
    s = str(s).strip()
    if re.match(r"^\d{8}$", s):
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return None


# ───────────────────────── 拉取函数(各自容错) ─────────────────────────

def fetch_pmi():
    """制造业 PMI(月)。返回 [(date, value), ...] 或 []。"""
    try:
        import akshare as ak
        df = ak.macro_china_pmi()
        out = []
        for _, r in df.iterrows():
            d = _norm_date_pmi(r.get("月份"))
            v = r.get("制造业-指数")
            if d and v is not None:
                try:
                    out.append((d, float(v)))
                except Exception:
                    pass
        return sorted(out)
    except Exception as e:
        log.warning("PMI(akshare) 拉取失败: %s", e)
        return []


def fetch_tsf():
    """社会融资规模存量同比(% , 月)。返回 [(date, value), ...] 或 []。"""
    try:
        import akshare as ak
        df = ak.macro_china_bank_financing()
        out = []
        for _, r in df.iterrows():
            d = _norm_date_dash(r.get("日期"))
            v = r.get("最新值")
            if d and v is not None:
                try:
                    out.append((d, float(v)))
                except Exception:
                    pass
        return sorted(out)
    except Exception as e:
        log.warning("社融(akshare) 拉取失败: %s", e)
        return []


def fetch_northbound():
    """北向资金当日成交净买额(亿元, 日)。返回 [(date, value), ...] 或 []。"""
    try:
        import akshare as ak
        df = ak.stock_hsgt_hist_em(symbol="北向资金")
        out = []
        for _, r in df.iterrows():
            d = _norm_date_dash(r.get("日期"))
            v = r.get("当日成交净买额")
            if d and v is not None:
                try:
                    out.append((d, float(v)))
                except Exception:
                    pass
        return sorted(out)
    except Exception as e:
        log.warning("北向资金(akshare) 拉取失败: %s", e)
        return []


def fetch_margin():
    """沪深融资余额合计(亿元, 日)。合并 sse+szse 的融资余额(元->亿)。返回 [(date, value), ...] 或 []。"""
    try:
        import akshare as ak
        frames = []
        for fn in (ak.stock_margin_sse, ak.stock_margin_szse):
            try:
                df = fn()
                df = df[["信用交易日期", "融资余额"]].copy()
                df["信用交易日期"] = df["信用交易日期"].astype(str)
                frames.append(df)
            except Exception as e:
                log.debug("融资余额单市场拉取失败: %s", e)
        if not frames:
            return []
        merged = {}
        for df in frames:
            for _, r in df.iterrows():
                d = _norm_date_compact(r["信用交易日期"])
                v = r["融资余额"]
                if d and v is not None:
                    try:
                        merged[d] = merged.get(d, 0.0) + float(v)
                    except Exception:
                        pass
        # 元 -> 亿元
        out = [(d, v / 1e8) for d, v in merged.items()]
        return sorted(out)
    except Exception as e:
        log.warning("融资余额(akshare) 拉取失败: %s", e)
        return []


# ───────────────────────── 入库 / 访问 ─────────────────────────

def store_series(conn, name, rows):
    """rows: [(date, value), ...]。幂等 upsert。"""
    if not rows:
        return 0
    cur = conn.cursor()
    cur.executemany(
        "INSERT OR REPLACE INTO macro_indicator(date, name, value) VALUES (?,?,?)",
        [(d, name, v) for d, v in rows])
    conn.commit()
    return len(rows)


def backfill(conn=None, start="2005-01-01", end=None):
    """拉取并入库全部宏观指标。各指标独立容错;返回 {name: 入库条数}。"""
    own = conn is None
    if own:
        conn = get_conn()
    ensure_macro_table(conn=conn)
    summary = {}
    try:
        for name, fn in [("PMI", fetch_pmi), ("TSF_YOY", fetch_tsf),
                         ("NORTHBOUND_NET", fetch_northbound), ("MARGIN_BALANCE", fetch_margin)]:
            rows = fn()
            if start:
                rows = [(d, v) for d, v in rows if d >= start]
            if end:
                rows = [(d, v) for d, v in rows if d <= end]
            n = store_series(conn, name, rows)
            summary[name] = n
            log.info("macro backfill %s: %d 条", name, n)
    finally:
        if own:
            conn.close()
    return summary


def value_on(conn, name, date):
    """name 在 date(含)之前最近一条的 value;无则返回 None。"""
    try:
        r = conn.execute(
            "SELECT value FROM macro_indicator WHERE name=? AND date<=? ORDER BY date DESC LIMIT 1",
            (name, str(date))).fetchone()
        return float(r[0]) if r else None
    except Exception:
        return None


def window_sum(conn, name, date, days):
    """name 在 date(含)之前最近 days 条的 value 求和;不足返回 None。"""
    try:
        rows = conn.execute(
            "SELECT value FROM macro_indicator WHERE name=? AND date<=? ORDER BY date DESC LIMIT ?",
            (name, str(date), int(days))).fetchall()
        if not rows:
            return None
        return float(sum(r[0] for r in rows))
    except Exception:
        return None


def delta(conn, name, date, days):
    """name 在 date 与 date-days(条) 的差值;用于融资余额变化率等。"""
    try:
        rows = conn.execute(
            "SELECT value FROM macro_indicator WHERE name=? AND date<=? ORDER BY date DESC LIMIT ?",
            (name, str(date), int(days) + 1)).fetchall()
        if len(rows) < days + 1:
            return None
        cur = float(rows[0][0])
        past = float(rows[days][0])
        return cur - past
    except Exception:
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s|%(name)s|%(message)s")
    s = backfill()
    print("backfill summary:", s)
