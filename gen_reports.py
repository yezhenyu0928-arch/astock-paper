# -*- coding: utf-8 -*-
"""批量生成个股策略的五关验证报告 + 蒙特卡洛稳健性报告(reports/)。
数据须先 backfill。用法:python gen_reports.py [sid1 sid2 ...](默认 s1/s3/s4/s5)。"""
import sys
import io
import time
import logging

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.ERROR)
import backtest
import validate

SIDS = sys.argv[1:] or ["s3_ma_trend@v1", "s1_dividend@v1", "s4_smallcap@v1", "s5_grid@v1"]

for sid in SIDS:
    t0 = time.time()
    try:
        p1, met = backtest.five_pass_report(sid)
        print(f"[五关] {sid}: {backtest._fmt(met)} -> {p1} ({time.time()-t0:.0f}s)", flush=True)
    except Exception as e:
        import traceback
        print(f"[五关FAIL] {sid}: {e}", flush=True); traceback.print_exc()
    t0 = time.time()
    try:
        p2, r = validate.report(sid)
        print(f"[验证] {sid}: 判定={r['verdict']} | {r['reason']} -> {p2} ({time.time()-t0:.0f}s)", flush=True)
    except Exception as e:
        import traceback
        print(f"[验证FAIL] {sid}: {e}", flush=True); traceback.print_exc()
print("== 全部报告生成完毕 ==", flush=True)
