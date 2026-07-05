# -*- coding: utf-8 -*-
"""卡J: V3策略回测与因子暴露追踪。

1. 5段滚动回测(s1_dividend@v3 / s4_smallcap@v2)
2. 报告:年化收益/最大回撤/夏普比/卡玛比/胜率 + 5段平均因子暴露
3. 输出 reports/{name}_exposures.json: 因子暴露时间序列
"""
import json
import logging
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

import conf
import util
import factors
import riskmodel
import backtest as bt
from db import get_conn

log = logging.getLogger("backtest_v3")

SEGMENTS = [
    ("2019-01-01", "2019-12-31"),
    ("2020-01-01", "2020-12-31"),
    ("2021-01-01", "2021-12-31"),
    ("2022-01-01", "2022-12-31"),
    ("2023-01-01", "2023-12-31"),
]


def fmt_metrics(m):
    return (f"累计{m['total']:.1%} 年化{m['annual']:.1%} 回撤{m['max_dd']:.1%} "
            f"Calmar{m['calmar']:.2f} Sharpe{m['sharpe']:.2f} 胜率{m['win']:.1%} "
            f"超额{m['excess']:+.1%} ({m['days']}日)")


def run_segment_backtest(sid, start, end, capital=50000):
    """运行单段回测并收集因子暴露历史。"""
    try:
        result = bt.run_backtest(sid, start, end, capital=capital, keep_state=True)
        met = result.get("metrics", {})
        dates = result.get("dates", [])
        navs = result.get("navs", [])
        bench = result.get("bench_navs", [])
        state_dir = result.get("state_dir", "")

        # 尝试从 state 恢复因子暴露（解析 nav_history 对应的月末截面）
        exposures_records = []
        if dates and state_dir:
            exposures_records = _collect_exposures(sid, dates, state_dir)

        metrics = _process_metrics(met, navs, bench)

        # 清理临时 state dir
        import shutil
        if state_dir and Path(state_dir).exists():
            shutil.rmtree(Path(state_dir), ignore_errors=True)

        return {
            "metrics": metrics,
            "exposures": exposures_records,
            "success": True,
        }
    except Exception as e:
        log.error("段回测 %s %s-%s 失败: %s", sid, start, end, e)
        return {
            "metrics": {"annual": 0, "max_dd": 0, "sharpe": 0, "calmar": 0, "win": 0, "excess": 0, "total": 0, "days": 0},
            "exposures": [],
            "success": False,
        }


def _collect_exposures(sid, dates, state_dir):
    """收集回测期间的因子暴露（按月采样, 月末日期）。"""
    records = []
    # 取每月的最后一个 date（近似月末）
    monthly_dates = []
    for d in dates:
        parts = d.split("-")
        ym = (parts[0], parts[1])
        if not monthly_dates or monthly_dates[-1][0] != ym:
            monthly_dates.append((ym, d))
        else:
            monthly_dates[-1] = (ym, d)

    conn = get_conn()
    try:
        for (y, m), d in monthly_dates[-12:]:  # 最多12个月
            try:
                pool = [r[0] for r in conn.execute(
                    "SELECT code FROM index_members WHERE index_code='sh000300' AND in_date<=? "
                    "AND (out_date IS NULL OR out_date>?) LIMIT 300", (d, d)).fetchall()]
                if not pool:
                    continue
                exp = factors.compute_factor_exposures(pool, d, conn=conn)
                if exp.empty:
                    continue
                record = {"date": d, "year": int(y), "month": int(m)}
                for col in exp.columns:
                    record[col] = float(exp[col].mean())
                records.append(record)
            except Exception as e:
                log.debug("因子暴露采集 %s 失败: %s", d, e)
    finally:
        conn.close()
    return records


def _process_metrics(raw_met, navs, bench_navs):
    """用 raw backtest metrics 增强计算。"""
    m = {}
    navs_arr = np.asarray(navs, dtype=float) if navs else np.array([])
    bench_arr = np.asarray(bench_navs, dtype=float) if bench_navs else np.array([])

    if len(navs_arr) >= 2:
        rets = navs_arr[1:] / navs_arr[:-1] - 1
        n_days = len(navs_arr)
        years = max(n_days / 252.0, 0.02)
        total = navs_arr[-1] / navs_arr[0] - 1
        annual = (navs_arr[-1] / navs_arr[0]) ** (1 / years) - 1 if years > 0 else 0.0
        peak = np.maximum.accumulate(navs_arr)
        dd = 1 - navs_arr / peak
        max_dd = float(dd.max()) if len(dd) > 0 else 0
        calmar = (annual / max_dd) if max_dd > 1e-9 else 0.0
        sd = float(rets.std(ddof=1))
        sharpe = float(rets.mean() / sd * np.sqrt(252)) if sd > 1e-12 else 0.0
        win = float((rets > 0).mean())
        days = n_days
    else:
        total = annual = max_dd = calmar = sharpe = win = 0.0
        days = 0

    excess = 0.0
    if len(bench_arr) >= 2 and len(navs_arr) >= 2:
        excess = (navs_arr[-1] / navs_arr[0]) - (bench_arr[-1] / bench_arr[0])

    m["total"] = total
    m["annual"] = annual
    m["max_dd"] = max_dd
    m["calmar"] = calmar
    m["sharpe"] = sharpe
    m["win"] = win
    m["excess"] = excess
    m["days"] = days
    return m


def generate_report(sid, results, out_path=None):
    """生成 Markdown 报告 + exposures JSON。"""
    metrics_list = [r["metrics"] for r in results]
    all_exposures = []
    for r in results:
        all_exposures.extend(r.get("exposures", []))

    # write exposures json
    json_path = conf.REPORTS_DIR / f"{sid.replace('@', '_at_')}_exposures.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_exposures, f, ensure_ascii=False, indent=1)

    reg = conf.load_registry()
    today = util.today_str()

    lines = []
    lines.append(f"# {sid} 五段滚动回测报告(卡J)")
    lines.append("")
    lines.append(f"- 生成日: {today}  基准: {reg.get(sid, {}).get('benchmark', 'sh510300')}")
    lines.append(f"- 参数: {reg.get(sid, {}).get('params', {})}")
    lines.append("")
    lines.append("> 免责:模拟/历史表现不代表未来。使用 Barra 风格因子体系(factors.py pipeline)。")
    lines.append("")

    # 分段指标
    lines.append("## 五段滚动回测")
    lines.append("")
    lines.append("| 段 | 累计收益 | 年化收益 | 最大回撤 | Calmar | Sharpe | 胜率 | 超额 |")
    lines.append("|----|---------|---------|---------|--------|--------|------|------|")
    annual_vals = []
    calmar_vals = []
    sharpe_vals = []
    for i, (s, e) in enumerate(SEGMENTS):
        m = metrics_list[i] if i < len(metrics_list) else {}
        tag = s[:4]
        lines.append(f"| {tag} | {m.get('total', 0):.1%} | {m.get('annual', 0):.1%} | "
                     f"{m.get('max_dd', 0):.1%} | {m.get('calmar', 0):.2f} | "
                     f"{m.get('sharpe', 0):.2f} | {m.get('win', 0):.1%} | "
                     f"{m.get('excess', 0):+.1%} |")
        annual_vals.append(m.get("annual", 0))
        calmar_vals.append(m.get("calmar", 0))
        sharpe_vals.append(m.get("sharpe", 0))

    lines.append("")
    lines.append("### 汇总统计")
    lines.append(f"- 年化收益均值: {np.mean(annual_vals):.1%}  标准差: {np.std(annual_vals):.1%}")
    lines.append(f"- Calmar 均值: {np.mean(calmar_vals):.2f}  标准差: {np.std(calmar_vals):.2f}")
    lines.append(f"- Sharpe 均值: {np.mean(sharpe_vals):.2f}  标准差: {np.std(sharpe_vals):.2f}")
    lines.append("")

    # 因子暴露摘要
    lines.append("## 因子暴露摘要(五段平均)")
    lines.append("")

    if all_exposures:
        exposure_df = pd.DataFrame(all_exposures)
        date_cols = [c for c in exposure_df.columns if c not in ("date", "year", "month")]
        averages = {}
        for col in date_cols:
            vals = exposure_df[col].dropna()
            if len(vals) > 0:
                averages[col] = float(vals.mean())
        if averages:
            lines.append("| 因子 | 平均暴露 |")
            lines.append("|------|---------|")
            for k, v in averages.items():
                lines.append(f"| {k} | {v:+.4f} |")
        else:
            lines.append("(无因子暴露数据)")
    else:
        lines.append("(无因子暴露数据)")
    lines.append("")

    # 风险模型信息
    lines.append("## 风险模型备注")
    lines.append("- 因子体系: factors.py (10风格因子 + Gram-Schmidt 正交化)")
    lines.append("- 风险控制: riskmodel.py (Barra结构模型, EWMA协方差)")
    lines.append("- 因子暴露时序: 见 {}  ".format(json_path.name))
    lines.append("")

    text = "\n".join(lines)
    out_path = out_path or (conf.REPORTS_DIR / f"{sid.replace('@', '_at_')}_v3.md")
    Path(out_path).write_text(text, encoding="utf-8")
    return str(out_path)


def run_report_for(sid, capital=50000):
    """运行五段回测并生成报告。"""
    log.info("=== %s 五段回测开始 ===", sid)
    results = []
    for s, e in SEGMENTS:
        log.info("段 %s - %s", s, e)
        r = run_segment_backtest(sid, s, e, capital=capital)
        results.append(r)
        met = r["metrics"]
        log.info("  %s", fmt_metrics(met))

    path = generate_report(sid, results)
    log.info("报告: %s", path)
    return path, results


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s|%(name)s|%(message)s")

    targets = sys.argv[1:] if len(sys.argv) > 1 else ["s1_dividend@v3", "s4_smallcap@v2"]
    for sid in targets:
        try:
            path, results = run_report_for(sid)
            print(f"\n{'='*60}")
            print(f"  {sid} 完成")
            print(f"  报告: {path}")
            metrics_list = [r["metrics"] for r in results]
            ann_mean = np.mean([m["annual"] for m in metrics_list])
            cal_mean = np.mean([m["calmar"] for m in metrics_list])
            print(f"  年化均值: {ann_mean:.1%}  Calmar均值: {cal_mean:.2f}")
            print(f"{'='*60}")
        except Exception as e:
            log.exception("%s 回测失败: %s", sid, e)

    print("\n全部完成。")


if __name__ == "__main__":
    main()
