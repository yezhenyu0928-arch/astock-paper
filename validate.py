# -*- coding: utf-8 -*-
"""稳健性验证(SPEC P15 + SPEC_FILL 二次加压)。
- 蒙特卡洛(区块自助法,默认200次):重采样日收益重建净值路径,给出指标5%分位(悲观下界)。
- 双重成本加压:滑点×2 + 冲击×2 + 参与量×0.5 再回测。
- 入池判据:双重加压下 + 蒙卡5%分位 总收益>0 且 Calmar>0(不看均值看下界,拒绝过拟合)。
用法:python validate.py [sid]"""
import sys
import logging
import numpy as np

import conf
import util
import backtest as bt

log = logging.getLogger("validate")


def monte_carlo(navs, n=200, block=5, seed=42):
    """区块自助法:保留自相关地重采样日收益,返回指标分布的分位数。"""
    navs = np.asarray(navs, dtype=float)
    if len(navs) < block + 2:
        return None
    rets = navs[1:] / navs[:-1] - 1
    rng = np.random.default_rng(seed)
    L = len(rets)
    n_blocks = L // block + 1
    totals, calmars, maxdds = [], [], []
    for _ in range(n):
        idx = rng.integers(0, L - block, size=n_blocks)
        seq = np.concatenate([rets[i:i + block] for i in idx])[:L]
        path = np.cumprod(1 + seq)
        path = np.insert(path, 0, 1.0)
        m = bt.compute_metrics(path)
        totals.append(m["total"]); calmars.append(m["calmar"]); maxdds.append(m["max_dd"])
    def pct(a, p):
        return float(np.percentile(a, p))
    return {
        "total_p5": pct(totals, 5), "total_p50": pct(totals, 50), "total_p95": pct(totals, 95),
        "calmar_p5": pct(calmars, 5), "calmar_p50": pct(calmars, 50),
        "maxdd_p95": pct(maxdds, 95), "n": n, "block": block,
    }


def validate(sid, start="2022-01-01", end=None, capital=None, n=200):
    end = end or util.today_str()
    cfg = conf.load_config()
    capital = capital or cfg["user"]["capital"]
    base = bt.run_backtest(sid, start, end, capital=capital)
    mc = monte_carlo(base["navs"], n=n)

    stressed = bt.run_backtest(
        sid, start, end, capital=capital,
        cost_override={"slippage": {"etf": cfg["costs"]["slippage"]["etf"] * 2,
                                    "stock": cfg["costs"]["slippage"]["stock"] * 2}},
        custom_override={"impact_k": {"stock": cfg["custom"]["impact_k"]["stock"] * 2,
                                      "etf": cfg["custom"]["impact_k"]["etf"] * 2},
                        "open_frac": {"stock": cfg["custom"]["open_frac"]["stock"] * 0.5,
                                      "etf": cfg["custom"]["open_frac"]["etf"] * 0.5}})
    mc_stress = monte_carlo(stressed["navs"], n=n)

    verdict = "观察"
    reason = "数据不足"
    if mc and mc_stress:
        pass_gate = (mc_stress["total_p5"] > 0 and mc_stress["calmar_p5"] > 0)
        verdict = "入池" if pass_gate else "观察"
        reason = (f"双压蒙卡5%分位:总收益{mc_stress['total_p5']:+.1%} Calmar{mc_stress['calmar_p5']:.2f}"
                  f"(>0且>0 则入池)")
    return {"sid": sid, "base": base["metrics"], "mc": mc, "stressed": stressed["metrics"],
            "mc_stress": mc_stress, "verdict": verdict, "reason": reason}


def report(sid, out_path=None, **kw):
    r = validate(sid, **kw)
    L = [f"# {sid} 稳健性验证(蒙特卡洛+双重成本加压)", "",
         f"- 生成日:{util.today_str()}  判据:双压下蒙卡5%分位 总收益>0 且 Calmar>0", ""]
    L.append(f"## 基准回测\n- {bt._fmt(r['base'])}\n")
    if r["mc"]:
        m = r["mc"]
        L.append("## 蒙特卡洛(区块自助 {n}次,块{block}日)".format(**m))
        L.append(f"- 总收益 5%/50%/95%分位: {m['total_p5']:+.1%} / {m['total_p50']:+.1%} / {m['total_p95']:+.1%}")
        L.append(f"- Calmar 5%/50%分位: {m['calmar_p5']:.2f} / {m['calmar_p50']:.2f};最大回撤95%分位 {m['maxdd_p95']:.1%}\n")
    L.append(f"## 双重成本加压回测\n- {bt._fmt(r['stressed'])}")
    if r["mc_stress"]:
        m = r["mc_stress"]
        L.append(f"- 双压蒙卡:总收益5%分位{m['total_p5']:+.1%} 50%分位{m['total_p50']:+.1%};Calmar5%分位{m['calmar_p5']:.2f}\n")
    L.append(f"## 结论:**{r['verdict']}**\n- {r['reason']}")
    text = "\n".join(L)
    out_path = out_path or (conf.REPORTS_DIR / f"{sid.replace('@','_at_')}_validate.md")
    from pathlib import Path
    Path(out_path).write_text(text, encoding="utf-8")
    return str(out_path), r


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    sid = sys.argv[1] if len(sys.argv) > 1 else "s2_etf@v1"
    path, r = report(sid)
    print("验证报告:", path)
    print(f"{sid}: 判定={r['verdict']} | {r['reason']}")
