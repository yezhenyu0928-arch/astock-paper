# -*- coding: utf-8 -*-
"""全 A 股补数(科创 688 / 创业 300·301 / 北交 8xx·4xx·920), 供"全 A 策略"回测。

设计:
  - 复用现有 daily_bar / fundamental / index_members 表结构(与主板同源, 因子用收益率不依赖绝对价)。
  - 日线: akshare stock_zh_a_hist(adjust=hfq) → 直接存后复权 close。
  - 基本面: 优先 akshare stock_value_em(时序 市值/PE/PB); 超时/失败回退 stock_individual_info_em 快照。
  - 关键加固: 每个 akshare 网络调用包超时(线程 join), 防止东财接口挂死导致整轮卡住。
  - 幂等可续跑: 日线 >=500 且 基本面 >0 才整体跳过; 仅日线齐但基本面缺则只补基本面。
  - 结果写入 index_members['all_a'](与 mainboard 并存), 供新策略 pool_index='all_a' 使用。

用法: python backfill_all_a.py  (可设环境变量 AA_ONLY=1 只补不建池)
"""
import sys
import io
import time
import socket
import threading
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

import akshare as ak
import sqlite3
from db import get_conn

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(asctime)s %(message)s",
                    stream=sys.stderr)
log = logging.getLogger("backfill_all_a")

# 全局 socket 超时作为最后兜底(防止底层挂死)
socket.setdefaulttimeout(30)

NEW_PREFIX = ("688", "300", "301")          # 科创 / 创业
BJ_PREFIX = ("430", "831", "832", "833", "834", "835", "836", "837", "838", "839",
             "840", "841", "842", "843", "844", "845", "846", "847", "848", "849",
             "870", "871", "872", "873", "874", "875", "876", "877", "878", "879",
             "880", "881", "882", "883", "884", "885", "886", "887", "888", "889", "920")
START = "20220101"
DELAY = 0.06  # 轻量限速, 避免被东财封
CALL_TIMEOUT = 25   # 单次 akshare 调用超时
CALL_RETRY = 2      # 超时/异常重试次数


def _prefix(code6):
    return code6[:3]


def _is_new_board(code6):
    p3 = code6[:3]
    return p3 in NEW_PREFIX or p3 in BJ_PREFIX


def _full(code6):
    if code6[:1] in ("6", "9"):
        return "sh" + code6
    if code6[:1] in ("8", "4", "9"):
        return "bj" + code6
    return "sz" + code6


def with_timeout(fn, *a, **k):
    """在线程里跑 fn, 超时抛 TimeoutError; 捕获异常并上抛。"""
    res = [None]
    exc = [None]

    def runner():
        try:
            res[0] = fn(*a, **k)
        except Exception as e:  # noqa
            exc[0] = e

    for attempt in range(1, CALL_RETRY + 2):
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(CALL_TIMEOUT)
        if t.is_alive():
            if attempt <= CALL_RETRY:
                log.warning("超时重试 %s (第%d次)", getattr(fn, "__name__", "call"), attempt)
                time.sleep(1.0)
                continue
            raise TimeoutError(f"{getattr(fn, '__name__', 'call')} 超时 {CALL_TIMEOUT}s")
        if exc[0] is not None:
            raise exc[0]
        return res[0]
    raise TimeoutError(f"{getattr(fn, '__name__', 'call')} 重试耗尽")


def fetch_codes():
    """akshare 全部 A 股代码(含主板/科创/创业/北交/京市)。返回 set('600519'...)。"""
    df = ak.stock_info_a_code_name()
    out = set()
    for v in df["code"].astype(str):
        v = v.zfill(6)
        if len(v) == 6 and v.isdigit():
            out.add(v)
    return out


def upsert_daily(conn, full, rows):
    data = [(full, d, o, h, l, c, v, a, 1.0, 0, None, None, "akshare_hfq")
            for (d, o, h, l, c, v, a) in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO daily_bar "
        "(code,trade_date,open,high,low,close,volume,amount,adj_factor,is_suspended,limit_up,limit_down,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
    conn.commit()


def fetch_daily(code):
    df = with_timeout(ak.stock_zh_a_hist, symbol=code, period="daily",
                     start_date=START, end_date="20261231", adjust="hfq")
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append((str(r["日期"]), float(r["开盘"]), float(r["最高"]),
                         float(r["最低"]), float(r["收盘"]), float(r["成交量"]),
                         float(r["成交额"])))
        except Exception:
            continue
    return rows


def _value_em(conn, full):
    code6 = full[2:]
    df = with_timeout(ak.stock_value_em, symbol=code6)
    if df is None or df.empty:
        return False
    for _, r in df.iterrows():
        try:
            d = str(r["数据日期"])[:10]
            conn.execute(
                "INSERT OR REPLACE INTO fundamental "
                "(code,trade_date,pe,pb,market_cap,dividend_yield) VALUES (?,?,?,?,?,?)",
                (full, d,
                 float(r["PE(TTM)"]) if r["PE(TTM)"] not in (None, "") else None,
                 float(r["市净率"]) if r["市净率"] not in (None, "") else None,
                 float(r["总市值"]) if r["总市值"] not in (None, "") else None,
                 None))
        except Exception:
            continue
    conn.commit()
    return True


def _info_em(conn, full):
    code6 = full[2:]
    info = with_timeout(ak.stock_individual_info_em, symbol=code6)
    m = {str(r["item"]): r["value"] for _, r in info.iterrows()}
    mc = m.get("总市值")
    pe = m.get("市盈率")
    pb = m.get("市净率")
    dy = m.get("股息率TTM") or m.get("股息率")
    today = time.strftime("%Y-%m-%d")
    conn.execute(
        "INSERT OR REPLACE INTO fundamental "
        "(code,trade_date,pe,pb,market_cap,dividend_yield) VALUES (?,?,?,?,?,?)",
        (full, today,
         float(pe) if pe not in (None, "") else None,
         float(pb) if pb not in (None, "") else None,
         float(mc) if mc not in (None, "") else None,
         float(dy) if dy not in (None, "") else None))
    conn.commit()
    return True


def fetch_fund(conn, full):
    """时序基本面优先 stock_value_em; 失败/超时回退 info_em 快照。"""
    try:
        if _value_em(conn, full):
            return True
    except Exception as e:
        log.warning("value_em %s 失败: %s", full, e)
    try:
        return _info_em(conn, full)
    except Exception as e:
        log.warning("info_em %s 失败: %s", full, e)
        return False


def main():
    t0 = time.time()
    conn = get_conn()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_code ON fundamental(code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_code ON daily_bar(code)")
    codes = fetch_codes()
    targets = sorted(c for c in codes if _is_new_board(c))
    log.info("全 A 候选(科创/创业/北交): %d 只", len(targets))

    done = 0
    for i, code in enumerate(targets, 1):
        full = _full(code)
        have = conn.execute("SELECT count(*) FROM daily_bar WHERE code=?", (full,)).fetchone()[0]
        fund_have = conn.execute("SELECT count(*) FROM fundamental WHERE code=?", (full,)).fetchone()[0]
        if have >= 500 and fund_have > 0:
            done += 1
            if i % 200 == 0:
                log.info("跳过已完成 %d/%d (累计%d) 用时%.0fs", i, len(targets), done, time.time() - t0)
            continue
        # 日线(若已有则跳过以省时)
        rows = None
        if have < 500:
            try:
                rows = fetch_daily(code)
                if rows:
                    upsert_daily(conn, full, rows)
            except Exception as e:
                log.warning("日线 %s 失败: %s", code, e)
                time.sleep(DELAY)
                continue
        # 基本面
        if fund_have == 0:
            try:
                fetch_fund(conn, full)
            except Exception as e:
                log.warning("基本面 %s 失败: %s", code, e)
        if i % 50 == 0:
            log.info("进度 %d/%d (%.0fs) 本只%s 日线条数=%s",
                     i, len(targets), time.time() - t0, code,
                     len(rows) if rows is not None else "skip")
        time.sleep(DELAY)

    # 构建 all_a 池(新板 + 已有 mainboard)
    if not os_environ_aa_only():
        mb = [r[0] for r in conn.execute(
            "SELECT code FROM index_members WHERE index_code='mainboard'").fetchall()]
        new = [_full(c) for c in targets]
        all_a = sorted(set(mb) | set(new))
        have_set = set(r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh6%' OR code LIKE 'sz0%' "
            "OR code LIKE 'sh9%' OR code LIKE 'sz3%' OR code LIKE 'bj%'").fetchall())
        all_a = [c for c in all_a if c in have_set]
        conn.execute("DELETE FROM index_members WHERE index_code='all_a'")
        conn.executemany(
            "INSERT OR REPLACE INTO index_members(index_code,code,in_date,out_date) VALUES ('all_a',?,?,NULL)",
            [(c, "2018-01-01") for c in all_a])
        conn.commit()
        log.info("all_a 池构建完成: %d 只 (主板%d + 新板%d)", len(all_a), len(mb), len(all_a) - len(mb))
    conn.close()
    log.info("全 A 补数完成 用时%.0fs", time.time() - t0)


def os_environ_aa_only():
    import os
    return os.environ.get("AA_ONLY") == "1"


if __name__ == "__main__":
    main()
