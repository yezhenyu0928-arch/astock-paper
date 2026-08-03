# -*- coding: utf-8 -*-
"""指数/基准数据回填: 把真指数(sh000300/000905/000852) 写入 daily_bar, 供策略基准与大盘择时使用。
用法: python backfill_index.py   (本地/云端均可跑, 幂等增量)
云端注意: akshare 在海外 Runner 可能不可达, 但本脚本 try/except 降级, 失败不影响主流程。
"""
import sys, io, os, time, logging
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import util
import data_adapter as da
from db import get_conn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backfill_index")

# 目标指数: code -> 名称
INDEX_CODES = {
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "sh000001": "上证指数",
}


def backfill_indices(conn=None, end=None):
    own = conn is None
    if own:
        conn = get_conn()
    end = end or util.today_str()
    total = 0
    for code, name in INDEX_CODES.items():
        try:
            df = da.fetch_index_daily(code, start="2018-01-01", end=end)
        except Exception as e:
            log.warning("拉取 %s(%s) 失败(降级跳过): %s", code, name, e)
            continue
        if df is None or df.empty:
            log.warning("%s 无数据, 跳过", code)
            continue
        # 转 daily_bar 格式(code, trade_date, open, high, low, close, volume, amount,
        #                    adj_factor, is_suspended, limit_up, limit_down, source)
        rows = []
        for _, r in df.iterrows():
            rows.append((code, str(r["trade_date"])[:10],
                         float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                         0, 0, 1.0, 0, None, None, "index"))
        conn.executemany(
            "INSERT OR REPLACE INTO daily_bar "
            "(code, trade_date, open, high, low, close, volume, amount, adj_factor, "
            "is_suspended, limit_up, limit_down, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        total += len(rows)
        log.info("%s(%s): 入库 %d 行", code, name, len(rows))
    if own:
        conn.close()
    return total


if __name__ == "__main__":
    n = backfill_indices()
    print(f"指数回填完成, 共 {n} 行")
