"""本地抓取 baostock 季报数据 -> db/market.sqlite 新表 profit_q。
仅本地可用(云端海外Runner连不上baostock); 抓完需重导种子库上云。
用法:
  python fetch_profit_q.py            # 全量 daily_bar 去重 code, 2020-2026, 按4季抓取
  python fetch_profit_q.py 400      # 仅前400个code(小样本快速验证)
字段: code, stat_date(季末日), pub_date(公告日), net_profit(单季归母), roe_avg, mb_revenue(单季营收)
net_profit 单位: 元(如 28592000000.0); 比值尺度无关, SUE/同比均稳健。
"""
import baostock as bs
import sqlite3, sys, time

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 10**9
START_YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else 2020
END_YEAR = int(sys.argv[3]) if len(sys.argv) > 3 else 2026

conn = sqlite3.connect("db/market.sqlite")
# 仅抓 fundamental 覆盖的 1465 只(真实可投宇宙), 避免 daily_bar 中无基本面数据的票浪费调用。
codes = [r[0] for r in conn.execute(
    "SELECT DISTINCT code FROM fundamental ORDER BY code")]
codes = codes[:LIMIT]
conn.execute("""CREATE TABLE IF NOT EXISTS profit_q (
    code TEXT, stat_date TEXT, pub_date TEXT,
    net_profit REAL, roe_avg REAL, mb_revenue REAL,
    PRIMARY KEY(code, stat_date))""")
conn.commit()

bs.login()
cur = conn.cursor()
n = 0
t0 = time.time()
for ci, code in enumerate(codes):
    bs_code = code[:2] + "." + code[2:]   # sh510300 -> sh.510300 (baostock 9位格式)
    # 长会话保护: 每300只重登录一次, 防 session 静默失效导致后续全空。
    if ci > 0 and ci % 300 == 0:
        try:
            bs.logout()
        except Exception:
            pass
        bs.login()
    for y in range(START_YEAR, END_YEAR + 1):
        for q in (1, 2, 3, 4):
            try:
                rs = bs.query_profit_data(bs_code, year=str(y), quarter=q)
                for row in rs.get_data().values.tolist():
                    # 字段: code,pubDate,statDate,roeAvg,npMargin,gpMargin,netProfit,epsTTM,MBRevenue,totalShare,liqaShare
                    stat, pub = row[2], row[1]

                    def f(x):
                        try:
                            return float(x) if x not in ("", None) else None
                        except Exception:
                            return None

                    cur.execute(
                        "INSERT OR REPLACE INTO profit_q VALUES(?,?,?,?,?,?)",
                        (code, stat, pub, f(row[6]), f(row[3]), f(row[8])))
                    n += 1
            except Exception:
                pass
    if n % 200 == 0:
        conn.commit()
        print(f"  {code} n={n} {time.time()-t0:.0f}s", flush=True)
conn.commit()
bs.logout()
conn.close()
print(f"DONE codes={len(codes)} calls={n} {time.time()-t0:.0f}s")
