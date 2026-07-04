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


def _latest_close(conn, code):
    try:
        r = conn.execute("SELECT close FROM daily_bar WHERE code=? ORDER BY trade_date DESC LIMIT 1",
                         (code,)).fetchone()
        return float(r[0]) if r else 0.0
    except Exception:
        return 0.0


def ctx_name(conn, code):
    try:
        r = conn.execute("SELECT name FROM security WHERE code=?", (code,)).fetchone()
        return r[0] if r and r[0] else util.bare(code)
    except Exception:
        return util.bare(code)


def _acct_total(conn, a):
    total = a.get("cash", 0)
    for code, p in a.get("positions", {}).items():
        total += p.get("shares", 0) * _latest_close(conn, code)
    return total


def _op_qty(conn, a, o):
    """返回(数量描述, 参考价)。买:约x%≈y股;卖:全部x股。参考价=最新收盘。"""
    code = o["code"]
    close = _latest_close(conn, code)
    if o["side"] == "sell" or o.get("weight", 0) == 0:
        held = a.get("positions", {}).get(code, {}).get("shares", 0)
        return f"全部{held}股", close
    total = _acct_total(conn, a)
    est = util.floor100(total * o.get("weight", 0) / close) if close else 0
    return f"约{o['weight']*100:.0f}%≈{est}股", close


def generate(out_path=None):
    accts = _load_accounts()
    today = util.today_str()
    from db import get_conn
    try:
        conn = get_conn()
        last = conn.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
    except Exception:
        conn = None
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
        # 卡片(含曲线 + 今日操作:代码/名称/参考价/股数/理由)
        pend = a.get("pending", [])
        op_items = []
        for o in pend:
            qty, ref = _op_qty(conn, a, o) if conn else ("", 0)
            nm = ctx_name(conn, o["code"])
            op_items.append(
                f"<div class='op {'sell' if o['side']=='sell' else 'buy'}'>"
                f"<b>{'卖出' if o['side']=='sell' else '买入'} {util.bare(o['code'])} {nm}</b>"
                f"<span class='q'>{qty} · 参考价{util.r2(ref)}</span>"
                f"<span class='reason'>{html.escape(o.get('reason','')[:50])}</span></div>")
        ops = "".join(op_items) or "<div class='op none'>今日无操作(空仓或未到调仓日)</div>"
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
    # 同步生成历史交易页
    try:
        generate_trades(conn)
    except Exception:
        pass
    if conn:
        conn.close()
    return str(out_path)


def generate_trades(conn, out_path=None, cap=800):
    """历史交易页 docs/trades.html:各策略回测全量成交(reports/{sid}_trades.csv)+实盘流水,折叠表,手机友好。"""
    import csv
    sids = ["s2_etf@v1", "s1_dividend@v1", "s3_ma_trend@v1", "s4_smallcap@v1", "s5_grid@v1"]
    live_rows = []
    live_csv = conf.STATE_DIR / "trade_log.csv"
    if live_csv.exists():
        with open(live_csv, encoding="utf-8") as f:
            live_rows = [r for r in csv.DictReader(f) if r.get("status") in ("filled", "cut_liquidity")]

    def table(rows):
        body = ""
        for r in rows[:cap]:
            side = "卖出" if r.get("side") == "sell" else "买入"
            cls = "sell" if r.get("side") == "sell" else "buy"
            nm = ctx_name(conn, r.get("code", "")) if conn else util.bare(r.get("code", ""))
            real = r.get("real_price") or ""
            body += (f"<tr class='{cls}'><td>{r.get('trade_date','')}</td><td>{side}</td>"
                     f"<td>{util.bare(r.get('code',''))} {nm}</td><td>{r.get('shares','')}</td>"
                     f"<td>{r.get('sim_price','')}</td><td>{real}</td>"
                     f"<td class='rs'>{html.escape((r.get('reason','') or '')[:44])}</td></tr>")
        head = ("<table class='t'><tr><th>日期</th><th>方向</th><th>标的</th><th>股数</th>"
                "<th>模拟价</th><th>实盘价</th><th>理由</th></tr>")
        return head + body + "</table>"

    sections = ""
    for sid in sids:
        p = conf.REPORTS_DIR / f"{sid.replace('@','_at_')}_trades.csv"
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("status") in ("filled", "cut_liquidity")]
        rows.sort(key=lambda r: r.get("trade_date", ""), reverse=True)
        note = f"共{len(rows)}笔" + (f",显示最近{cap}笔" if len(rows) > cap else "")
        sections += (f"<details><summary>{_cn(sid)} · 回测成交 {note}</summary>{table(rows)}</details>")
    if live_rows:
        live_rows.sort(key=lambda r: r.get("trade_date", ""), reverse=True)
        sections += (f"<details open><summary>🔴 实盘模拟成交(上线后,共{len(live_rows)}笔)</summary>"
                     f"{table(live_rows)}</details>")
    if not sections:
        sections = "<p class='empty'>暂无交易记录。运行 gen_reports.py 生成回测流水。</p>"

    doc = _TRADES_TEMPLATE.format(today=util.today_str(), sections=sections)
    out_path = out_path or (conf.ROOT / "docs" / "trades.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
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
.op.none{{background:#f3f4f6;color:var(--mut)}}
.op .q{{display:block;font-size:12.5px;color:#374151;margin:3px 0}}.reason{{display:block;color:var(--mut);font-size:12.5px}}
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
<div class="sec"><a href="trades.html" style="color:#2563eb;text-decoration:none">📜 查看各策略全部历史交易记录 →</a></div>
<div class="foot">本页由 report_html.py 自动生成,零外部依赖,可离线打开。<br>
每次买卖以推送与本页『今日操作』为准,按次日开盘价附近跟单。触发🔴熔断的策略已自动清仓降险。</div>
</div></body></html>"""


_TRADES_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>历史交易记录</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1f2937;--mut:#6b7280;--card:#fff;--line:#e5e7eb}}
*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--fg);font-size:14px;line-height:1.5}}
.wrap{{max-width:820px;margin:0 auto;padding:16px}}
h1{{font-size:20px;margin:8px 0}}.sub{{color:var(--mut);font-size:13px;margin-bottom:14px}}
a{{color:#2563eb;text-decoration:none}}
details{{background:var(--card);border-radius:10px;margin:10px 0;padding:6px 12px;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
summary{{cursor:pointer;font-weight:600;padding:8px 0}}
.t{{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0}}
.t th,.t td{{padding:6px 5px;border-bottom:1px solid var(--line);text-align:center;white-space:nowrap}}
.t th{{background:#f0f2f5;color:var(--mut);position:sticky;top:0}}
.t td.rs{{white-space:normal;text-align:left;color:var(--mut);min-width:120px}}
.t tr.buy td:nth-child(2){{color:#065f46}}.t tr.sell td:nth-child(2){{color:#991b1b}}
.tw{{overflow-x:auto}}.empty{{color:var(--mut);text-align:center;padding:40px}}
.foot{{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}}
</style></head><body><div class="wrap">
<h1>📜 历史交易记录</h1>
<div class="sub">生成 {today} · <a href="index.html">← 返回看板</a> · 回测按次日开盘价+真实费用滑点模拟成交</div>
<div class="tw">{sections}</div>
<div class="foot">回测成交=2022年至今历史回放的模拟成交,含费用/滑点/T+1等真实建模。实盘价一列由你在 Streamlit 看板回填。
完整明细也在仓库 reports/*_trades.csv。</div>
</div></body></html>"""


if __name__ == "__main__":
    print("已生成:", generate())
