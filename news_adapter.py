# -*- coding: utf-8 -*-
"""消息面数据层(SPEC_NEWS N1)。与 data_adapter 同级,负责抓快讯/个股新闻/公告/宏观日历并落库去重。
铁律:消息层是保险不是引擎,任何抓取失败都不得阻断主流程 → 全部 try/except 静默降级。"""
import hashlib
import logging
import pandas as pd

import util
from db import get_conn, ensure_table

log = logging.getLogger("news_adapter")

NEWS_DDL = """
CREATE TABLE IF NOT EXISTS news_raw (
  id TEXT PRIMARY KEY, ts TEXT, source TEXT, code TEXT, title TEXT, content TEXT);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_raw(ts);
CREATE TABLE IF NOT EXISTS news_signal (
  signal_date TEXT, scope TEXT, score REAL, level TEXT, evidence TEXT,
  PRIMARY KEY(signal_date, scope));
"""


def ensure():
    ensure_table(NEWS_DDL)


def _hash(source, ts, title):
    return hashlib.md5(f"{source}|{ts}|{title}".encode("utf-8")).hexdigest()


def _retry(fn, what=""):
    try:
        return fn()
    except Exception as e:
        log.warning("消息抓取失败(降级) %s: %s", what, e)
        return None


# ---------------- 抓取 ----------------
def fetch_flash(since_ts=None) -> pd.DataFrame:
    """快讯。用新浪财经新闻 API(稳定,无需 akshare 版本同步)。返回 ts,title,content,source。"""
    rows = _retry(_fetch_sina_realtime, "flash_sina")
    if rows:
        out = pd.DataFrame(rows)
        if since_ts:
            out = out[out["ts"] >= since_ts]
        return out
    # 备源:akshare 央视新闻联播文字稿
    try:
        import akshare as ak
        df2 = _retry(lambda: ak.news_cctv(), "news_cctv")
        if df2 is not None and not df2.empty:
            return pd.DataFrame({"ts": df2.get("date", "").astype(str),
                                 "title": df2.get("title", ""), "content": df2.get("content", ""),
                                 "source": "news_cctv"})
    except Exception:
        pass
    return pd.DataFrame(columns=["ts", "title", "content", "source"])


def _fetch_sina_realtime():
    """新浪财经滚动新闻。返回 list of {ts,title,content,source}。"""
    import requests
    r = requests.get("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&num=30",
                     timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    data = r.json()
    items = []
    for entry in (data.get("result", {}) or {}).get("data", []) or []:
        items.append({
            "ts": (entry.get("ctime", "") or "")[:19].replace("T", " "),
            "title": entry.get("title", ""),
            "content": entry.get("summary", ""),
            "source": "sina_roll",
        })
    return items


def fetch_stock_news(code, days=3) -> pd.DataFrame:
    """个股新闻。返回 ts,title,content,source,code。"""
    import akshare as ak
    six = util.bare(code)
    df = _retry(lambda: ak.stock_news_em(symbol=six), f"stock_news {six}")
    if df is None or df.empty:
        return pd.DataFrame(columns=["ts", "title", "content", "source", "code"])
    out = pd.DataFrame({"ts": df.get("发布时间", "").astype(str),
                        "title": df.get("新闻标题", ""), "content": df.get("新闻内容", ""),
                        "source": df.get("文章来源", "em"), "code": util.with_prefix(code)})
    return out


def fetch_announcements(codes, date) -> pd.DataFrame:
    """交易所/巨潮公告(best-effort)。返回 code,ts,title,type。"""
    import akshare as ak
    rows = []
    for code in (codes or [])[:50]:
        df = _retry(lambda: ak.stock_news_em(symbol=util.bare(code)), f"ann {code}")
        if df is None or df.empty:
            continue
        for _, r in df.head(5).iterrows():
            rows.append({"code": util.with_prefix(code), "ts": str(r.get("发布时间", "")),
                         "title": str(r.get("新闻标题", "")), "type": "news"})
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["code", "ts", "title", "type"])


def fetch_macro_calendar(date) -> pd.DataFrame:
    """当日宏观事件(best-effort)。返回 ts,title,type。"""
    import akshare as ak
    df = _retry(lambda: ak.news_economic_baidu(date=util.to_date_str(date).replace("-", "")), "macro")
    if df is None or df.empty:
        return pd.DataFrame(columns=["ts", "title", "type"])
    return pd.DataFrame({"ts": df.get("日期", "").astype(str), "title": df.get("事件", ""),
                         "type": "macro"})


# ---------------- 落库(去重) ----------------
def store_news(df, conn=None) -> int:
    """写 news_raw,主键 id=hash(source+ts+title) 去重(INSERT OR IGNORE)。"""
    if df is None or df.empty:
        return 0
    ensure()
    own = conn is None
    if own:
        conn = get_conn()
    n = 0
    for _, r in df.iterrows():
        src, ts, title = str(r.get("source", "")), str(r.get("ts", "")), str(r.get("title", ""))
        _id = _hash(src, ts, title)
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO news_raw (id,ts,source,code,title,content) VALUES (?,?,?,?,?,?)",
                (_id, ts, src, str(r.get("code", "") or ""), title, str(r.get("content", "") or "")))
            n += cur.rowcount
        except Exception as e:
            log.warning("news 落库失败: %s", e)
    conn.commit()
    if own:
        conn.close()
    return n


def store_signal(signal_date, scope, score, level, evidence, conn=None):
    ensure()
    own = conn is None
    if own:
        conn = get_conn()
    import json
    conn.execute("INSERT OR REPLACE INTO news_signal (signal_date,scope,score,level,evidence) VALUES (?,?,?,?,?)",
                 (util.to_date_str(signal_date), scope, float(score), level,
                  json.dumps(evidence, ensure_ascii=False)))
    conn.commit()
    if own:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure()
    f = fetch_flash()
    print("快讯:", len(f), "条")
    if len(f):
        print(f.head(2).to_string())
    print("落库:", store_news(f), "新条")
