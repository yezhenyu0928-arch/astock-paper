# -*- coding: utf-8 -*-
"""历史回放回测(SPEC 模块3):复用 Engine 循环 settle+run_strategies,不另写引擎。
产出净值曲线 + 指标(Calmar/Sharpe/最大回撤/年化/胜率/超额)。
提供五关验证报告生成 five_pass_report()。"""
import os
import copy
import shutil
import logging
import tempfile
from pathlib import Path

import numpy as np

import conf
import util
import trade_calendar as cal
import engine as E
from db import get_conn

log = logging.getLogger("backtest")


# ---------------- 指标 ----------------
def compute_metrics(navs, bench_navs=None):
    navs = np.asarray(navs, dtype=float)
    if len(navs) < 2:
        return {"total": 0, "annual": 0, "max_dd": 0, "calmar": 0, "sharpe": 0, "win": 0, "excess": 0, "days": len(navs)}
    rets = navs[1:] / navs[:-1] - 1
    n = len(navs)
    years = n / 252.0
    total = navs[-1] / navs[0] - 1
    annual = navs[-1] / navs[0] ** 1  # placeholder
    annual = (navs[-1] / navs[0]) ** (1 / years) - 1 if years > 0 else 0.0
    peak = np.maximum.accumulate(navs)
    dd = 1 - navs / peak
    max_dd = float(dd.max())
    calmar = (annual / max_dd) if max_dd > 1e-9 else 0.0
    sd = rets.std(ddof=1)
    sharpe = float(rets.mean() / sd * np.sqrt(252)) if sd > 1e-12 else 0.0
    win = float((rets > 0).mean())
    excess = 0.0
    if bench_navs is not None and len(bench_navs) >= 2:
        bench_navs = np.asarray(bench_navs, dtype=float)
        excess = total - (bench_navs[-1] / bench_navs[0] - 1)
    return {"total": total, "annual": annual, "max_dd": max_dd, "calmar": calmar,
            "sharpe": sharpe, "win": win, "excess": excess, "days": n}


def _benchmark_navs(conn, bench_code, dates):
    """基准净值(对齐 dates,起点归一)。"""
    if not bench_code:
        return None
    rows = conn.execute(
        "SELECT trade_date, close FROM daily_bar WHERE code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (bench_code, dates[0], dates[-1])).fetchall()
    if not rows:
        return None
    m = {r[0]: r[1] for r in rows}
    base = None
    out = []
    last = 1.0
    for d in dates:
        if d in m:
            if base is None:
                base = m[d]
            last = m[d] / base
        out.append(last)
    return out if base else None


# ---------------- 单次回测 ----------------
def run_backtest(sid, start, end, capital=None, param_override=None,
                 cost_override=None, custom_override=None, keep_state=False):
    """返回 {dates, navs, metrics, bench_navs, trade_log_path, state_dir}。"""
    base_cfg = conf.load_config(use_cache=False)
    cfg = copy.deepcopy(base_cfg)
    cfg["strategies"] = {sid: True}
    if capital:
        cfg["user"]["capital"] = capital
    if cost_override:
        _deep_update(cfg["costs"], cost_override)
    if custom_override:
        _deep_update(cfg["custom"], custom_override)

    reg = copy.deepcopy(conf.load_registry())
    if param_override and sid in reg:
        reg[sid].setdefault("params", {}).update(param_override)

    sdir = Path(tempfile.mkdtemp(prefix=f"bt_{sid.replace('@','_')}_"))
    old_state, old_log = conf.STATE_DIR, E.TRADE_LOG
    conf.STATE_DIR = sdir
    E.conf.STATE_DIR = sdir
    E.TRADE_LOG = sdir / "trade_log.csv"

    conn = get_conn()
    try:
        eng = E.Engine(config=cfg, registry=reg, conn=conn, cache_bars=True)
        days = cal.trade_days(start, end)
        for d in days:
            eng.settle(d)
            eng.run_strategies(d)
        st = eng.state.get(sid, {})
        hist = st.get("nav_history", [])
        dates = [h[0] for h in hist]
        navs = [h[1] for h in hist]
        bench = _benchmark_navs(conn, reg.get(sid, {}).get("benchmark"), dates) if dates else None
        met = compute_metrics(navs, bench)
        result = {"dates": dates, "navs": navs, "metrics": met, "bench_navs": bench,
                  "trade_log_path": str(E.TRADE_LOG), "state_dir": str(sdir),
                  "final_positions": {c: p.shares for c, p in eng.load_account(sid).positions.items()}}
    finally:
        conf.STATE_DIR, E.conf.STATE_DIR, E.TRADE_LOG = old_state, old_state, old_log
        if not keep_state:
            shutil.rmtree(sdir, ignore_errors=True)
    return result


def _deep_update(d, u):
    for k, v in u.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            _deep_update(d[k], v)
        else:
            d[k] = v


def _fmt(m):
    return (f"累计{m['total']:.1%} 年化{m['annual']:.1%} 回撤{m['max_dd']:.1%} "
            f"Calmar{m['calmar']:.2f} Sharpe{m['sharpe']:.2f} 胜率{m['win']:.1%} "
            f"超额{m['excess']:+.1%} ({m['days']}日)")


# ---------------- 五关验证报告 ----------------
def five_pass_report(sid, capital=None, out_path=None):
    reg = conf.load_registry()
    cfg = conf.load_config()
    capital = capital or cfg["user"]["capital"]
    params = reg[sid]["params"]
    today = util.today_str()
    lines = [f"# {sid} 五关验证报告", "",
             f"- 资金档:{capital:,.0f} 元(按用户 config 实跑,不用大资金美化)",
             f"- 生成日:{today}  基准:{reg[sid].get('benchmark')}",
             f"- 参数:{params}", "",
             "> 免责:模拟/历史表现不代表未来。成交按次日开盘价+真实费用滑点(SPEC_FILL)建模,故意保守。", ""]

    # 主回测(2022-01-01~今)
    main = run_backtest(sid, "2022-01-01", today, capital=capital)
    lines += ["## 主回测(2022-01-01 ~ 今)", f"- {_fmt(main['metrics'])}", ""]

    # ① 样本内/外 Calmar
    ins = run_backtest(sid, "2019-01-01", "2023-12-31", capital=capital)
    oos = run_backtest(sid, "2024-01-01", today, capital=capital)
    lines += ["## ①样本内(2019-2023)/样本外(2024-今) Calmar 对比",
              f"- 样本内:{_fmt(ins['metrics'])}",
              f"- 样本外:{_fmt(oos['metrics'])}",
              f"- Calmar 保持率:{_ratio(oos['metrics']['calmar'], ins['metrics']['calmar'])}", ""]

    # ② 滚动前推5轮(逐年)
    lines += ["## ②滚动前推(逐年分段,考察稳定性)"]
    seg_years = [("2019-01-01", "2019-12-31"), ("2020-01-01", "2020-12-31"),
                 ("2021-01-01", "2021-12-31"), ("2022-01-01", "2022-12-31"),
                 ("2023-01-01", "2023-12-31")]
    calmars = []
    for s, e in seg_years:
        r = run_backtest(sid, s, e, capital=capital)
        calmars.append(r["metrics"]["calmar"])
        lines.append(f"- {s[:4]}: {_fmt(r['metrics'])}")
    lines += [f"- Calmar 均值 {np.mean(calmars):.2f} 标准差 {np.std(calmars):.2f}", ""]

    # ③ 参数±20%扰动
    lines += ["## ③参数±20%扰动"]
    if "momentum_windows" in params:
        for scale, tag in [(0.8, "-20%"), (1.2, "+20%")]:
            w = [max(2, int(round(x * scale))) for x in params["momentum_windows"]]
            r = run_backtest(sid, "2022-01-01", today, capital=capital,
                             param_override={"momentum_windows": w})
            lines.append(f"- 窗口{tag}={w}: {_fmt(r['metrics'])}")
    lines.append("")

    # ④ 成本加压(滑点×2 + impact×2 + open_frac×0.5, SPEC_FILL F1.4)
    stressed = run_backtest(sid, "2022-01-01", today, capital=capital,
                            cost_override={"slippage": {"etf": cfg["costs"]["slippage"]["etf"] * 2,
                                                        "stock": cfg["costs"]["slippage"]["stock"] * 2}},
                            custom_override={"impact_k": {"stock": cfg["custom"]["impact_k"]["stock"] * 2,
                                                          "etf": cfg["custom"]["impact_k"]["etf"] * 2},
                                            "open_frac": {"stock": cfg["custom"]["open_frac"]["stock"] * 0.5,
                                                          "etf": cfg["custom"]["open_frac"]["etf"] * 0.5}})
    lines += ["## ④成本双重加压(滑点×2 冲击×2 参与量×0.5)",
              f"- 加压后:{_fmt(stressed['metrics'])}",
              f"- 收益衰减:{main['metrics']['total'] - stressed['metrics']['total']:+.1%}", ""]

    # ⑤ 牛熊分段
    lines += ["## ⑤牛熊震荡分段"]
    for s, e, tag in [("2018-01-01", "2018-12-31", "2018熊"),
                      ("2019-01-01", "2021-12-31", "2019-21牛"),
                      ("2022-01-01", "2024-12-31", "2022-24震荡")]:
        r = run_backtest(sid, s, e, capital=capital)
        lines.append(f"- {tag}: {_fmt(r['metrics'])}")
    lines.append("")
    lines += ["## 结论", "见各关指标;入池最终以 validate.py 蒙特卡洛5%分位为准(P15)。", ""]

    text = "\n".join(lines)
    out_path = out_path or (conf.REPORTS_DIR / f"{sid.replace('@','_at_')}.md")
    Path(out_path).write_text(text, encoding="utf-8")
    return str(out_path), main["metrics"]


def _ratio(a, b):
    if not b or b <= 1e-9:
        return "N/A(样本内 Calmar≤0,不适用)"
    return f"{a/b:.0%}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        sid = sys.argv[2] if len(sys.argv) > 2 else "s2_etf@v1"
        path, met = five_pass_report(sid)
        print("报告已生成:", path)
        print("主回测:", _fmt(met))
    else:
        sid = sys.argv[1] if len(sys.argv) > 1 else "s2_etf@v1"
        r = run_backtest(sid, "2022-01-01", util.today_str())
        print(sid, _fmt(r["metrics"]))
        print("final positions:", r["final_positions"])
