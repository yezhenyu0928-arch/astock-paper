# -*- coding: utf-8 -*-
"""冲50%主线实验(exp_alla5): 基于国信"超预期精选(36%)/成长稳健(41%)"的日线可复现近似。
核心武器 = mf_core 内置 event_pool 事件门控(近期盈余公告且SUE>0) + SUE 极致化 + 高集中度。
此前 R2=17.8% 根本没开 event_pool, 本实验补上并强化。

硬约束(用户铁律): 年化>10% 且 最大回撤<=年化。但目标线已升到"冲50%",
所以本实验优先看 SUE/事件 主线的收益上限, 回撤<=年化 作为达标门槛。
"""
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
    event_pool=True,           # ★ 超预期事件门控(国信36%核心近似)
    event_window=60,           # 公告后60自然日内视为"黄金期"
)

# Y1: 超预期事件 + SUE主导(复现国信超预期精选骨架)
Y1 = dict(weights={"sue":0.40,"momentum":0.20,"high52":0.20,"valuation":0.10,"value":0.10})
# Y2: 超预期 + 高集中度锐度(hold5, 更激进)
Y2 = dict(hold_n=5, max_per_industry=2,
          weights={"sue":0.45,"momentum":0.25,"high52":0.15,"value":0.15})
# Y3: 超预期 + 成长(国信成长稳健41%思路)
Y3 = dict(weights={"sue":0.30,"growth":0.25,"momentum":0.20,"valuation":0.15,"roe":0.10})
# Y4: R2 原权重 + event_pool(对照: 看事件门控对 R2 的提升)
Y4 = dict(weights={"momentum":0.12,"high52":0.12,"sue":0.18,"valuation":0.18,"value":0.10,
                   "roe":0.10,"growth":0.15,"dividend":0.05})
# Y5: 纯 SUE 权重 无 event_pool(对照: event_pool 是否真带来增益)
Y5 = dict(event_pool=False,
          weights={"sue":0.45,"momentum":0.20,"high52":0.15,"valuation":0.10,"value":0.10})

RECIPES = [("Y1_event_sue", Y1), ("Y2_event_concentrated", Y2), ("Y3_event_growth", Y3),
           ("Y4_r2_event", Y4), ("Y5_pure_sue_noevent", Y5)]


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
    print(f"{name:20s} 年化{ann:6.1f}%  回撤{dd:5.1f}%  夏普{sh:4.2f}  胜率{win:4.1f}%  {flag}  ({time.time()-t0:.0f}s)", flush=True)
    return (name, ann, dd, sh, win, ok)


if __name__ == "__main__":
    print(f"=== 冲50%主线: 超预期事件门控+SUE {START}~{END} cap=¥{CAP:,} ===", flush=True)
    results = []
    for name, ov in RECIPES:
        try:
            results.append(run_one(name, ov))
        except Exception as ex:
            print(f"{name:20s} ERROR {ex}", flush=True)
            results.append((name, None, None, None, None, False))
    print("\n=== 汇总 ===", flush=True)
    for name, ann, dd, sh, win, ok in results:
        print(f"{name:20s} ann={ann} dd={dd} sharpe={sh} pass={ok}", flush=True)
