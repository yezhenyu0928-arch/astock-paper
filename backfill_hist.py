# -*- coding: utf-8 -*-
"""补新板(688/300/301/920)2018-2021历史日线 v2 (腾讯源).
东财 stock_zh_a_hist 已被限流(RemoteDisconnected), 改用 ak.stock_zh_a_hist_tx.
关键: 腾讯 hfq 基准与东财 hfq 不同, 直接入库会在 2022-01 边界跳变.
处理: 多拉到 2022-01-15 重叠区, 取库内(东财)同日收盘做比例缩放对齐, 只写 <=2021-12-31 部分.
INSERT OR REPLACE 幂等, 可断点续跑(min(trade_date)<=2021-12-31 即跳过).
"""
import sqlite3, sys, time, socket, logging, os
import concurrent.futures as cf

os.environ.setdefault("TQDM_DISABLE", "1")
socket.setdefaulttimeout(30)
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(message)s")
log = logging.getLogger("hist")

DB = "db/market.sqlite"
START = "20180101"
END_OVERLAP = "20220115"   # 多拉重叠区用于基准对齐
CUT = "2021-12-31"         # 只入库此日期(含)之前
CALL_TIMEOUT = 40
CALL_RETRY = 2

import akshare as ak
try:
    from tqdm import tqdm
    import tqdm as _tqdm_mod
    _tqdm_mod.tqdm.__init__ = (lambda orig: lambda self, *a, **k: orig(self, *a, **{**k, "disable": True}))(_tqdm_mod.tqdm.__init__)
except Exception:
    pass


def with_timeout(fn, *a, **k):
    last = None
    for i in range(CALL_RETRY + 1):
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(fn, *a, **k)
            try:
                return fut.result(timeout=CALL_TIMEOUT)
            except Exception as e:
                last = e
                time.sleep(1 + i)
    raise last


def fetch_tx(full):
    """腾讯源, symbol 形如 sh688001/sz300750; 返回 [(date,o,h,l,c,vol,amt)] 已含重叠区"""
    sym = full if not full.startswith("sh920") else full  # 920 北交尝试原样
    df = with_timeout(ak.stock_zh_a_hist_tx, symbol=sym,
                      start_date=START, end_date=END_OVERLAP, adjust="hfq")
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        try:
            out.append((str(r["date"]), float(r["open"]), float(r["high"]),
                        float(r["low"]), float(r["close"]),
                        float(r["volume"]), float(r.get("amount") or 0.0)))
        except Exception:
            continue
    return out


WORKERS = 4


def main():
    conn = sqlite3.connect(DB, timeout=120)
    conn.execute("PRAGMA journal_mode=WAL")
    todo = [r[0] for r in conn.execute(
        "SELECT code FROM daily_bar WHERE code LIKE 'sh688%' OR code LIKE 'sz300%' "
        "OR code LIKE 'sz301%' OR code LIKE 'sh920%' "
        "GROUP BY code HAVING min(trade_date) > '2021-12-31'").fetchall()]
    log.info("待补历史: %d 只 (workers=%d)", len(todo), WORKERS)
    done = fail = empty = noalign = 0
    t0 = time.time()
    pool = cf.ThreadPoolExecutor(max_workers=WORKERS)

    def _fetch_safe(full):
        try:
            return full, fetch_tx(full), None
        except Exception as e:
            return full, None, e

    for i, (full, rows, err) in enumerate(pool.map(_fetch_safe, todo), 1):
        if err is not None:
            fail += 1
            log.info("FAIL %s %s", full, type(err).__name__)
            continue
        hist = [r for r in rows if r[0] <= CUT]
        if not hist:
            empty += 1  # 2022年后才上市, 正常
        else:
            # 基准对齐: 用重叠区(2022-01-04~01-15)库内东财收盘 / 腾讯收盘 求比例
            ov = {r[0]: r[4] for r in rows if r[0] > CUT}
            scale = None
            if ov:
                q = conn.execute(
                    "SELECT trade_date, close FROM daily_bar WHERE code=? AND trade_date>? AND trade_date<=? ORDER BY trade_date",
                    (full, CUT, "2022-01-15")).fetchall()
                for d, c_em in q:
                    if d in ov and ov[d] and c_em:
                        scale = c_em / ov[d]
                        break
            if scale is None:
                # 无重叠(2022年初停牌等): 不缩放直接用(标记), 仍比缺失好
                scale = 1.0
                noalign += 1
            data = [(full, d, o * scale, h * scale, l * scale, c * scale, v, a,
                     1.0, 0, None, None, "akshare_tx_hfq_aligned")
                    for (d, o, h, l, c, v, a) in hist]
            conn.executemany(
                "INSERT OR REPLACE INTO daily_bar "
                "(code,trade_date,open,high,low,close,volume,amount,adj_factor,is_suspended,limit_up,limit_down,source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
            conn.commit()
            done += 1
        if i % 50 == 0:
            el = time.time() - t0
            print(f"{i}/{len(todo)} 补={done} 空={empty} 失败={fail} 未对齐={noalign} 用时{el:.0f}s", flush=True)
    print(f"完成: 补={done} 空(22年后上市)={empty} 失败={fail} 未对齐={noalign} 总用时{time.time()-t0:.0f}s", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
