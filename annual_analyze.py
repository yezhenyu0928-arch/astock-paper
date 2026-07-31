"""读取 results_annual.json，生成逐年收益矩阵 + 牛熊依赖判定。

输出:
  - results_annual_matrix.md   纯文本统计表（逐年收益% + 判定）
  - results_annual_matrix.html 带热力色的可视化表
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(HERE, "results_annual.json")
MD_PATH = os.path.join(HERE, "results_annual_matrix.md")
HTML_PATH = os.path.join(HERE, "results_annual_matrix.html")

LABELS = {
    "s26_microcap@v1": "s26 微盘",
    "s27_dividend_lowvol@v1": "s27 红利低波",
    "s29_smallcap_select@v1": "s29 小盘精选",
    "s32_roe_quality@v1": "s32 高ROE质量",
    "s37_earnings_accel@v1": "s37 盈利加速",
    "s42_sue_enriched@v1": "s42 SUE增强",
    "s53_all_a_momentum_smallcap@v1": "s53 全A动量",
}
ORDER = list(LABELS.keys())


def load():
    with open(JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def classify(annual, bench):
    """按基准(沪深300)把年份分牛/熊，量化对牛市的依赖程度。

    牛熊差 = 牛市年(沪深300>=0)平均收益 - 熊市年(沪深300<0)平均收益。
    差值越大说明年化越靠牛市抬升；差值小且熊市年稳赚=真韧性。
    """
    years = sorted(set(annual) | set(bench))
    bull = [y for y in years if bench.get(y) is not None and bench.get(y) >= 0]
    bear = [y for y in years if bench.get(y) is not None and bench.get(y) < 0]

    def avg(ys):
        vals = [annual.get(y) for y in ys if annual.get(y) is not None]
        return sum(vals) / len(vals) if vals else None

    bull_avg = avg(bull)
    bear_avg = avg(bear)
    bear_vals = [annual.get(y) for y in bear if annual.get(y) is not None]
    min_bear = min(bear_vals) if bear_vals else None
    gap = (bull_avg - bear_avg) if (bull_avg is not None and bear_avg is not None) else None
    pos_years = sum(1 for y in years if annual.get(y) is not None and annual[y] > 0)
    total_years = sum(1 for y in years if annual.get(y) is not None)

    # 依赖判定
    verdict = ""
    if bear_avg is None:
        verdict = "无熊市样本"
    elif min_bear is not None and min_bear <= -15:
        verdict = "熊市有硬伤(某年大亏)"
    elif gap is not None and gap >= 25:
        verdict = "高度依赖牛市"
    elif gap is not None and gap >= 12:
        verdict = "偏牛市驱动"
    elif bear_avg < 2:
        verdict = "较抗跌(熊市微赚)"
    elif bear_avg >= 3:
        verdict = "真有韧性(熊市也赚)"
    else:
        verdict = "中性"
    return dict(years=years, bull=bull, bear=bear, bull_avg=bull_avg,
                bear_avg=bear_avg, min_bear=min_bear, gap=gap,
                pos_years=pos_years, total_years=total_years,
                verdict=verdict)


def pct(v):
    return f"{v*100:+.1f}" if isinstance(v, (int, float)) else "-"


def f2(v):
    return f"{v:+.1f}" if isinstance(v, (int, float)) else "-"


def build():
    data = load()
    all_years = set()
    for sid in ORDER:
        d = data.get(sid, {})
        all_years |= set(d.get("annual", {}).keys())
        all_years |= set(d.get("bench_annual", {}).keys())
    years = sorted(all_years)

    print(f"年份覆盖: {years}")
    print(f"策略数: {len([s for s in ORDER if s in data])}")

    # ---- Markdown 表 ----
    md = []
    md.append("# 策略逐年收益统计（2022–2026，自然年）\n")
    md.append("> 收益=年末净值/年初净值-1（%）。基准=沪深300。绿色=正收益，红色=负收益。\n")
    md.append("## 1. 逐年收益矩阵（%）\n")
    header = "| 策略 | " + " | ".join(years) + " | 正收益年 | 总年 |"
    md.append(header)
    md.append("|" + "---|" * (len(years) + 3))

    clf = {}
    for sid in ORDER:
        d = data.get(sid)
        if not d or "annual" not in d:
            md.append(f"| {LABELS[sid]} | " + " | ".join(["-"] * len(years)) + " | - | - |")
            continue
        a = d["annual"]
        cells = []
        for y in years:
            v = a.get(y)
            cells.append(f"{v:+.1f}" if isinstance(v, (int, float)) else "-")
        md.append(f"| {LABELS[sid]} | " + " | ".join(cells) +
                  f" | {sum(1 for y in years if isinstance(a.get(y),(int,float)) and a[y]>0)} | {sum(1 for y in years if isinstance(a.get(y),(int,float)))} |")

    # 基准行
    bench = {}
    for sid in ORDER:
        b = data.get(sid, {}).get("bench_annual", {})
        if b:
            bench = b
            break
    if bench:
        bcells = []
        for y in years:
            v = bench.get(y)
            bcells.append(f"{v:+.1f}" if isinstance(v, (int, float)) else "-")
        md.append(f"| **沪深300(基准)** | " + " | ".join(bcells) +
                  f" | {sum(1 for y in years if isinstance(bench.get(y),(int,float)) and bench[y]>0)} | {sum(1 for y in years if isinstance(bench.get(y),(int,float)))} |")

    # ---- 牛熊依赖判定表 ----
    md.append("\n## 2. 牛市依赖判定\n")
    md.append("判定逻辑：以沪深300当年收益分**牛市年(≥0)/熊市年(<0)**；牛熊差=牛市年均−熊市年均，差值越大越靠牛市。")
    md.append("")
    md.append("| 策略 | 正收益年/总年 | 牛市年平均 | 熊市年平均 | 牛熊差 | 判定 |")
    md.append("|---|---|---|---|---|---|")
    for sid in ORDER:
        d = data.get(sid)
        if not d or "annual" not in d:
            continue
        c = classify(d["annual"], d.get("bench_annual", {}))
        clf[sid] = c
        ba = f"{c['bull_avg']:+.1f}" if c["bull_avg"] is not None else "-"
        bea = f"{c['bear_avg']:+.1f}" if c["bear_avg"] is not None else "-"
        gp = f"{c['gap']:+.1f}" if isinstance(c.get('gap'), (int, float)) else "-"
        md.append(f"| {LABELS[sid]} | {c['pos_years']}/{c['total_years']} | {ba} | {bea} | {gp} | **{c['verdict']}** |")

    # ---- 全周期总指标 ----
    md.append("\n## 3. 全周期总指标\n")
    md.append("| 策略 | 年化% | 最大回撤% | Calmar | 夏普 | 累计% |")
    md.append("|---|---|---|---|---|---|")
    for sid in ORDER:
        d = data.get(sid)
        if not d or "met" not in d:
            continue
        m = d["met"]
        md.append(f"| {LABELS[sid]} | {pct(m.get('annual'))} | {pct(m.get('max_dd'))} | "
                  f"{f2(m.get('calmar'))} | {f2(m.get('sharpe'))} | {pct(m.get('total'))} |")

    # ---- 结论摘要 ----
    md.append("\n## 结论摘要\n")
    bullish = [LABELS[s] for s in ORDER if clf.get(s, {}).get("verdict") == "高度依赖牛市"]
    driven = [LABELS[s] for s in ORDER if clf.get(s, {}).get("verdict") == "偏牛市驱动"]
    resilient = [LABELS[s] for s in ORDER if clf.get(s, {}).get("verdict") == "真有韧性(熊市也赚)"]
    resist = [LABELS[s] for s in ORDER if "抗跌" in clf.get(s, {}).get("verdict", "")]
    hardhit = [LABELS[s] for s in ORDER if "硬伤" in clf.get(s, {}).get("verdict", "")]
    md.append(f"- **高度依赖牛市（牛熊差≥25pt，年化主要靠牛市抬升）**: {', '.join(bullish) or '无'}")
    md.append(f"- **偏牛市驱动（牛熊差12~25pt）**: {', '.join(driven) or '无'}")
    md.append(f"- **真有韧性（熊市年也稳赚，牛熊差小）**: {', '.join(resilient) or '无'}")
    md.append(f"- **较抗跌（熊市仅微赚）**: {', '.join(resist) or '无'}")
    md.append(f"- **熊市有硬伤（某年大亏>15pt）**: {', '.join(hardhit) or '无'}")
    md.append("")
    md.append("> **牛熊差** = 牛市年(沪深300≥0)平均收益 − 熊市年(沪深300<0)平均收益；差值越大说明年化越靠牛市。")
    md.append("> 注：2022 为基准熊市年(沪深300 −21.1%)，但微盘/小盘/红利策略逆势大涨，故多数策略熊市年均仍为正。")
    md.append("> 注：2026 为部分年（截至 07-30），仅反映年初至 07-30 收益，与完整年不可直接比。")
    md.append("")

    md_text = "\n".join(md)
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write(md_text)
    print("->", MD_PATH)

    # ---- HTML ----
    html = render_html(data, years, bench, clf)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print("->", HTML_PATH)


def render_html(data, years, bench, clf):
    def color(v):
        if not isinstance(v, (int, float)):
            return "#f3f4f6", "#6b7280"
        # 红涨绿跌(中国习惯)
        if v > 0:
            inten = min(1.0, v / 40.0)
            return f"rgba(220,38,38,{0.15+0.65*inten})", "#7f1d1d"
        elif v < 0:
            inten = min(1.0, abs(v) / 40.0)
            return f"rgba(22,163,74,{0.15+0.65*inten})", "#14532d"
        return "#f3f4f6", "#374151"

    rows = []
    for sid in ORDER:
        d = data.get(sid)
        if not d or "annual" not in d:
            continue
        a = d["annual"]
        cells = []
        for y in years:
            v = a.get(y)
            bg, fg = color(v)
            txt = f"{v:+.1f}" if isinstance(v, (int, float)) else "-"
            cells.append(f'<td style="background:{bg};color:{fg};text-align:right;font-variant-numeric:tabular-nums">{txt}</td>')
        c = clf.get(sid, {})
        rows.append(f"<tr><td style='text-align:left;font-weight:600'>{LABELS[sid]}</td>"
                    + "".join(cells) +
                    f"<td>{c.get('pos_years','-')}/{c.get('total_years','-')}</td></tr>")

    # 基准行
    bench_cells = ""
    for y in years:
        v = bench.get(y)
        bg, fg = color(v)
        txt = f"{v:+.1f}" if isinstance(v, (int, float)) else "-"
        bench_cells += f'<td style="background:{bg};color:{fg};text-align:right;font-weight:700">{txt}</td>'

    # 判定行
    verdict_rows = ""
    for sid in ORDER:
        d = data.get(sid)
        if not d or "annual" not in d:
            continue
        c = clf.get(sid, {})
        vc = {"高度依赖牛市": "#b91c1c", "偏牛市驱动": "#dc2626", "熊市拖累明显": "#c2410c",
              "较抗跌(熊市微赚)": "#a16207", "真有韧性(熊市也赚)": "#15803d",
              "熊市有硬伤(某年大亏)": "#7c2d12", "无熊市样本": "#6b7280"}.get(c.get("verdict", ""), "#374151")
        gp = f2(c.get('gap'))
        verdict_rows += (f"<tr><td style='text-align:left;font-weight:600'>{LABELS[sid]}</td>"
                         f"<td>{c.get('pos_years','-')}/{c.get('total_years','-')}</td>"
                         f"<td>{f2(c.get('bull_avg'))}</td><td>{f2(c.get('bear_avg'))}</td>"
                         f"<td>{gp}</td>"
                         f"<td style='color:{vc};font-weight:700'>{c.get('verdict','-')}</td></tr>")

    year_head = "".join(f"<th>{y}</th>" for y in years)
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>策略逐年收益统计</title>
<style>
body{{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:24px;background:#fff;color:#1f2937}}
h1{{font-size:20px;margin-bottom:4px}} .sub{{color:#6b7280;font-size:13px;margin-bottom:20px}}
.section{{margin:28px 0}} h2{{font-size:16px;border-left:4px solid #2563eb;padding-left:8px}}
table{{border-collapse:collapse;font-size:13px;margin-top:10px}}
th,td{{border:1px solid #e5e7eb;padding:6px 9px}}
th{{background:#f8fafc;font-weight:600}}
.legend{{font-size:12px;color:#6b7280;margin-top:8px}}
.note{{background:#fef9c3;padding:10px 14px;border-radius:6px;font-size:13px;line-height:1.6;margin-top:14px}}
</style></head><body>
<h1>策略逐年收益统计（2022–2026，自然年）</h1>
<div class="sub">收益=年末净值/年初净值-1（%）。基准=沪深300。颜色按中国习惯：<b style="color:#b91c1c">红涨</b>/<b style="color:#15803d">绿跌</b>。</div>

<div class="section">
<h2>1. 逐年收益矩阵（%）</h2>
<table>
<thead><tr><th>策略</th>{year_head}<th>正/总年</th></tr></thead>
<tbody>
{''.join(rows)}
<tr style="font-weight:700;background:#f8fafc"><td>沪深300(基准)</td>{bench_cells}<td>-</td></tr>
</tbody></table>
<div class="legend">注：2026 为部分年（截至 07-30），仅反映年初至 07-30 收益。</div>
</div>

<div class="section">
<h2>2. 牛市依赖判定</h2>
<div class="sub">以沪深300当年收益分<b>牛市年(≥0)</b>/<b>熊市年(&lt;0)</b>；<b>牛熊差</b>=牛市年均−熊市年均，差值越大越靠牛市。</div>
<table>
<thead><tr><th>策略</th><th>正/总年</th><th>牛市年平均</th><th>熊市年平均</th><th>牛熊差</th><th>判定</th></tr></thead>
<tbody>{verdict_rows}</tbody>
</table>
</div>

<div class="note" id="summary"></div>
<script>
// 动态填结论摘要
var rows = document.querySelectorAll('#tbody-verdict tr');
</script>
</body></html>"""
    return html


if __name__ == "__main__":
    build()
