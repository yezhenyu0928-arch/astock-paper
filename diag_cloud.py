# -*- coding: utf-8 -*-
"""云端 DB 体检:定位"新策略云端回测全空仓"根因。
输出 reports/diag_cloud_db.md:
- daily_bar / fundamental / index_members 行数与日期覆盖
- fundamental 按年 distinct code 数(历史覆盖 vs 只有近期快照)
- 模拟 2023-01-04 一次 s26 选股,打出"候选筛选"诊断行
"""
import io
import sys
import logging
import sqlite3
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(message)s")

import conf
import engine as E
from db import get_conn

out = ["# 云端 DB 体检报告", ""]
conn = get_conn()

def q(sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception as e:
        return [("ERR", str(e))]

out.append("## 表规模")
for t in ("daily_bar", "fundamental", "index_members", "security", "annual_roe", "dividend"):
    r = q(f"SELECT COUNT(*) FROM {t}")
    out.append(f"- {t}: {r[0][0]} 行")

out.append("\n## daily_bar 覆盖")
r = q("SELECT MIN(date), MAX(date), COUNT(DISTINCT code) FROM daily_bar")
out.append(f"- 日期 {r[0][0]} ~ {r[0][1]},distinct code {r[0][2]}")

out.append("\n## fundamental 覆盖(关键!)")
r = q("SELECT MIN(date), MAX(date), COUNT(DISTINCT code) FROM fundamental")
out.append(f"- 日期 {r[0][0]} ~ {r[0][1]},distinct code {r[0][2]}")
out.append("- 按年 distinct code:")
for row in q("SELECT substr(date,1,4) y, COUNT(DISTINCT code) FROM fundamental GROUP BY y ORDER BY y"):
    out.append(f"  - {row[0]}: {row[1]} 只")
out.append("- 各字段非空率(最近 30 天内快照):")
r = q("""SELECT COUNT(*),
        SUM(CASE WHEN pe IS NOT NULL THEN 1 ELSE 0 END),
        SUM(CASE WHEN market_cap IS NOT NULL AND market_cap>0 THEN 1 ELSE 0 END),
        SUM(CASE WHEN dividend_yield IS NOT NULL AND dividend_yield>0 THEN 1 ELSE 0 END)
        FROM fundamental WHERE date >= (SELECT MAX(date) FROM fundamental)""")
if r and r[0][0]:
    n, pe, mc, dy = r[0]
    out.append(f"  - 最新快照 {n} 行: pe {pe}, market_cap {mc}, dividend_yield {dy}")

out.append("\n## index_members 池")
for row in q("SELECT index_code, COUNT(*) FROM index_members GROUP BY index_code"):
    out.append(f"- {row[0]}: {row[1]} 条")

out.append("\n## 模拟选股(s26_microcap@v1 @ 2023-01-04 与 2025-06-04)")
buf = io.StringIO()
h = logging.StreamHandler(buf)
h.setLevel(logging.INFO)
logging.getLogger().addHandler(h)
try:
    cfg = conf.load_config(use_cache=False)
    reg = conf.load_registry()
    eng = E.Engine(config=cfg, registry=reg, conn=conn, cache_bars=True)
    stg = eng.get_strategy("s26_microcap@v1")
    from strategies import mf_core
    for d in ("2023-01-04", "2025-06-04"):
        ctx = eng.ctx(d)
        try:
            sel = mf_core.select(stg.strategy_id, stg.params, cfg, ctx, d)
            tgt = sel.get("target", [])
            out.append(f"- {d}: target {len(tgt)} 只; empty_reason={sel.get('empty_reason','')}")
        except Exception as e:
            out.append(f"- {d}: select 异常 {type(e).__name__}: {e}")
except Exception as e:
    out.append(f"- Engine 初始化异常 {type(e).__name__}: {e}")
logging.getLogger().removeHandler(h)
diag_lines = [l for l in buf.getvalue().splitlines() if "候选筛选" in l or "池" in l]
if diag_lines:
    out.append("\n### 选股诊断日志")
    for l in diag_lines[:20]:
        out.append(f"    {l}")

Path("reports").mkdir(exist_ok=True)
Path("reports/diag_cloud_db.md").write_text("\n".join(out) + "\n", encoding="utf-8")
print("\n".join(out))
