# -*- coding: utf-8 -*-
"""多臂参数搜索(multi-arm parameter search)——达标硬目标: 年化>10% & 最大回撤<10%。

背景: 6 个 @v3 策略共用 mf_core 选股 + macro.macro_exposure_mult 大盘趋势闸控总仓。
2026-07-24 把 macro_exposure_mult 重构成真正的"见顶清仓/见底满仓"择时开关, 其四个阈值
可由环境变量覆盖:
    MACRO_BASELINE   上升趋势基线仓位   (默认 0.85)
    MACRO_BEAR_FLOOR 风险市地板仓位     (默认 0.15)
    MACRO_WEAK_MULT  转弱/破MA50 仓位   (默认 0.45)
    MACRO_MA200_MULT 仅破MA200 仓位     (默认 0.65)

本脚本对每个策略扫多组阈值(每组=一个"臂"), 跑主回测(2022-01-01~今)比对达标情况,
为每个策略挑出最优臂(优先达标; 否则取综合分最高), 然后用该臂环境变量重生成完整五关报告
(reports/{sid}.md) + 蒙特卡洛(validate.py), 供看板展示。

用法:
    python search_arms.py                 # 全臂 × 6 策略 主回测搜参 + 最优臂重生成完整报告
    python search_arms.py --quick         # 只跑前 3 个臂(快速验证)
    python search_arms.py --no-final      # 只搜参出榜, 不重生成完整五关报告
    python search_arms.py s4_smallcap@v3  # 只搜某几个策略(可跟多个 sid)

产出:
    reports/_arm_search.md                最优臂榜单 + 每臂每策略 年化/回撤 明细
    reports/{sid}.md / {sid}_validate.md  最优臂下的完整五关 + 蒙卡(--no-final 时跳过)
"""
import os
import sys
import json
import logging
import importlib

logging.basicConfig(level=logging.ERROR, format="%(levelname)s|%(name)s|%(message)s")
log = logging.getLogger("search_arms")

# 达标硬目标
TARGET_ANNUAL = 0.10
TARGET_MAXDD = 0.10

# 6 个参赛策略(@v3)
DEFAULT_SIDS = [
    "s1_dividend@v3",
    "s15_core_allocation@v3",
    "s8_checklist@v3",
    "s4_smallcap@v3",
    "s13_growth_quality_rotation@v3",
    "s14_value_reversal_rotation@v3",
]

# ── 参数臂(env 覆盖 macro_exposure_mult 的四阈值) ──
# 从"防御"到"进取"梯度排列; 达标 = 年化>10% & 回撤<10%。
ARMS = [
    {"name": "A_base",  "MACRO_BASELINE": "0.85", "MACRO_BEAR_FLOOR": "0.15", "MACRO_WEAK_MULT": "0.45", "MACRO_MA200_MULT": "0.65"},
    {"name": "B_defensive", "MACRO_BASELINE": "0.80", "MACRO_BEAR_FLOOR": "0.10", "MACRO_WEAK_MULT": "0.35", "MACRO_MA200_MULT": "0.55"},
    {"name": "C_aggressive", "MACRO_BASELINE": "0.90", "MACRO_BEAR_FLOOR": "0.20", "MACRO_WEAK_MULT": "0.55", "MACRO_MA200_MULT": "0.75"},
    {"name": "D_ultradef", "MACRO_BASELINE": "0.85", "MACRO_BEAR_FLOOR": "0.05", "MACRO_WEAK_MULT": "0.30", "MACRO_MA200_MULT": "0.50"},
    {"name": "E_mid",   "MACRO_BASELINE": "0.88", "MACRO_BEAR_FLOOR": "0.12", "MACRO_WEAK_MULT": "0.40", "MACRO_MA200_MULT": "0.60"},
]

ENV_KEYS = ["MACRO_BASELINE", "MACRO_BEAR_FLOOR", "MACRO_WEAK_MULT", "MACRO_MA200_MULT"]


def _apply_arm(arm):
    for k in ENV_KEYS:
        os.environ[k] = str(arm[k])


def _clear_arm_env():
    for k in ENV_KEYS:
        os.environ.pop(k, None)


def _score(annual, max_dd):
    """综合分: 达标优先(both pass → 大加分), 否则按 年化 - 回撤惩罚 排序。
    回撤是硬约束, 权重更高; 达标者额外 +100 保证排在最前。"""
    passed = (annual > TARGET_ANNUAL) and (max_dd < TARGET_MAXDD)
    base = annual * 100.0 - max(0.0, max_dd - TARGET_MAXDD) * 300.0
    return base + (100.0 if passed else 0.0), passed


def _run_main(sid, capital):
    """跑主回测(2022-01-01~今), 返回 metrics dict。失败返回 None。

    改用独立子进程调用 backtest_one.py: 每次回测在独立解释器内运行,
    内存不跨调用累积, 且若某次回测段错误(C 扩展崩, 如之前
    0xC0000005 / 退出码 3221225477)只会拖垮子进程, 主进程捕获
    stderr 后跳过该组合, 不会让整个搜索崩溃(之前同进程连续跑
    18~30 次 run_backtest, cache_bars 把全市场日线缓存进内存叠加,
    最终内存爆掉段错误, 连 _arm_search.md 都写不出)。"""
    import subprocess
    import util
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(
            [sys.executable, "backtest_one.py", sid, "2022-01-01",
             util.today_str(), str(int(capital))],
            cwd=here, capture_output=True, text=True, timeout=2400)
        if out.returncode != 0:
            log.error("backtest_one 失败 %s rc=%s: %s", sid, out.returncode, out.stderr[-2000:])
            return None
        js = [l for l in out.stdout.splitlines() if l.strip()]
        if not js:
            log.error("backtest_one 无输出 %s", sid)
            return None
        return json.loads(js[-1])
    except Exception as e:
        log.error("run_backtest 失败 %s: %s", sid, e)
        return None


def main():
    args = [a for a in sys.argv[1:]]
    quick = "--quick" in args
    no_final = "--no-final" in args
    args = [a for a in args if not a.startswith("--")]
    sids = args if args else DEFAULT_SIDS
    arms = ARMS[:3] if quick else ARMS

    import conf
    capital = conf.load_config()["user"]["capital"]

    print(f"=== 多臂搜参开始 | 资金档 {capital:,.0f} | 目标 年化>{TARGET_ANNUAL:.0%} & 回撤<{TARGET_MAXDD:.0%} ===")
    print(f"策略 {len(sids)} 个 × 臂 {len(arms)} 组 = {len(sids)*len(arms)} 次主回测\n")

    # results[sid][arm_name] = metrics
    results = {sid: {} for sid in sids}
    for arm in arms:
        _apply_arm(arm)
        print(f"--- 臂 {arm['name']}: baseline={arm['MACRO_BASELINE']} floor={arm['MACRO_BEAR_FLOOR']} "
              f"weak={arm['MACRO_WEAK_MULT']} ma200={arm['MACRO_MA200_MULT']} ---")
        for sid in sids:
            m = _run_main(sid, capital)
            if m is None:
                print(f"    {sid:38s}  跑测失败")
                continue
            results[sid][arm["name"]] = m
            sc, passed = _score(m["annual"], m["max_dd"])
            flag = "✅达标" if passed else "  "
            print(f"    {sid:38s}  年化{m['annual']:+6.1%}  回撤{m['max_dd']:5.1%}  "
                  f"Sharpe{m['sharpe']:+5.2f}  分{sc:+6.1f} {flag}")
    _clear_arm_env()

    # 为每个策略挑最优臂
    best = {}
    for sid in sids:
        cands = []
        for arm in arms:
            m = results[sid].get(arm["name"])
            if not m:
                continue
            sc, passed = _score(m["annual"], m["max_dd"])
            cands.append((sc, passed, arm, m))
        if not cands:
            best[sid] = None
            continue
        cands.sort(key=lambda x: x[0], reverse=True)
        best[sid] = cands[0]

    # ── 榜单 ──
    lines = ["# 多臂参数搜索榜单", "",
             f"> 目标: 年化>{TARGET_ANNUAL:.0%} 且 最大回撤<{TARGET_MAXDD:.0%} (硬约束)。资金档 {capital:,.0f} 元。",
             f"> 主回测窗口 2022-01-01~今。搜参维度: macro_exposure_mult 大盘趋势闸四阈值。", "",
             "## 各策略最优臂", "",
             "| 策略 | 最优臂 | 年化 | 最大回撤 | Sharpe | 达标 |",
             "|---|---|---|---|---|---|"]
    n_pass = 0
    for sid in sids:
        b = best[sid]
        if not b:
            lines.append(f"| {sid} | — | — | — | — | ❌无数据 |")
            continue
        _sc, passed, arm, m = b
        n_pass += 1 if passed else 0
        lines.append(f"| {sid} | {arm['name']} | {m['annual']:+.1%} | {m['max_dd']:.1%} | "
                     f"{m['sharpe']:+.2f} | {'✅' if passed else '❌'} |")
    lines += ["", f"**达标进度: {n_pass} / {len(sids)}**", "",
              "## 全臂明细", ""]
    for sid in sids:
        lines.append(f"### {sid}")
        lines.append("| 臂 | 年化 | 最大回撤 | Sharpe | 综合分 |")
        lines.append("|---|---|---|---|---|")
        for arm in arms:
            m = results[sid].get(arm["name"])
            if not m:
                lines.append(f"| {arm['name']} | 失败 | — | — | — |")
                continue
            sc, _ = _score(m["annual"], m["max_dd"])
            lines.append(f"| {arm['name']} | {m['annual']:+.1%} | {m['max_dd']:.1%} | {m['sharpe']:+.2f} | {sc:+.1f} |")
        lines.append("")

    import conf as _c
    out = _c.REPORTS_DIR / "_arm_search.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n=== 榜单已写出: {out} | 达标 {n_pass}/{len(sids)} ===")

    # 记录最优臂 env(给 run_local / CI 复用)
    best_env = {sid: (best[sid][2] if best[sid] else None) for sid in sids}
    (_c.REPORTS_DIR / "_arm_best.json").write_text(
        json.dumps(best_env, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 用最优臂重生成完整五关 + 蒙卡(供看板) ──
    if no_final:
        print("--no-final: 跳过完整五关重生成。")
        return
    print("\n=== 用各策略最优臂重生成完整五关报告 + 蒙特卡洛 ===")
    import backtest
    import subprocess
    for sid in sids:
        b = best[sid]
        if not b:
            continue
        arm = b[2]
        _apply_arm(arm)
        try:
            path, met = backtest.five_pass_report(sid, capital=capital)
            print(f"  {sid}: 五关报告 {path} | 主回测 年化{met['annual']:+.1%} 回撤{met['max_dd']:.1%} (臂{arm['name']})")
        except Exception as e:
            print(f"  {sid}: 五关报告失败 {e}")
        # 蒙特卡洛(独立进程, 沿用当前 env)
        try:
            subprocess.run([sys.executable, "validate.py", sid], check=False,
                           cwd=os.path.dirname(os.path.abspath(__file__)))
        except Exception as e:
            print(f"  {sid}: validate 失败 {e}")
    _clear_arm_env()
    print("=== 完成。看板可读取 reports/*.md ===")


if __name__ == "__main__":
    main()
