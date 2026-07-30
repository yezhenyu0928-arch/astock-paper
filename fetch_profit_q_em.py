# -*- coding: utf-8 -*-
"""东财业绩报表(stock_yjbb_em)批量补全A profit_q 季报数据。

与 fetch_profit_q.py(baostock 逐只, 5000只x32季≈13h)相比:
  东财按季度一次拉全市场(~5900只/次), 2018Q1~2026Q2 共34次调用≈6分钟。

写入约定(与 baostock 版对齐):
  net_profit = 累计归母净利(东财'净利润-净利润', baostock netProfit 同为累计)
  roe_avg    = 累计净资产收益率
  mb_revenue = 累计营业总收入
  已有(code,stat_date)行用 INSERT OR IGNORE 保留 baostock 原数据(s42 不受扰动)。
附带: 用'所处行业'补 stock_industry 缺失映射(尤其科创板)。
"""
import akshare as ak
import sqlite3, socket, time, sys
from concurrent.futures import ThreadPoolExecutor

socket.setdefaulttimeout(30)
DB = "db/market.sqlite"
CALL_TIMEOUT = 90
RETRY = 2

def code_full(code6: str):
    code6 = str(code6).zfill(6)
    if code6.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh" + code6
    if code6.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz" + code6
    if code6.startswith("920"):
        return "sh" + code6          # 库内北交统一 sh920 前缀
    if code6.startswith(("430", "83", "87", "88")):
        return "bj" + code6
    return None

def quarters():
    out = []
    for y in range(2018, 2027):
        for md in ("0331", "0630", "0930", "1231"):
            q = f"{y}{md}"
            if q <= "20260630":
                out.append(q)
    return out

def f(x):
    try:
        v = float(x)
        return v if v == v else None   # NaN -> None
    except Exception:
        return None

def main():
    conn = sqlite3.connect(DB, timeout=120)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS profit_q (
        code TEXT, stat_date TEXT, pub_date TEXT,
        net_profit REAL, roe_avg REAL, mb_revenue REAL,
        PRIMARY KEY(code, stat_date))""")
    conn.execute("CREATE TABLE IF NOT EXISTS stock_industry (code TEXT PRIMARY KEY, industry TEXT)")
    total = ind_new = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=1) as ex:
        for qd in quarters():
            stat_date = f"{qd[:4]}-{qd[4:6]}-{qd[6:]}"
            df = None
            for attempt in range(RETRY + 1):
                fut = ex.submit(ak.stock_yjbb_em, date=qd)
                try:
                    df = fut.result(timeout=CALL_TIMEOUT)
                    break
                except Exception as e:
                    print(f"[{qd}] attempt{attempt} {type(e).__name__}", flush=True)
                    time.sleep(3)
            if df is None or df.empty:
                print(f"[{qd}] SKIP(无数据)", flush=True)
                continue
            rows, irows = [], []
            for _, r in df.iterrows():
                full = code_full(r["股票代码"])
                if not full:
                    continue
                pub = str(r.get("最新公告日期") or "")[:10] or None
                rows.append((full, stat_date, pub,
                             f(r.get("净利润-净利润")), f(r.get("净资产收益率")),
                             f(r.get("营业总收入-营业总收入"))))
                ind = r.get("所处行业")
                if isinstance(ind, str) and ind and ind != "nan":
                    irows.append((full, ind))
            conn.executemany("INSERT OR IGNORE INTO profit_q VALUES(?,?,?,?,?,?)", rows)
            c2 = conn.executemany("INSERT OR IGNORE INTO stock_industry VALUES(?,?)", irows)
            ind_new += c2.rowcount if c2.rowcount and c2.rowcount > 0 else 0
            conn.commit()
            total += len(rows)
            print(f"[{qd}] rows={len(rows)} 累计={total} {(time.time()-t0):.0f}s", flush=True)
            time.sleep(1)
    n = conn.execute("select count(*) from profit_q").fetchone()[0]
    print("profit_q 总行数:", n, "| 行业新增:", ind_new)
    for pref in ["sh600", "sz000", "sz002", "sh688", "sz300", "sz301", "sh920"]:
        print(pref, conn.execute(
            f"select count(distinct code) from profit_q where code like '{pref}%'").fetchone()[0])
    conn.close()
    print(f"DONE {(time.time()-t0)/60:.1f}min")

if __name__ == "__main__":
    main()
