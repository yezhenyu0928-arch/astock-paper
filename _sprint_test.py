# -*- coding: utf-8 -*-
"""冲刺20%+ 候选批量回测: 数据就绪后一键跑 s38/s39/s40/s41/s42 主窗口(2022-01-01~今)。
每个策略独立子进程(避免 run_backtest 内存累积段错误), 输出 annual/max_dd/sharpe。
用法: python _sprint_test.py
"""
import subprocess, json, sys

PY = r"C:/Users/zhenyu/.workbuddy/binaries/python/versions/3.13.12/python.exe"
START, END, CAP = "2022-01-01", "2026-07-27", "100000"
CANDS = [
    "s38_earnings_event@v1",
    "s39_earnings_event50@v1",
    "s40_sue_momentum@v1",
    "s41_sue_high52@v1",
    "s42_sue_enriched@v1",
]

if __name__ == "__main__":
    for sid in CANDS:
        try:
            r = subprocess.run(
                [PY, "backtest_one.py", sid, START, END, CAP],
                capture_output=True, text=True, timeout=900)
            line = [l for l in r.stdout.strip().splitlines() if l.strip().startswith("{")]
            if not line:
                print(f"{sid}: NO_JSON  rc={r.returncode}  err={r.stderr.strip()[:300]}")
                continue
            m = json.loads(line[-1])
            print(f"{sid}: annual={m.get('annual')}  max_dd={m.get('max_dd')}  "
                  f"sharpe={m.get('sharpe')}  trades={m.get('trades')}")
        except Exception as e:
            print(f"{sid}: EXC {e}")
    print("DONE sprint test")
