# -*- coding: utf-8 -*-
"""选项B: 分析师评级/一致预期上调因子 + 涨停因子 组合实验 (全A股, 2022-2025)。
复用 s53 车(all_a 池, 无规模倾斜), 仅用 param_override 换 weights。
目标: 在 R2(17.8%) 基础上, 用 analyst+limit_up 把年化推过 30%~50%, 且回撤<=年化。

断点续跑版: 每组跑完立即写 results_alla7.json; 被杀后重跑自动跳过已完成组。
排序: 把最有戏的 B1 放第一个, 保证睡眠前优先出关键结果。
"""
import time, traceback, json, os
import backtest

START, END, CAP = "2022-01-01", "2025-12-31", 100_000
SID = "s53_all_a_momentum_smallcap@v1"
CKPT = "results_alla7.json"

# R2 基础配方(无规模倾斜, 已达标17.8%)
R2 = dict(momentum=0.12, high52=0.12, sue=0.18, valuation=0.18, value=0.10,
          roe=0.10, growth=0.15, dividend=0.05, limit_up=0.0, analyst=0.0)
BASE = dict(cap_segment="all", cap_tilt=False, cap=0.0, hold_n=10,
            max_per_industry=3, low_vol_pct=0.75, stop_pct=0.20)

def mk(weights):
    p = dict(BASE); p["weights"] = weights; return p

# 排序: B1(领先候选) 最前, 控制组 B0 最后
RECIPES = {
    "B1_an25_lu15":    mk(dict(R2, analyst=0.25, limit_up=0.15)),
    "B3_an35_dom":     mk(dict(momentum=0.10, high52=0.10, sue=0.15, valuation=0.10,
                               value=0.10, roe=0.05, growth=0.05, dividend=0.0,
                               limit_up=0.15, analyst=0.35)),
    "B5_an20_lu20_bal":mk(dict(momentum=0.10, high52=0.10, sue=0.20, valuation=0.10,
                               value=0.10, roe=0.05, growth=0.05, dividend=0.0,
                               limit_up=0.20, analyst=0.20)),
    "B2_an30_lu20":    mk(dict(R2, analyst=0.30, limit_up=0.20, growth=0.10)),
    "B4_an25_lu30":    mk(dict(sue=0.15, momentum=0.10, high52=0.10, valuation=0.10,
                               value=0.10, roe=0.00, growth=0.00, dividend=0.0,
                               limit_up=0.30, analyst=0.25)),
    "B0_R2_control":   mk(dict(R2)),
}

def load_ckpt():
    if os.path.exists(CKPT):
        try:
            with open(CKPT) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_ckpt(done):
    tmp = CKPT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(done, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CKPT)

def run_one(name, params):
    t = time.time()
    try:
        r = backtest.run_backtest(SID, START, END, capital=CAP, param_override=params)
        m = r.get("metrics") or {}
        ann = m.get("annual", 0) * 100
        dd = m.get("max_dd", 0) * 100
        sh = m.get("sharpe", 0)
        ok = (ann > 10 and dd <= ann)
        flag = "✅达标" if ok else ("⚠️回撤>年化" if ann > 10 else "❌")
        line = f"{name}: 年化 {ann:.1f}% / 回撤 {dd:.1f}% / 夏普 {sh:.2f}  {flag}  ({round(time.time()-t)}s)"
        print(line, flush=True)
        return {name: dict(ann=float(round(ann, 1)), dd=float(round(dd, 1)),
                           sharpe=float(round(sh, 2)), ok=bool(ok),
                           secs=int(round(time.time() - t)))}
    except Exception:
        print(f"{name}: 失败", flush=True)
        traceback.print_exc()
        return {name: dict(error=True)}

if __name__ == "__main__":
    done = load_ckpt()
    print(f"[checkpoint] 已有结果: {list(done.keys())}", flush=True)
    for name, p in RECIPES.items():
        if name in done:
            print(f"[skip] 已完成 {name}: {done[name]}", flush=True)
            continue
        print(f"[start] {name} ...", flush=True)
        res = run_one(name, p)
        done.update(res)
        save_ckpt(done)
        print(f"[checkpoint saved] {name}", flush=True)
    print("\n========== 实验结论 ==========", flush=True)
    for name, v in done.items():
        print(f"  {name}: {v}", flush=True)
