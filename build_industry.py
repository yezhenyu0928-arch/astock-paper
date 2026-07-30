"""构建全 A 行业映射表 stock_industry (code TEXT PRIMARY KEY, industry TEXT)。

数据源(东财限流后的稳定三源, 2026-07-29 本地验证可用):
  1) 深交所官方 A 股列表(自带"所属行业"列): 000/001/002/003/300/301
  2) 北交所官方列表: 920(库内前缀 sh920)/43x/8xx
  3) 新浪行业板块成分: 补沪市 600/601/603/605/688

factors.get_industry 优先读 stock_industry 表, 建成即生效。
科创板 688 覆盖偏少(新浪源仅部分), mf_core 已改"未知行业不占 max_per_industry 上限", 不阻塞。
"""
import sqlite3, socket, time, logging, sys

socket.setdefaulttimeout(30)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
log = logging.getLogger("industry")

DB = "db/market.sqlite"


def main():
    import akshare as ak
    c = sqlite3.connect(DB, timeout=120)
    c.execute("CREATE TABLE IF NOT EXISTS stock_industry (code TEXT PRIMARY KEY, industry TEXT)")
    total = 0

    # 1) 深交所官方
    try:
        df = ak.stock_info_sz_name_code(symbol="A股列表")
        rows = [("sz" + str(r["A股代码"]).zfill(6), str(r["所属行业"]))
                for _, r in df.iterrows() if str(r["所属行业"]) != "nan"]
        c.executemany("INSERT OR REPLACE INTO stock_industry VALUES (?,?)", rows)
        c.commit(); total += len(rows)
        log.info("SZ 写入 %d", len(rows))
    except Exception as e:
        log.info("SZ FAIL %s %s", type(e).__name__, str(e)[:80])

    # 2) 北交所官方
    try:
        df = ak.stock_info_bj_name_code()
        rows = []
        for _, r in df.iterrows():
            code6 = str(r["证券代码"]).zfill(6)
            ind = str(r["所属行业"])
            if ind == "nan":
                continue
            pref = "sh" if code6.startswith("920") else "bj"
            rows.append((pref + code6, ind))
        c.executemany("INSERT OR REPLACE INTO stock_industry VALUES (?,?)", rows)
        c.commit(); total += len(rows)
        log.info("BJ 写入 %d", len(rows))
    except Exception as e:
        log.info("BJ FAIL %s %s", type(e).__name__, str(e)[:80])

    # 3) 新浪行业成分, 补沪市
    try:
        secs = ak.stock_sector_spot(indicator="新浪行业")
        sh_rows = {}
        for _, s in secs.iterrows():
            label = s["label"]; name = s["板块"]
            d = None
            for _ in range(2):
                try:
                    d = ak.stock_sector_detail(sector=label)
                    break
                except Exception:
                    time.sleep(2)
            if d is None:
                log.info("SINA SKIP %s", name)
                continue
            for _, r in d.iterrows():
                code = str(r["code"])
                if code.startswith(("600", "601", "603", "605", "688", "689")):
                    sh_rows["sh" + code] = name
            time.sleep(0.3)
        rows = list(sh_rows.items())
        c.executemany("INSERT OR REPLACE INTO stock_industry VALUES (?,?)", rows)
        c.commit(); total += len(rows)
        log.info("SH(新浪) 写入 %d", len(rows))
    except Exception as e:
        log.info("SINA FAIL %s %s", type(e).__name__, str(e)[:80])

    n = c.execute("select count(*) from stock_industry").fetchone()[0]
    print("stock_industry 总数:", n)
    for pref in ["sh600", "sh688", "sz300", "sz301", "sh920"]:
        print(pref, c.execute(
            f"select count(*) from stock_industry where code like '{pref}%'").fetchone()[0])
    c.close()


if __name__ == "__main__":
    main()
