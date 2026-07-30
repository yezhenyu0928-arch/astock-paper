"""抓取全市场逐股研报历史 -> analyst_report 表，用于构建"分析师评级/一致预期上调"因子(选项B)。
仅拉取有分析师覆盖的票(空快照 2810 只) ∩ all_a 基础宇宙(>=480日线)。
后台运行，断点续拉，限流重试。
用法: python fetch_analyst_reports.py
"""
import time, sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import akshare as ak
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
SLEEP = 0.35          # 每次调用后休眠，降低限流
RETRY = 2
WINDOW_DAYS = 366*9   # 研报历史回溯上限(覆盖2022-2025回测窗即可，但多拉无妨)

RATING_MAP = {'买入':1.0,'增持':0.7,'持有':0.4,'中性':0.2,'减持':0.1,'卖出':0.0,'':None}

def get_conn():
    return db.get_conn()

def _bare(code):
    """剥掉交易所前缀, 统一成裸6位代码(akshare 接口用裸码, 本地库用带前缀码)。"""
    return ''.join(ch for ch in str(code) if ch.isdigit())

def get_target_codes(conn):
    # 1) 空快照: 有分析师覆盖的票(裸6位)
    try:
        snap = ak.stock_profit_forecast_em(symbol='')
        covered = set(_bare(x) for x in snap['代码'].astype(str).tolist())
        print('[snapshot] 有覆盖票:', len(covered))
    except Exception as e:
        print('[snapshot] 失败, 退回全基础宇宙:', e)
        covered = None
    # 2) 基础宇宙: >=480 日线(带前缀), 转裸码
    base = [r[0] for r in conn.execute(
        "SELECT code FROM daily_bar GROUP BY code HAVING count(*)>=480").fetchall()]
    base_bare = {_bare(c) for c in base}
    print('[base] all_a基础宇宙:', len(base))
    if covered is None:
        return list(base_bare)
    target = [b for b in covered if b in base_bare]
    print('[target] 交集(有覆盖∩基础宇宙):', len(target))
    return target

def ensure_tables(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS analyst_report("
        "code TEXT, date TEXT, rating TEXT, eps26 REAL, eps27 REAL, eps28 REAL, "
        "institution TEXT, name TEXT, "
        "PRIMARY KEY(code, date, institution, eps26))")
    conn.execute("CREATE TABLE IF NOT EXISTS analyst_pull_log("
        "code TEXT PRIMARY KEY, status TEXT, rows INTEGER, ts TEXT)")
    conn.commit()

def already_done(conn, code):
    r = conn.execute("SELECT status FROM analyst_pull_log WHERE code=?", (code,)).fetchone()
    return r and r[0] == 'done'

def parse_and_store(conn, code, df):
    if df is None or len(df) == 0:
        return 0
    cnt = 0
    for _, row in df.iterrows():
        try:
            d = row.get('日期')
            if d is None: continue
            ds = pd.to_datetime(d).strftime('%Y-%m-%d')
            rating = str(row.get('东财评级','') or '')
            eps26 = row.get('2026-盈利预测-收益'); eps26 = float(eps26) if pd.notna(eps26) else None
            eps27 = row.get('2027-盈利预测-收益'); eps27 = float(eps27) if pd.notna(eps27) else None
            eps28 = row.get('2028-盈利预测-收益'); eps28 = float(eps28) if pd.notna(eps28) else None
            inst = str(row.get('机构','') or '')
            name = str(row.get('股票简称','') or '')
            conn.execute('INSERT OR IGNORE INTO analyst_report VALUES(?,?,?,?,?,?,?,?)',
                (code, ds, rating, eps26, eps27, eps28, inst, name))
            cnt += 1
        except Exception:
            continue
    return cnt

def main():
    conn = get_conn()
    ensure_tables(conn)
    target = get_target_codes(conn)
    total = len(target)
    done = sum(1 for c in target if already_done(conn, c))
    print(f'[start] total={total} already_done={done} todo={total-done}')
    processed = done
    for i, code in enumerate(target):
        if already_done(conn, code):
            continue
        ok = False; rows = 0
        for attempt in range(RETRY+1):
            try:
                df = ak.stock_research_report_em(symbol=code)
                rows = parse_and_store(conn, code, df)
                ok = True
                break
            except Exception as e:
                if attempt < RETRY:
                    time.sleep(1.5*(attempt+1))
                else:
                    print(f'  [ERR] {code}: {e}')
        conn.execute("INSERT OR REPLACE INTO analyst_pull_log VALUES(?,?,?,?)",
            (code, 'done' if ok else 'fail', rows, time.strftime('%Y-%m-%d %H:%M')))
        conn.commit()
        processed += 1
        time.sleep(SLEEP)
        if processed % 50 == 0:
            print(f'[progress] {processed}/{total}  rows_so_far={conn.execute("SELECT count(*) FROM analyst_report").fetchone()[0]}')
    print('[done] 抓取完成. analyst_report 总行数:', conn.execute("SELECT count(*) FROM analyst_report").fetchone()[0])

if __name__ == '__main__':
    main()
