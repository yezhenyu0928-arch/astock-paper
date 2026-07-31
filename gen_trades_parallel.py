# -*- coding: utf-8 -*-
"""并行生成剩余策略的回测成交流水(2022-01-01 -> 2026-07-30)。
每个 sid 起一个独立 python 子进程跑 gen_one_trade.py,父进程等待全部完成。
"""
import subprocess, sys

SIDS = ["s32_roe_quality@v1", "s37_earnings_accel@v1",
        "s42_sue_enriched@v1", "s53_all_a_momentum_smallcap@v1"]
END = "2026-07-30"
py = sys.executable

procs = []
for sid in SIDS:
    p = subprocess.Popen([py, "-u", "gen_one_trade.py", sid, END],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    procs.append((sid, p))
    print(f"[spawn] {sid} pid={p.pid}", flush=True)

for sid, p in procs:
    out_b, _ = p.communicate()
    txt = out_b.decode(errors="replace").strip().splitlines()
    last = txt[-1] if txt else ""
    print(f"[result] {sid}: {last}", flush=True)

print("== 并行回测成交生成完毕 ==")
