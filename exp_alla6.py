# -*- coding: utf-8 -*-
"""冲50%主线实验(exp_alla6): 新增"涨停因子"(题材强势代理) 融入月频选股。
基于此前 R2=17.8%(全A多因子天花板) 之上叠加涨停因子, 验证能否把全A年化推过30%。
基准利率 = R2 原权重(控制组), 其余为涨停因子增强组。
硬约束: 年化>10% 且 最大回撤<=年化; 目标线=冲50%。
"""
import time
import backtest as BT

SID = "s53_all_a_momentum_smallcap@v1"   # all_a 车(已固化 R2 配方), 全量 param_override
START, END = "2022-01-01", "2025-12-31"
CAP = 100000

BASE = dict(
    pool_index="all_a",
    cap_segment="all",
    cap_tilt=False,
    cap=0.0,
    value_tilt=True,
    min_dividend_yield=0.0,
    dividend_years=0,
    roe_years=0,
    roe_min=0.0,
    hold_n=10,
    max_per_industry=3,
    low_vol_pct=0.75,
    momentum_window=252,
    momentum_skip=21,
    momentum_min=-1.0,
    regime_downsize=False,
    stop_pct=0.20,
    event_pool=False,
)

# R2 基准(控制组, 应复现 ~17.8%)
R2 = dict(weights={"momentum":0.12,"high52":0.12,"sue":0.18,"valuation":0.18,
                   "value":0.10,"roe":0.10,"growth":0.15,"dividend":0.05})
# Z1: R2 + 涨停 0.20(替代部分估值/价值权重)
Z1 = dict(weights={"momentum":0.12,"high52":0.12,"sue":0.18,"valuation":0.13,
                   "value":0.05,"roe":0.10,"growth":0.10,"limit_up":0.20})
# Z2: 极致强势(涨停+动量+52周新高 主导)
Z2 = dict(weights={"limit_up":0.30,"momentum":0.25,"high52":0.25,"sue":0.10,
                   "valuation":0.10})
# Z3: 涨停主导 + 价值压舱(题材+质量均衡)
Z3 = dict(weights={"limit_up":0.35,"valuation":0.20,"value":0.20,"momentum":0.25})
# Z4: 高集中度(持仓5) + 涨停主导(锐度更高)
Z4 = dict(hold_n=5, max_per_industry=2,
          weights={"limit_up":0.35,"momentum":0.25,"high52":0.20,"sue":0.10,"valuation":0.10})
# Z5: 涨停60日窗口版(更长的题材记忆)
Z5 = dict(weights={"limit_up60":0.10,"limit_up":0.20,"momentum":0.15,"high52":0.15,
                   "sue":0.15,"valuation":0.15,"value":0.10})

RECIPES = [("R2_base", R2), ("Z1_r2_plus_limitup", Z1), ("Z2_momentum_limitup", Z2),
           ("Z3_limitup_value", Z3), ("Z4_concentrated", Z4), ("Z5_lu60", Z5)]


def run_one(name, ov):
    p = dict(BASE)
    p.update(ov)
    t0 = time.time()
    r = BT.run_backtest(SID, START, END, capital=CAP, param_override=p)
    m = r["metrics"]
    ann = m["annual"] * 100
    dd = m["max_dd"] * 100
    sh = m["sharpe"]
    win = m["win"] * 100
    ok = (ann > 10 and dd <= ann)
    flag = "✅达标" if ok else ("⚠️回撤>年化" if ann > 10 else "❌年化不足")
    print(f"{name:22s} 年化{ann:6.1f}%  回撤{dd:5.1f}%  夏普{sh:4.2f}  胜率{win:4.1f}%  {flag}  ({time.time()-t0:.0f}s)", flush=True)
    return (name, ann, dd, sh, win, ok)


if __name__ == "__main__":
    print(f"=== 涨停因子实验: 全A {START}~{END} cap=¥{CAP:,} ===", flush=True)
    results = []
    for name, ov in RECIPES:
        try:
            results.append(run_one(name, ov))
        except Exception as ex:
            print(f"{name:22s} ERROR {ex}", flush=True)
            results.append((name, None, None, None, None, False))
    print("\n=== 汇总 ===", flush=True)
    for name, ann, dd, sh, win, ok in results:
        print(f"{name:22s} ann={ann} dd={dd} sharpe={sh} pass={ok}", flush=True)
