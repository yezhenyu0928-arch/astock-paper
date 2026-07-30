# -*- coding: utf-8 -*-
"""全A配方实验 exp_alla3 第二批: 跑除 R1 外的其余配方, 结果写日志。"""
import time
import exp_alla3 as E

# 第二批(跳过已跑的 R1)
BATCH = [("R0_size_baseline", E.R0), ("R2_barra_sue", E.R2), ("R3_quality_growth", E.R3),
         ("R4_breakout_mom", E.R4), ("R5_equal_weight", E.R5), ("R6_barra_midsmall", E.R6)]

print(f"=== 全A配方实验 第二批 {E.START}~{E.END} cap=¥{E.CAP:,} ===", flush=True)
results = []
for name, ov in BATCH:
    try:
        results.append(E.run_one(name, ov))
    except Exception as ex:
        print(f"{name:22s} ERROR {ex}", flush=True)
        results.append((name, None, None, None, None, False))
print("\n=== 汇总 ===", flush=True)
for name, ann, dd, sh, win, ok in results:
    print(f"{name:22s} ann={ann} dd={dd} sharpe={sh} pass={ok}", flush=True)
