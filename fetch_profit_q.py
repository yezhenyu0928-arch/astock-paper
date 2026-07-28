"""本地抓取 baostock 季报数据 -> db/market.sqlite 新表 profit_q。
仅本地可用(云端海外Runner连不上baostock); 抓完需重导种子库上云。

健壮性设计(应对 baostock 单查询挂死在死 socket):
  1) 断点续传: 启动时统计 profit_q 已抓够行数的 code, 直接跳过 -> 重启不重抓。
  2) 单调用超时: 每次 bs.query_profit_data 包进线程池, 25s 超时即放弃该次(泄漏1线程),
     并强制 logout+login 重置会话, 继续后续 code(避免整进程冻死)。
  3) 看门狗: 后台线程监控"距上次成功 commit"时长, >25min 即 os._exit(1) 自杀,
     由外层 bash 重试循环接手(续传重启)。

用法:
  python fetch_profit_q.py                 # 全量 fundamental code, 2018-2026, 按4季抓取(支持续传)
  python fetch_profit_q.py 400           # 仅前400个code(小样本快速验证)
字段: code, stat_date(季末日), pub_date(公告日), net_profit(单季归母), roe_avg, mb_revenue(单季营收)
"""
import baostock as bs
import sqlite3, sys, time, os, threading
from concurrent.futures import ThreadPoolExecutor

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 10**9
START_YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else 2018
END_YEAR = int(sys.argv[3]) if len(sys.argv) > 3 else 2026

EXPECTED = (END_YEAR - START_YEAR + 1) * 4
DONE_THRESH = int(EXPECTED * 0.7)   # 已抓够 70% 视为完成, 续传跳过
CALL_TIMEOUT = 25                    # 单次 baostock 查询超时(秒)
RELOG_EVERY = 200                    # 每 N 只重登录防 session 静默失效
COMMIT_EVERY = 100                   # 每 N 只 commit 一次(续传检查点)

_t0 = time.time()
_last_progress = {"t": _t0}

# ---- 看门狗: 卡死超 25min 自杀 ----
def _watchdog():
    while True:
        time.sleep(60)
        idle = time.time() - _last_progress["t"]
        if idle > 25 * 60:
            print(f"[watchdog] 距上次进度 {idle/60:.0f}min 无进展, 自杀退出", flush=True)
            os._exit(1)
threading.Thread(target=_watchdog, daemon=True).start()

def _q(code, y, q):
    """带超时的单次查询; 超时/异常返回 None。"""
    try:
        rs = bs.query_profit_data(code, year=str(y), quarter=q)
        return rs
    except Exception:
        return None

with ThreadPoolExecutor(max_workers=1) as ex:
    conn = sqlite3.connect("db/market.sqlite")
    conn.execute("""CREATE TABLE IF NOT EXISTS profit_q (
        code TEXT, stat_date TEXT, pub_date TEXT,
        net_profit REAL, roe_avg REAL, mb_revenue REAL,
        PRIMARY KEY(code, stat_date))""")
    conn.commit()

    # ---- 断点续传: 跳过已完成的 code ----
    done = set()
    for code, cnt in conn.execute(
            "SELECT code, COUNT(*) FROM profit_q GROUP BY code"):
        if cnt >= DONE_THRESH:
            done.add(code)
    print(f"[resume] 已完成 {len(done)} 只(阈值>= {DONE_THRESH}行), 将跳过", flush=True)

    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM fundamental ORDER BY code")]
    codes = [c for c in codes if c not in done][:LIMIT]

    def f(x):
        try:
            return float(x) if x not in ("", None) else None
        except Exception:
            return None

    bs.login()
    cur = conn.cursor()
    n = 0
    for ci, code in enumerate(codes):
        bs_code = code[:2] + "." + code[2:]   # sh510300 -> sh.510300 (baostock 9位格式)
        if ci > 0 and ci % RELOG_EVERY == 0:
            try:
                bs.logout()
            except Exception:
                pass
            bs.login()
        for y in range(START_YEAR, END_YEAR + 1):
            for q in (1, 2, 3, 4):
                fut = ex.submit(_q, bs_code, y, q)
                try:
                    rs = fut.result(timeout=CALL_TIMEOUT)
                except Exception:
                    rs = None
                    # 超时: 重置会话, 避免死 socket 污染后续
                    try:
                        bs.logout()
                    except Exception:
                        pass
                    try:
                        bs.login()
                    except Exception:
                        pass
                if rs is None:
                    continue
                try:
                    for row in rs.get_data().values.tolist():
                        # 字段: code,pubDate,statDate,roeAvg,npMargin,gpMargin,netProfit,epsTTM,MBRevenue,totalShare,liqaShare
                        stat, pub = row[2], row[1]
                        cur.execute(
                            "INSERT OR REPLACE INTO profit_q VALUES(?,?,?,?,?,?)",
                            (code, stat, pub, f(row[6]), f(row[3]), f(row[8])))
                        n += 1
                except Exception:
                    pass
        if (ci + 1) % COMMIT_EVERY == 0:
            conn.commit()
            _last_progress["t"] = time.time()
            print(f"  [{len(codes)-ci-1} left] {code} n={n} {(time.time()-_t0)/60:.0f}min", flush=True)
    conn.commit()
    _last_progress["t"] = time.time()
    try:
        bs.logout()
    except Exception:
        pass
    conn.close()
print(f"DONE codes={len(codes)} rows={n} {(time.time()-_t0)/60:.0f}min")
