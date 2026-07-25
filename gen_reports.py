# -*- coding: utf-8 -*-
"""批量生成个股策略的五关验证报告 + 蒙特卡洛稳健性报告(reports/)。
数据须先 backfill。用法:python gen_reports.py [sid1 sid2 ...](默认读取 registry.yaml 全量已注册sid,卡L.4)。"""
import sys
import io
import time
import logging

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.ERROR)
import conf
import util
import backtest
import validate

SIDS = sys.argv[1:] or sorted(conf.load_registry().keys())   # 卡L.4:默认sid动态读registry全量,不再手写过期列表
TODAY = util.today_str()

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
    # 导出该策略的全部回测成交流水(2022→今)
    t0 = time.time()
    try:
        out = str(conf.REPORTS_DIR / f"{sid.replace('@','_at_')}_trades.csv")
        r = backtest.run_backtest(sid, "2022-01-01", TODAY, trades_out=out)
        import csv as _csv
        n = sum(1 for _ in open(out, encoding="utf-8")) - 1 if __import__("os").path.exists(out) else 0
        print(f"[流水] {sid}: {n}笔 -> {out} ({time.time()-t0:.0f}s)", flush=True)
    except Exception as e:
        import traceback
        print(f"[流水FAIL] {sid}: {e}", flush=True); traceback.print_exc()
print("== 全部报告+流水生成完毕 ==", flush=True)
