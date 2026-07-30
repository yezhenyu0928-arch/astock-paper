# -*- coding: utf-8 -*-
"""救援第2名全A配方(exp_alla4): 基于 R5(等权分散11.6%/12.2%) / R3(质量成长12.7%/15.9%) 控回撤+提年化,
以及 R2 稳健版(R2b), 找第2个达标(年化>10%且回撤<=年化)且风格有差异的全A策略。"""
import time
import backtest as BT

SID = "s54_all_a_industry_mom@v1"   # 仅作 all_a 车, 全量 param_override
START, END = "2022-01-01", "2025-12-31"
CAP = 100000

BASE = dict(
    pool_index="all_a",
    cap_segment="all",         # 取消 small 市值段切割
    cap_tilt=False,            # 取消小盘倾斜
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
)

# R5 等权分散(原: 11.6%/12.2%)
R5 = dict(weights={"momentum":0.11,"dividend":0.11,"value":0.11,"valuation":0.11,"sue":0.11,
                   "roe":0.11,"growth":0.11,"high52":0.11,"low_vol":0.12})
# R3 质量成长(原: 12.7%/15.9%)
R3 = dict(weights={"roe":0.20,"growth":0.25,"valuation":0.15,"value":0.10,"momentum":0.10,
                   "sue":0.12,"high52":0.08})
# R2 配方(达标17.8%/15.2%)
R2 = dict(weights={"momentum":0.12,"high52":0.12,"sue":0.18,"valuation":0.18,"value":0.10,
                   "roe":0.10,"growth":0.15,"dividend":0.05})

# 救援1: R5 + 强低波预筛(压回撤)
R5b = dict(low_vol_pct=0.40, weights=R5["weights"])
# 救援2: R5 + 紧止损(压回撤)
R5c = dict(stop_pct=0.15, weights=R5["weights"])
# 救援3: R3 + 综合控回撤(紧止损+低波+熊市降仓+更分散)
R3b = dict(stop_pct=0.15, low_vol_pct=0.45, regime_downsize=True, hold_n=12, max_per_industry=2,
           weights=R3["weights"])
# 救援4: R2 稳健版(低波预筛+更多持仓, 降低回撤档位)
R2b = dict(low_vol_pct=0.45, hold_n=12, max_per_industry=2, weights=R2["weights"])

RECIPES = [("R5b_lowvol", R5b), ("R5c_tightstop", R5c), ("R3b_ctrl", R3b), ("R2b_steady", R2b)]


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
    print(f"{name:16s} 年化{ann:6.1f}%  回撤{dd:5.1f}%  夏普{sh:4.2f}  胜率{win:4.1f}%  {flag}  ({time.time()-t0:.0f}s)", flush=True)
    return (name, ann, dd, sh, win, ok)


if __name__ == "__main__":
    print(f"=== 救援第2名全A配方 {START}~{END} cap=¥{CAP:,} ===", flush=True)
    results = []
    for name, ov in RECIPES:
        try:
            results.append(run_one(name, ov))
        except Exception as ex:
            print(f"{name:16s} ERROR {ex}", flush=True)
            results.append((name, None, None, None, None, False))
    print("\n=== 汇总 ===", flush=True)
    for name, ann, dd, sh, win, ok in results:
        print(f"{name:16s} ann={ann} dd={dd} sharpe={sh} pass={ok}", flush=True)
