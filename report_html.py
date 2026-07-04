# -*- coding: utf-8 -*-
"""静态HTML看板生成(国内可达方案)。零外部依赖:数据内嵌、内联SVG画净值曲线、手机自适应。
产出 docs/index.html —— GitHub Pages 托管即可,或本地双击打开(无需翻墙/CDN)。
被 run_daily 收尾调用,也可单独:python report_html.py"""
import json
import glob
import os
import re
import html

import conf
import util
import backtest as bt


def _grab(line, pat):
    m = re.search(pat, line or "")
    return m.group(1) if m else "—"


def _backtest_summary(sid):
    """从 reports/ 读回测主线 + 入池判定,供静态看板首日即展示预期表现。"""
    slug = sid.replace("@", "_at_")
    bt_line, verdict = "", ""
    rp = conf.REPORTS_DIR / f"{slug}.md"
    if rp.exists():
        m = re.search(r"## 主回测.*?\n- (.+)", rp.read_text(encoding="utf-8"))
        if m:
            bt_line = m.group(1).strip()
    vp = conf.REPORTS_DIR / f"{slug}_validate.md"
    if vp.exists():
        m = re.search(r"## 结论:\*\*(.+?)\*\*", vp.read_text(encoding="utf-8"))
        if m:
            verdict = m.group(1).strip()
    return bt_line, verdict

STRAT_CN = {"s2_etf@v1": "ETF动量轮动", "s1_dividend@v1": "红利低波", "s3_ma_trend@v1": "双均线趋势",
            "s4_smallcap@v1": "小市值多因子", "s5_grid@v1": "大盘网格"}


def _cn(sid):
    return STRAT_CN.get(sid, sid)


def _load_accounts():
    out = {}
    for f in glob.glob(str(conf.STATE_DIR / "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
            if "strategy_id" in d:
                out[d["strategy_id"]] = d
        except Exception:
            pass
    return out


def _sparkline(navs, w=320, h=64, color="#2563eb"):
    if len(navs) < 2:
        return ""
    lo, hi = min(navs), max(navs)
    rng = (hi - lo) or 1
    pts = []
    for i, v in enumerate(navs):
        x = i / (len(navs) - 1) * w
        y = h - (v - lo) / rng * (h - 6) - 3
        pts.append(f"{x:.1f},{y:.1f}")
    base = h - (1.0 - lo) / rng * (h - 6) - 3 if lo <= 1.0 <= hi else None
    baseline = f'<line x1="0" y1="{base:.1f}" x2="{w}" y2="{base:.1f}" stroke="#ccc" stroke-dasharray="3" />' if base else ""
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none">'
            f'{baseline}<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(pts)}"/></svg>')


def _metrics(a):
    hist = a.get("nav_history", [])
    if len(hist) < 2:
        return None
    return bt.compute_metrics([h[1] for h in hist])


def _pct(x):
    return f"{x:+.1%}" if x is not None else "—"


def generate(out_path=None):
    accts = _load_accounts()
    today = util.today_str()
    try:
        from db import get_conn
        conn = get_conn()
        last = conn.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
        conn.close()
    except Exception:
        last = "—"

    # 总览表
    rows = ""
    cards = ""
    for sid, a in sorted(accts.items()):
        m = _metrics(a)
        navs = [h[1] for h in a.get("nav_history", [])]
        st = "🔴熔断" if a.get("frozen") else "🟢正常"
        color = "#16a34a" if (m and m["total"] >= 0) else "#dc2626"
        bt_line, verdict = _backtest_summary(sid)
        bt_cum = _grab(bt_line, r"累计([+\-\d.]+%)")
        bt_cal = _grab(bt_line, r"Calmar([+\-\d.]+)")
        bt_dd = _grab(bt_line, r"回撤([+\-\d.]+%)")
        vc = "#16a34a" if "入池" in verdict else "#a16207"
        vbadge = f"<span class='badge' style='background:{vc}'>{verdict}</span>" if verdict else ""
        cumcolor = "#16a34a" if not bt_cum.startswith("-") else "#dc2626"
        rows += (f"<tr><td>{_cn(sid)}</td>"
                 f"<td style='color:{cumcolor}'>{bt_cum}</td><td>{bt_dd}</td><td>{bt_cal}</td>"
                 f"<td>{verdict or '—'}</td></tr>")
        # 卡片(含曲线 + 今日操作)
        pend = a.get("pending", [])
        ops = "".join(
            f"<div class='op {'sell' if o['side']=='sell' else 'buy'}'>"
            f"{'卖出' if o['side']=='sell' else '买入'} {util.bare(o['code'])} "
            f"<span class='reason'>{html.escape(o.get('reason','')[:40])}</span></div>" for o in pend)
        ops = ops or "<div class='op none'>今日无操作</div>"
        live_m = (f"<div class='m'>实盘跟踪: 净值 {a.get('nav',1):.3f} · "
                  f"累计 {_pct(m['total']) if m else '今日起步'}</div>") if m else \
                 f"<div class='m'>实盘跟踪: 今日起步(曲线随天数累积)</div>"
        bt_html = f"<div class='bt'>📈 回测(2022→今): {html.escape(bt_line)}</div>" if bt_line else ""
        cards += (f"<div class='card'><div class='card-h'><b>{_cn(sid)}</b>"
                  f"<span>{st} {vbadge}</span></div>"
                  f"{_sparkline(navs, color=color)}"
                  f"{bt_html}{live_m}"
                  f"<div class='ops'>{ops}</div></div>")

    if not accts:
        cards = "<p class='empty'>暂无策略状态。请先运行 run_daily.py 或回测生成 state/。</p>"

    html_doc = _TEMPLATE.format(today=today, last=last or "—", rows=rows, cards=cards)
    out_path = out_path or (conf.ROOT / "docs" / "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return str(out_path)


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股模拟跟单看板</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1f2937;--mut:#6b7280;--card:#fff;--line:#e5e7eb}}
*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--fg);font-size:15px;line-height:1.5}}
.wrap{{max-width:720px;margin:0 auto;padding:16px}}
h1{{font-size:20px;margin:8px 0}}.sub{{color:var(--mut);font-size:13px;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;font-size:14px}}
th,td{{padding:9px 8px;text-align:center;border-bottom:1px solid var(--line)}}
th{{background:#f0f2f5;color:var(--mut);font-weight:600}}td:first-child,th:first-child{{text-align:left}}
.card{{background:var(--card);border-radius:12px;padding:14px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.card-h{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}}
.card-h span{{color:var(--mut);font-size:13px}}
.m{{color:var(--mut);font-size:13px;margin:6px 0}}
.ops{{margin-top:8px}}.op{{padding:6px 10px;border-radius:8px;margin:4px 0;font-size:13px}}
.op.buy{{background:#ecfdf5;color:#065f46}}.op.sell{{background:#fef2f2;color:#991b1b}}
.op.none{{background:#f3f4f6;color:var(--mut)}}.reason{{color:var(--mut)}}
.badge{{color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px}}
.bt{{background:#f0f7ff;color:#1e40af;font-size:12.5px;padding:6px 10px;border-radius:8px;margin:6px 0}}
.sec{{margin:22px 0 8px;font-size:16px;font-weight:600}}
.foot{{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}}
.empty{{color:var(--mut);text-align:center;padding:40px}}
</style></head><body><div class="wrap">
<h1>📊 A股模拟跟单看板</h1>
<div class="sub">生成 {today} · 数据最新 {last} · 模拟/历史不代表未来,非投资建议,人工跟单</div>
<div class="sec">策略总览(回测 2022→今,严格稳健性判定)</div>
<table><tr><th>策略</th><th>累计</th><th>回撤</th><th>Calmar</th><th>判定</th></tr>{rows}</table>
<div class="sec">各策略详情 &amp; 今日操作</div>
{cards}
<div class="foot">本页由 report_html.py 自动生成,零外部依赖,可离线打开。<br>
每次买卖以推送与本页『今日操作』为准,按次日开盘价附近跟单。触发🔴熔断的策略已自动清仓降险。</div>
</div></body></html>"""


if __name__ == "__main__":
    print("已生成:", generate())
