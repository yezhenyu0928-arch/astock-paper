# -*- coding: utf-8 -*-
"""全A股策略配方实验(exp_alla3)。

依据 Barra CNE6 长期模型 alpha 权重(报告 line 1286):
  Momentum/DividendYield/BookToPrice/EarningsQuality/Profitability/LongTermReversal 各10%,
  Growth/EarningsYield 各20%  —— 完全不含 Size(规模)倾斜。

此前 s53/s54/s55 全部 cap_segment=small + cap_tilt=true + cap:0.30 小盘倾斜,
这应是全A崩盘(年化0.87%~6.63%)的根因。本实验关掉规模倾斜, 用均衡多因子配方试多组。

用法: python exp_alla3.py
"""
import time
import backtest as BT

SID = "s53_all_a_momentum_smallcap@v1"   # 注册表内 pool_index=all_a, 仅用 param_override 换配方
START, END = "2022-01-01", "2025-12-31"
CAP = 100000

# 公共覆盖: 关掉规模倾斜(关键), 放宽门槛(全A广泛性), 月频再平衡
BASE = dict(
    pool_index="all_a",
    cap_segment="all",         # 取消 small 市值段切割
    cap_tilt=False,            # 取消小盘倾斜排名
    cap=0.0,                   # 取消 Size 权重
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
)

# R0: 规模倾斜基线(复刻此前 s53+SUE, cap0.30+small) —— 用于对比
R0 = dict(cap_segment="small", cap_tilt=True, cap=0.30, value_tilt=True,
          weights={k: 0.0 for k in ["dividend","low_vol","roe","valuation","value","momentum","growth","sue","high52","ind_mom","industry","news"]}
          | {"cap":0.30,"valuation":0.15,"value":0.15,"momentum":0.20,"sue":0.10,"high52":0.10,"low_vol":0.05,"roe":0.05})

# R1: Barra 忠实复刻(无规模倾斜)
R1 = dict(weights={"dividend":0.10,"roe":0.10,"value":0.10,"sue":0.10,"momentum":0.10,
                   "high52":0.10,"valuation":0.20,"growth":0.20})

# R2: Barra + SUE 强化(SUE 是此前主板16点胜负手)
R2 = dict(weights={"momentum":0.12,"high52":0.12,"sue":0.18,"valuation":0.18,"value":0.10,
                   "roe":0.10,"growth":0.15,"dividend":0.05})

# R3: 质量+成长主导(弱化分红/价值)
R3 = dict(weights={"roe":0.20,"growth":0.25,"valuation":0.15,"value":0.10,"momentum":0.10,
                   "sue":0.12,"high52":0.08})

# R4: 52周新高突破 + 动量主导(经典 A 股强势股策略)
R4 = dict(weights={"high52":0.25,"momentum":0.20,"sue":0.15,"valuation":0.15,"value":0.10,
                   "roe":0.08,"growth":0.07})

# R5: 等权深度分散(9因子各~0.11, 低波兜底)
R5 = dict(weights={"momentum":0.11,"dividend":0.11,"value":0.11,"valuation":0.11,"sue":0.11,
                   "roe":0.11,"growth":0.11,"high52":0.11,"low_vol":0.12})

# R6: Barra 忠实但市值段取 midsmall(避开极小盘, 保留中等偏小)
R6 = dict(cap_segment="midsmall",
          weights={"dividend":0.10,"roe":0.10,"value":0.10,"sue":0.10,"momentum":0.10,
                   "high52":0.10,"valuation":0.20,"growth":0.20})

RECIPES = [("R0_size_baseline", R0), ("R1_barra_faithful", R1), ("R2_barra_sue", R2),
           ("R3_quality_growth", R3), ("R4_breakout_mom", R4), ("R5_equal_weight", R5),
           ("R6_barra_midsmall", R6)]


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
    print(f"=== 全A配方实验 {START}~{END} cap=¥{CAP:,} ===", flush=True)
    results = []
    for name, ov in RECIPES:
        results.append(run_one(name, ov))
    print("\n=== 汇总 ===", flush=True)
    for name, ann, dd, sh, win, ok in results:
        print(f"{name:22s} ann={ann:6.1f}% dd={dd:5.1f}% sharpe={sh:4.2f} pass={ok}", flush=True)
