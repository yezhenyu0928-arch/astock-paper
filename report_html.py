# -*- coding: utf-8 -*-
"""静态HTML看板生成(国内可达方案·V2)。零外部依赖:数据内嵌、内联SVG画曲线、手机自适应。
产出 docs/index.html + docs/trades.html —— GitHub Pages 托管即可,或本地双击打开(无需翻墙/CDN)。
被 run_daily 收尾调用,也可单独:python report_html.py

V2 要点(见 docs/OPTIMIZE_V2.md 卡A):
- 盈红亏绿(A股习惯):涨/盈/买=红 --up,跌/亏/卖=绿 --down。
- 今日操作聚合置顶 + 一键复制 + 实时价渐进增强(腾讯行情,失败静默回昨收)。
- 实盘赛马总览(2026-07-06 起算)+ 各策略卡(介绍/因子权重表/可展开实盘曲线/最新持仓表)。
- 数据新鲜度横幅(按交易日落后数,红/黄两档)。
"""
import json
import glob
import os
import re
import csv
import html
from datetime import datetime

import conf
import util
import backtest as bt
import trade_calendar as cal

# ---------------- 常量 ----------------
LIVE_START = "2026-07-06"          # 实盘模拟期起算日(此前 nav 作为归一基准)
BENCH = "sh000300"                 # 实盘曲线基准:沪深300(库内 daily_bar 或指数)
BUY_BAND = (0.99, 1.02)            # 买入跟单价格带:参考价×[0.99, 1.02]

# 策略介绍字典(卡A A5;S4 如实更名;新策略在此追加,未登记 sid 用兜底文案)
STRAT_META = {
    "s2_etf@v1": {
        "name": "ETF动量轮动", "risk": "★★☆☆☆ 中低", "fit": "≥1万",
        "tagline": "每周持有近期最强的一只宽基/商品ETF，市场整体走弱时自动切入国债ETF避险。",
        "factors": [("20日收益率排名", "50%"), ("60日收益率排名", "50%"),
                    ("绝对动量门槛", "最强者20日收益<0 → 全仓切国债ETF")],
        "rebalance": "每周最后交易日 · 持1只 · 池=沪深300/中证500/红利低波/黄金/纳指/国债 6只ETF"},
    "s1_dividend@v1": {
        "name": "红利低波", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "买入高股息且股价波动小的大盘股并长期持有，靠分红+低回撤积累收益（同类指数近6年年化约13%）。",
        "factors": [("股息率排名", "50%"), ("低波动排名(250日)", "50%"),
                    ("入选门槛", "股息率≥4% + 连续3年现金分红 + 波动率位于池内最低30%")],
        "rebalance": "每月最后交易日 · 等权约6-10只 · 池=沪深300"},
    "s1_dividend@v2": {
        "name": "红利低波·质量增强", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "在红利低波基础上再加盈利质量门槛：只买连续3年ROE>8%、净利润为正的高股息低波股，剔除“高股息陷阱”。",
        "factors": [("股息率排名", "40%"), ("低波动排名(250日)", "30%"), ("ROE盈利质量排名", "30%"),
                    ("入选门槛", "股息率≥4% + 连续3年分红 + 连续3年ROE>8%且净利>0 + 低波后30%")],
        "rebalance": "每月最后交易日 · 等权约6-10只 · 池=沪深300"},
    "s3_ma_trend@v1": {
        "name": "双均线趋势", "risk": "★★★☆☆ 中", "fit": "≥3万",
        "tagline": "20日均线上穿60日均线且放量时买入，跌破20日均线立即卖出；趋势市赚钱、震荡市易被磨损。",
        "factors": [("入场规则", "MA20上穿MA60 + 当日成交量>20日均量×1.5"),
                    ("出场规则", "收盘跌破MA20 清仓该票"), ("排序", "站上MA60幅度(强度)")],
        "rebalance": "每日检查 · 最多持约6只(按资金自适应) · 池=沪深300成分"},
    "s4_smallcap@v1": {
        "name": "沪深300价值精选(小市值演示档)", "risk": "★★★★☆ 中高", "fit": "≥5万",
        "tagline": "在沪深300内选市值偏小、估值偏低、近期不追高的股票。注：受免费数据限制，当前池为沪深300，非真·小盘。",
        "factors": [("总市值(小优先)", "50%"), ("市净率PB(低优先)", "30%"), ("20日动量", "20%")],
        "rebalance": "每月最后交易日 · 等权约6只 · 池=沪深300(过滤后市值最小400只再打分)"},
    "s5_grid@v1": {
        "name": "大盘估值网格", "risk": "★☆☆☆☆ 低", "fit": "≥1万",
        "tagline": "只做沪深300ETF：估值便宜(PE十年分位<30%)时越跌越买，贵(>70%)时越涨越卖，中间2%网格高抛低吸。",
        "factors": [("PE十年分位择时", "<30%只买不卖 / >70%只卖不买"),
                    ("网格", "±2%一档 · 共5档 · 每档约19%仓位")],
        "rebalance": "每日检查 · 标的=沪深300ETF(510300)"},
    "s6_sector@v1": {
        "name": "行业ETF轮动", "risk": "★★★☆☆ 中", "fit": "≥1万",
        "tagline": "每月持有近3-6月最强的行业ETF——产业政策利好最终都会体现为行业涨幅动量，本策略以此自动跟随政策与景气主线，弱市切国债ETF避险。",
        "factors": [("60日动量排名", "40%"), ("120日动量排名", "40%"), ("60日低波动排名", "20%"),
                    ("绝对动量门槛", "最强者60日收益<0 → 全仓切国债ETF")],
        "rebalance": "每月最后交易日 · 持1只 · 池=券商/半导体/医药/消费/军工/新能源/酒/光伏/银行/国债"},
}


def _meta(sid):
    return STRAT_META.get(sid, {
        "name": sid, "risk": "—", "fit": "—",
        "tagline": "（该策略暂无介绍文案）",
        "factors": [], "rebalance": "—"})


def _cn(sid):
    return _meta(sid)["name"]


# ---------------- 数据装载 ----------------
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


def _load_trade_log():
    """实盘成交流水 state/trade_log.csv(2026-07-06 起)。返回按 (sid,code) 分组的买入记录 + 全量行。"""
    path = conf.STATE_DIR / "trade_log.csv"
    rows = []
    if path.exists():
        try:
            with open(path, encoding="utf-8", newline="") as f:
                rows = [r for r in csv.DictReader(f)]
        except Exception:
            rows = []
    return rows


def _buy_info(sid, code, log_rows, fallback_date=""):
    """某策略某票最近一笔成交买入的 (日期, 理由)。无流水则回退持仓 buy_date + '—'。"""
    best = None
    for r in log_rows:
        if (r.get("strategy_id") == sid and r.get("code") == code
                and r.get("side") == "buy" and r.get("status") in ("filled", "cut_liquidity")):
            if best is None or r.get("trade_date", "") >= best.get("trade_date", ""):
                best = r
    if best:
        return best.get("trade_date", fallback_date), (best.get("reason", "") or "—")
    return fallback_date, "—"


def _backtest_summary(sid):
    """从 reports/ 读回测主线 + 入池判定,供看板展示历史参考。"""
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


def _grab(line, pat):
    m = re.search(pat, line or "")
    return m.group(1) if m else "—"


# ---------------- 价格/账户 ----------------
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


# ---------------- 颜色/格式(盈红亏绿) ----------------
def _col(x):
    """收益/涨跌上色:≥0 红(--up),<0 绿(--down)。"""
    return "var(--up)" if (x is not None and x >= 0) else "var(--down)"


def _pct(x, plus=True):
    if x is None:
        return "—"
    return (f"{x:+.1%}" if plus else f"{x:.1%}")


def _pct_span(x):
    return f"<span style='color:{_col(x)}'>{_pct(x)}</span>" if x is not None else "—"


# ---------------- 数据新鲜度 ----------------
def _freshness(conn):
    """按交易日落后数给横幅。返回 (last_date, banner_html)。"""
    try:
        last = conn.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
    except Exception:
        last = None
    if not last:
        return "—", "<div class='banner red'>🛑 数据库为空，请先运行 backfill 工作流，今日暂停跟单。</div>"
    try:
        days = cal._ensure(conn)
    except Exception:
        days = []
    now = util.now_cn()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    past = [d for d in days if d < today]
    if today in days and hhmm >= "15:00":
        expected_last = today
    else:
        expected_last = past[-1] if past else (days[-1] if days else last)
    delayed = len([d for d in days if last < d <= expected_last])
    if delayed <= 0:
        banner = ""
    elif delayed == 1:
        banner = "<div class='banner yellow'>⚠ 数据延迟1个交易日，请留意系统是否正常，谨慎跟单。</div>"
    else:
        banner = (f"<div class='banner red'>🛑 数据已过期{delayed}个交易日，今日暂停跟单，"
                  f"等待系统恢复（检查 Actions / backfill）。</div>")
    return last, banner


# ---------------- 实盘曲线(2026-07-06 起算) ----------------
def _live_series(a):
    """返回 (dates, pcts):自 LIVE_START 起相对基准净值的累计收益率序列(前置 0% 起点)。"""
    hist = a.get("nav_history", [])
    if not hist:
        return [], []
    base = None
    for d, nav in hist:
        if d < LIVE_START:
            base = nav
    if base is None or base <= 0:
        base = hist[0][1] or 1.0
    live = [(d, nav) for d, nav in hist if d >= LIVE_START]
    # 起点:LIVE_START 前最后一个有净值的交易日,收益率 0%
    start_anchor = None
    for d, nav in hist:
        if d < LIVE_START:
            start_anchor = d
    dates = [start_anchor] if start_anchor else []
    pcts = [0.0] if start_anchor else []
    for d, nav in live:
        dates.append(d)
        pcts.append(nav / base - 1)
    if not dates and live:                 # 无 pre-start 锚点的兜底
        d0, n0 = live[0]
        dates, pcts = [d0], [0.0]
        for d, nav in live[1:]:
            dates.append(d); pcts.append(nav / (n0 or 1.0) - 1)
    return dates, pcts


def _bench_series(conn, dmin, dmax):
    """沪深300 在 [dmin,dmax] 的归一化累计收益率(对齐交易日)。"""
    if not dmin:
        return {}, []
    try:
        rows = conn.execute(
            "SELECT trade_date, close FROM daily_bar WHERE code=? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date", (BENCH, dmin, dmax)).fetchall()
    except Exception:
        rows = []
    if not rows:
        return {}, []
    base = rows[0][1] or 1.0
    d2v = {r[0]: (r[1] / base - 1) for r in rows}
    return d2v, [r[0] for r in rows]


def _live_stats(a):
    """实盘累计收益/最大回撤/是否已起步。"""
    dates, pcts = _live_series(a)
    if not pcts:
        return {"total": None, "max_dd": None, "started": False}
    navs = [1 + p for p in pcts]
    if len(navs) < 2:
        return {"total": pcts[-1], "max_dd": 0.0, "started": len(pcts) > 1 or pcts[-1] != 0}
    m = bt.compute_metrics(navs)
    return {"total": navs[-1] / navs[0] - 1, "max_dd": m["max_dd"], "started": True}


def _chart_svg(dates, pcts, bench_d2v, up_color, w=720, h=260):
    """实盘收益率大图:策略线(终值定红/绿) + 沪深300灰虚线 + 坐标轴/网格。单点退化为点+标签。"""
    padL, padR, padT, padB = 46, 16, 16, 28
    bench_dates = [d for d in dates if d in bench_d2v]      # 只在策略有数据的交易日取基准
    bvals = [bench_d2v[d] for d in bench_dates]
    ys = list(pcts) + list(bvals) + [0.0]
    lo, hi = (min(ys), max(ys)) if ys else (-0.01, 0.01)
    if hi - lo < 0.002:
        lo -= 0.01; hi += 0.01
    span = hi - lo
    lo -= span * 0.10; hi += span * 0.10
    all_dates = sorted(set(dates) | set(bench_dates))
    idx = {d: i for i, d in enumerate(all_dates)}
    n = len(all_dates)

    def xof(d):
        return padL + (idx[d] / (n - 1) if n > 1 else 0.5) * (w - padL - padR)

    def yof(v):
        return padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB)

    # y 网格 + 刻度
    grid = ""
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = yof(v)
        emph = "stroke='#cbd5e1'" if abs(v) < (hi - lo) / 200 else "stroke='#eef1f4'"
        grid += f"<line x1='{padL}' y1='{y:.1f}' x2='{w-padR}' y2='{y:.1f}' {emph} stroke-width='1'/>"
        grid += (f"<text x='{padL-6}' y='{y+3:.1f}' text-anchor='end' font-size='10' "
                 f"fill='#94a3b8'>{v*100:+.0f}%</text>")
    # 0% 轴加深
    if lo <= 0 <= hi:
        y0 = yof(0)
        grid += f"<line x1='{padL}' y1='{y0:.1f}' x2='{w-padR}' y2='{y0:.1f}' stroke='#94a3b8' stroke-width='1'/>"

    def polyline(ds, vs, color, dash=""):
        if not ds:
            return ""
        pts = " ".join(f"{xof(d):.1f},{yof(v):.1f}" for d, v in zip(ds, vs))
        line = (f"<polyline fill='none' stroke='{color}' stroke-width='2' "
                f"{'stroke-dasharray=4' if dash else ''} points='{pts}'/>") if len(ds) > 1 else ""
        # 末点圆点(单点时也可见)
        dot = f"<circle cx='{xof(ds[-1]):.1f}' cy='{yof(vs[-1]):.1f}' r='3' fill='{color}'/>"
        return line + dot

    strat_line = polyline(dates, pcts, up_color)
    bench_line = polyline(bench_dates, bvals, "#9aa5b1", dash=True)
    # 末点数值标签(策略)
    label = ""
    if dates:
        lx, ly = xof(dates[-1]), yof(pcts[-1])
        anchor = "end" if lx > w - 60 else "start"
        dx = -6 if anchor == "end" else 6
        label = (f"<text x='{lx+dx:.1f}' y='{ly-6:.1f}' text-anchor='{anchor}' font-size='11' "
                 f"font-weight='700' fill='{up_color}'>{pcts[-1]*100:+.1f}%</text>")
    # x 轴首末日期
    xlab = ""
    if all_dates:
        xlab += (f"<text x='{padL}' y='{h-8}' font-size='10' fill='#94a3b8'>{all_dates[0][5:]}</text>")
        if n > 1:
            xlab += (f"<text x='{w-padR}' y='{h-8}' text-anchor='end' font-size='10' "
                     f"fill='#94a3b8'>{all_dates[-1][5:]}</text>")
    # 图例
    legend = (f"<circle cx='{padL+2}' cy='10' r='3' fill='{up_color}'/>"
              f"<text x='{padL+10}' y='13' font-size='10' fill='#64748b'>本策略</text>"
              f"<line x1='{padL+58}' y1='10' x2='{padL+74}' y2='10' stroke='#9aa5b1' stroke-width='2' stroke-dasharray='4'/>"
              f"<text x='{padL+80}' y='13' font-size='10' fill='#64748b'>沪深300</text>")
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' preserveAspectRatio='xMidYMid meet' "
            f"style='background:#fff;border-radius:8px'>{grid}{bench_line}{strat_line}{label}{xlab}{legend}</svg>")


def _mini_spark(pcts, up_color, w=300, h=40):
    """策略卡 summary 行的迷你走势(无坐标)。"""
    if len(pcts) < 2:
        return ""
    lo, hi = min(pcts + [0.0]), max(pcts + [0.0])
    rng = (hi - lo) or 1
    pts = " ".join(f"{i/(len(pcts)-1)*w:.1f},{h-(v-lo)/rng*(h-6)-3:.1f}" for i, v in enumerate(pcts))
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' height='{h}' preserveAspectRatio='none'>"
            f"<polyline fill='none' stroke='{up_color}' stroke-width='2' points='{pts}'/></svg>")


# ---------------- 今日操作(聚合) ----------------
def _op_calc(conn, a, o):
    """返回 (qty_desc, ref_price, target_amount)。买:约x%≈y股;卖:全部x股。"""
    code = o["code"]
    ref = _latest_close(conn, code)
    if o["side"] == "sell" or o.get("weight", 0) == 0:
        held = a.get("positions", {}).get(code, {}).get("shares", 0)
        return f"全部{held}股", ref, 0.0
    total = _acct_total(conn, a)
    amt = total * o.get("weight", 0)
    est = util.floor100(amt / ref) if ref else 0
    return f"约{o['weight']*100:.0f}%≈{est}股", ref, amt


def _factor_block(meta):
    rows = "".join(f"<tr><td>{html.escape(str(n))}</td><td>{html.escape(str(wt))}</td></tr>"
                   for n, wt in meta.get("factors", []))
    tbl = (f"<table class='fx'><tr><th>选股因子 / 规则</th><th>权重 / 说明</th></tr>{rows}</table>"
           if rows else "")
    return (f"<div class='tagline'>{html.escape(meta['tagline'])}</div>{tbl}"
            f"<div class='rb'>调仓：{html.escape(meta['rebalance'])} · 适合资金：{html.escape(meta['fit'])}</div>")


def _positions_table(conn, a, sid, log_rows):
    pos = a.get("positions", {})
    cash = a.get("cash", 0)
    total = _acct_total(conn, a)
    init = a.get("init_capital", cash) or cash
    if not pos:
        return (f"<div class='pos-empty'>当前空仓（现金 100%，约 {cash:,.0f} 元）</div>")
    body = ""
    today = util.today_str()
    for code, p in pos.items():
        shares = p.get("shares", 0)
        avg = p.get("avg_cost", 0)
        last = _latest_close(conn, code)
        mv = shares * last
        pnl = (last / avg - 1) if avg else None
        posp = (mv / total) if total else 0
        nm = ctx_name(conn, code)
        bdate, reason = _buy_info(sid, code, log_rows, fallback_date=p.get("buy_date", ""))
        hold = _hold_days(bdate, today)
        body += (
            f"<tr><td class='l'>{util.bare(code)} {html.escape(nm)}</td>"
            f"<td>{shares}</td><td>{util.r2(avg)}</td><td>{util.r2(last)}</td>"
            f"<td>{mv:,.0f}</td><td style='color:{_col(pnl)}'>{_pct(pnl)}</td>"
            f"<td>{posp*100:.0f}%</td></tr>"
            f"<tr class='why'><td colspan='7'>买入 {bdate or '—'} · 持有{hold}天 · "
            f"理由：{html.escape(reason)}</td></tr>")
    tot_pnl = (total / init - 1) if init else None
    body += (f"<tr class='sum'><td class='l'>现金</td><td colspan='3'></td>"
             f"<td>{cash:,.0f}</td><td colspan='2'></td></tr>"
             f"<tr class='sum'><td class='l'>合计总资产</td><td colspan='3'></td>"
             f"<td>{total:,.0f}</td><td style='color:{_col(tot_pnl)}'>{_pct(tot_pnl)}</td><td></td></tr>")
    return ("<table class='pos'><tr><th>标的</th><th>股数</th><th>成本</th><th>最新</th>"
            "<th>市值</th><th>盈亏</th><th>仓位</th></tr>" + body + "</table>")


def _hold_days(bdate, today):
    try:
        a = datetime.strptime(bdate[:10], "%Y-%m-%d")
        b = datetime.strptime(today[:10], "%Y-%m-%d")
        return max(0, (b - a).days)
    except Exception:
        return 0


_WD = "一二三四五六日"


def _exec_date(pendings):
    """待执行订单的开盘执行日 = 其信号日的下一个交易日(与撮合口径一致)。返回 (date_str, 周X, 是否=今天)。"""
    sig = max((o.get("signal_date", "") for o in pendings), default="")
    if not sig:
        return None, "", False
    try:
        d = cal.next_trade_day(sig)
        wd = _WD[datetime.strptime(d, "%Y-%m-%d").weekday()]
    except Exception:
        return None, "", False
    return d, wd, (d == util.today_str())


# ---------------- 主生成 ----------------
def generate(out_path=None):
    accts = _load_accounts()
    log_rows = _load_trade_log()
    today = util.today_str()
    from db import get_conn
    try:
        conn = get_conn()
    except Exception:
        conn = None
    last, banner = _freshness(conn) if conn else ("—", "")

    # ===== 操作计划聚合(按执行日,而非"今日";每条明确标注所属策略) =====
    all_pending = [o for a in accts.values() for o in a.get("pending", [])]
    exec_d, exec_wd, is_today = _exec_date(all_pending)
    if all_pending and exec_d:
        ops_title = ("今日操作" if is_today else f"操作计划（{exec_d[5:]} 周{exec_wd} 开盘跟单）")
        head_txt = (f"【操作计划】将于 {exec_d} 周{exec_wd} 开盘按价格带手动跟单"
                    if not is_today else "【今日操作】按开盘价附近手动跟单")
    else:
        ops_title = "操作计划"
        head_txt = ""

    op_rows_html = []
    copy_lines = []
    for sid, a in sorted(accts.items()):
        for o in a.get("pending", []):
            qty, ref, amt = _op_calc(conn, a, o) if conn else ("", 0, 0)
            nm = ctx_name(conn, o["code"]) if conn else util.bare(o["code"])
            is_sell = o["side"] == "sell" or o.get("weight", 0) == 0
            side_cn = "卖出" if is_sell else "买入"
            cls = "sell" if is_sell else "buy"
            band = ""
            if not is_sell and ref:
                band = (f"<div class='band'>跟单价格带：{util.r2(ref*BUY_BAND[0])} ~ {util.r2(ref*BUY_BAND[1])}"
                        f"（高于上带建议减半或放弃，勿追高）</div>")
            op_rows_html.append(
                f"<div class='op {cls}' data-code='{o['code']}' data-side='{o['side']}' "
                f"data-amount='{amt:.0f}' data-ref='{util.r2(ref)}'>"
                f"<div class='op-hd'><span class='chip'>{_cn(sid)}</span>"
                f"<b>{side_cn} {util.bare(o['code'])} {html.escape(nm)}</b></div>"
                f"<span class='q'>{qty} · 参考价 {util.r2(ref)}</span>"
                f"<span class='reason'>{html.escape(o.get('reason', ''))}</span>{band}</div>")
            copy_lines.append(f"【{_cn(sid)}】{side_cn} {util.bare(o['code'])} {nm} {qty} 参考价{util.r2(ref)}")
    if op_rows_html:
        copy_js = json.dumps((f"操作计划 {exec_d}(周{exec_wd})开盘跟单：\n" if exec_d else "") + "\n".join(copy_lines),
                             ensure_ascii=False)
        ops_section = (
            f"<div class='ops-head'><span>{head_txt}</span>"
            f"<button class='copybtn' onclick='copyOps()'>📋 复制指令</button></div>"
            + "".join(op_rows_html)
            + "<div class='op-note'>每条操作左侧标签为所属策略；页面会尝试用实时价校准股数与金额（失败则显示“昨收参考”）。</div>"
            + f"<script>var OPS_TEXT={copy_js};function copyOps(){{"
              "if(navigator.clipboard){navigator.clipboard.writeText(OPS_TEXT).then(function(){alert('已复制操作指令');},"
              "function(){alert('复制失败，请手动选择');});}else{alert('浏览器不支持一键复制，请手动选择');}}</script>")
    else:
        ops_section = ("<div class='op none'>暂无待执行操作（各策略空仓或未到调仓日）。"
                       "上线首个交易日 2026-07-06 起，有操作时此处按策略列出。</div>")

    # ===== 实盘赛马总览 =====
    ov = ""
    for sid, a in sorted(accts.items()):
        ls = _live_stats(a)
        st = "🔴熔断" if a.get("frozen") else "🟢正常"
        bt_line, verdict = _backtest_summary(sid)
        bt_cum = _grab(bt_line, r"累计([+\-\d.]+%)")
        bt_cal = _grab(bt_line, r"Calmar([+\-\d.]+)")
        npos = len(a.get("positions", {}))
        total_col = _col(ls["total"]) if ls["total"] is not None else "var(--mut)"
        total_txt = _pct(ls["total"]) if ls["started"] else "今日起步"
        ddtxt = _pct(ls["max_dd"], plus=False) if ls["max_dd"] is not None else "—"
        ov += (f"<tr><td class='l'>{_cn(sid)}</td>"
               f"<td style='color:{total_col};font-weight:700'>{total_txt}</td>"
               f"<td>{ddtxt}</td><td>{a.get('nav',1):.3f}</td><td>{npos}</td><td>{st}</td>"
               f"<td class='ref'>{bt_cum}·Calmar{bt_cal}·{verdict or '—'}</td></tr>")
    overview = ("<table class='ov'><tr><th>策略</th><th>实盘累计</th><th>最大回撤</th><th>净值</th>"
                "<th>持仓</th><th>状态</th><th>回测参考(2022→今)</th></tr>" + ov + "</table>") if accts else ""

    # ===== 各策略卡 =====
    cards = ""
    for sid, a in sorted(accts.items()):
        meta = _meta(sid)
        ls = _live_stats(a)
        st = "🔴熔断" if a.get("frozen") else "🟢正常"
        dates, pcts = _live_series(a)
        up_color = "#d92b2b" if (pcts and pcts[-1] >= 0) else "#0a9e6b"
        bench_d2v, _ = _bench_series(conn, dates[0], dates[-1]) if (conn and dates) else ({}, [])
        chart = _chart_svg(dates, pcts, bench_d2v, up_color) if dates else "<div class='pos-empty'>曲线将于 2026-07-06 起累积</div>"
        cur_txt = _pct(ls["total"]) if ls["started"] else "今日起步"
        bt_line, _ = _backtest_summary(sid)
        bt_html = f"<div class='bt'>📈 回测(2022→今)：{html.escape(bt_line)}</div>" if bt_line else ""
        # 该策略今日操作
        op_items = []
        for o in a.get("pending", []):
            qty, ref, _amt = _op_calc(conn, a, o) if conn else ("", 0, 0)
            nm = ctx_name(conn, o["code"]) if conn else util.bare(o["code"])
            is_sell = o["side"] == "sell" or o.get("weight", 0) == 0
            op_items.append(
                f"<div class='op {'sell' if is_sell else 'buy'}'>"
                f"<b>{'卖出' if is_sell else '买入'} {util.bare(o['code'])} {html.escape(nm)}</b>"
                f"<span class='q'>{qty} · 参考价 {util.r2(ref)}</span>"
                f"<span class='reason'>{html.escape(o.get('reason', ''))}</span></div>")
        ops = "".join(op_items) or "<div class='op none'>无待执行操作</div>"
        cards += (
            f"<div class='card'>"
            f"<div class='card-h'><b>{meta['name']}</b><span class='risk'>{meta['risk']}</span>"
            f"<span class='stat'>{st}</span></div>"
            f"{_factor_block(meta)}"
            f"<details><summary>📈 实盘收益率曲线（07-06 起）当前 "
            f"<span style='color:{up_color};font-weight:700'>{cur_txt}</span></summary>{chart}{bt_html}</details>"
            f"<div class='sub2'>最新持仓</div>{_positions_table(conn, a, sid, log_rows)}"
            f"<div class='sub2'>操作计划</div>{ops}"
            f"</div>")
    if not accts:
        cards = "<p class='empty'>暂无策略状态。请先运行 run_daily.py 或回测生成 state/。</p>"

    body = (
        f"<h1>📊 A股模拟跟单看板</h1>"
        f"<div class='sub'>生成 {today} · 数据最新 {last} · 实盘模拟期自 2026-07-06 起 · 模拟/历史不代表未来，非投资建议，人工跟单</div>"
        f"{banner}"
        f"<div class='sec'>{ops_title}</div>{ops_section}"
        f"<div class='sec'>实盘赛马总览（2026-07-06 起算）</div>{overview}"
        f"<div class='sec'>各策略详情</div>{cards}"
        f"<div class='sec'><a href='trades.html'>📜 查看全部历史交易记录 →</a></div>"
        f"{_FOOTER}")
    html_doc = f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>" \
               f"<meta name='viewport' content='width=device-width, initial-scale=1'>" \
               f"<title>A股模拟跟单看板</title>{_STYLE}</head><body><div class='wrap'>{body}</div>{_LIVE_JS}</body></html>"

    out_path = out_path or (conf.ROOT / "docs" / "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    try:
        generate_trades(conn)
    except Exception:
        pass
    if conn:
        conn.close()
    return str(out_path)


def generate_trades(conn, out_path=None, cap=800):
    """历史交易页:实盘成交(2026-07-06 起)置顶展开 + 各策略回测成交折叠靠后。买红卖绿。"""
    sids = ["s2_etf@v1", "s1_dividend@v1", "s3_ma_trend@v1", "s4_smallcap@v1", "s5_grid@v1", "s6_sector@v1"]
    live_rows = []
    live_csv = conf.STATE_DIR / "trade_log.csv"
    if live_csv.exists():
        with open(live_csv, encoding="utf-8") as f:
            live_rows = [r for r in csv.DictReader(f) if r.get("status") in ("filled", "cut_liquidity")]

    def table(rows, truncate=True):
        body = ""
        for r in rows[:cap]:
            is_sell = r.get("side") == "sell"
            side = "卖出" if is_sell else "买入"
            cls = "sell" if is_sell else "buy"
            nm = ctx_name(conn, r.get("code", "")) if conn else util.bare(r.get("code", ""))
            real = r.get("real_price") or ""
            reason = r.get("reason", "") or ""
            reason = html.escape(reason[:44]) if truncate else html.escape(reason)
            body += (f"<tr class='{cls}'><td>{r.get('trade_date','')}</td><td>{side}</td>"
                     f"<td>{util.bare(r.get('code',''))} {nm}</td><td>{r.get('shares','')}</td>"
                     f"<td>{r.get('sim_price','')}</td><td>{real}</td>"
                     f"<td class='rs'>{reason}</td></tr>")
        head = ("<table class='t'><tr><th>日期</th><th>方向</th><th>标的</th><th>股数</th>"
                "<th>模拟价</th><th>实盘价</th><th>理由</th></tr>")
        return head + body + "</table>"

    sections = ""
    if live_rows:
        live_rows.sort(key=lambda r: r.get("trade_date", ""), reverse=True)
        sections += (f"<details open><summary>🔴 实盘模拟成交 · 全部（2026-07-06 起，共{len(live_rows)}笔）</summary>"
                     f"{table(live_rows, truncate=False)}</details>")
        # 按策略筛选(纯 HTML 分组,卡E):每个有成交的策略一个可折叠子块
        by_sid = {}
        for r in live_rows:
            by_sid.setdefault(r.get("strategy_id", "?"), []).append(r)
        if len(by_sid) > 1:
            sections += "<div class='subhead'>按策略筛选</div>"
            for sid in sorted(by_sid):
                rows = by_sid[sid]
                sections += (f"<details><summary>{_cn(sid)}（{len(rows)}笔）</summary>"
                             f"{table(rows, truncate=False)}</details>")
    else:
        sections += ("<details open><summary>🔴 实盘模拟成交（2026-07-06 起）</summary>"
                     "<p class='empty'>实盘模拟期尚未产生成交（首个交易日为 2026-07-06）。上线后此处将按“全部 + 按策略筛选”分组展示。</p></details>")
    for sid in sids:
        p = conf.REPORTS_DIR / f"{sid.replace('@','_at_')}_trades.csv"
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("status") in ("filled", "cut_liquidity")]
        rows.sort(key=lambda r: r.get("trade_date", ""), reverse=True)
        note = f"共{len(rows)}笔" + (f"，显示最近{cap}笔" if len(rows) > cap else "")
        sections += (f"<details><summary>{_cn(sid)} · 历史回放成交（2022→今，仅供参考）{note}</summary>{table(rows)}</details>")

    doc = (f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>历史交易记录</title>{_TRADES_STYLE}</head><body><div class='wrap'>"
           f"<h1>📜 历史交易记录</h1>"
           f"<div class='sub'>生成 {util.today_str()} · <a href='index.html'>← 返回看板</a> · "
           f"回测按次日开盘价+真实费用滑点模拟成交 · 买入红 / 卖出绿</div>"
           f"<div class='tw'>{sections}</div>"
           f"<div class='foot'>实盘区为 2026-07-06 起真实跟踪的模拟成交；历史回放区为 2022 年至今回测(含费用/滑点/T+1)，仅供参考。"
           f"实盘价一列由你在 Streamlit 看板回填。完整明细见仓库 reports/*_trades.csv。</div>"
           f"</div></body></html>")
    out_path = out_path or (conf.ROOT / "docs" / "trades.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return str(out_path)


# ---------------- 样式 / 脚本 ----------------
_STYLE = """<style>
:root{--bg:#f6f7f9;--fg:#1f2937;--mut:#6b7280;--card:#fff;--line:#e5e7eb;--up:#d92b2b;--down:#0a9e6b}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--fg);font-size:15px;line-height:1.5}
.wrap{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}.sub{color:var(--mut);font-size:13px;margin-bottom:12px}
.sec{margin:22px 0 8px;font-size:16px;font-weight:600}.sec a{color:#2563eb;text-decoration:none}
.banner{padding:10px 12px;border-radius:8px;font-size:13.5px;font-weight:600;margin:10px 0}
.banner.yellow{background:#fef9c3;color:#854d0e;border:1px solid #fde68a}
.banner.red{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;font-size:13.5px}
th,td{padding:8px 7px;text-align:center;border-bottom:1px solid var(--line)}
th{background:#f0f2f5;color:var(--mut);font-weight:600}td.l,th:first-child{text-align:left}
.ov td.ref{color:var(--mut);font-size:12px}
.ops-head{display:flex;justify-content:space-between;align-items:center;margin:6px 0}
.ops-head span{font-size:13.5px;color:var(--mut)}
.copybtn{background:#2563eb;color:#fff;border:none;border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer}
.op{padding:9px 12px;border-radius:9px;margin:6px 0;font-size:14px}
.op.buy{background:#fef2f2;color:#991b1b}.op.sell{background:#ecfdf5;color:#065f46}
.op.none{background:#f3f4f6;color:var(--mut)}
.op .op-hd{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:2px}
.op .chip{display:inline-block;background:#fff;border:1px solid rgba(0,0,0,.18);color:#1f2937;font-size:11.5px;font-weight:700;padding:1px 9px;border-radius:999px;white-space:nowrap}
.op .q{display:block;font-size:13px;color:#374151;margin:3px 0}
.op .reason{display:block;color:var(--mut);font-size:12.5px}
.op .band{font-size:11.5px;color:#9a3412;margin-top:3px}
.op .stale{color:var(--mut);font-size:11.5px}.op .warn{color:#b91c1c;font-weight:600}
.op-note{color:var(--mut);font-size:12px;margin:4px 2px}
.card{background:var(--card);border-radius:12px;padding:14px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:6px}
.card-h b{font-size:15.5px}.card-h .risk{color:#a16207;font-size:12px;margin-left:auto}
.card-h .stat{font-size:12px;color:var(--mut)}
.tagline{font-size:13px;color:#374151;margin:4px 0 8px}
.fx{margin:6px 0;font-size:12.5px}.fx th{font-size:12px}
.rb{font-size:12px;color:var(--mut);margin:6px 0}
details{margin:8px 0}summary{cursor:pointer;font-size:13.5px;color:#334155;padding:4px 0}
.sub2{font-size:13px;font-weight:600;color:#334155;margin:10px 0 4px}
.pos{font-size:12.5px}.pos .why td{text-align:left;color:var(--mut);font-size:11.5px;background:#fafafa;padding:4px 8px}
.pos .sum td{font-weight:600;background:#f8fafc}
.pos-empty{color:var(--mut);font-size:13px;padding:10px;background:#f8fafc;border-radius:8px}
.bt{background:#f0f7ff;color:#1e40af;font-size:12px;padding:6px 10px;border-radius:8px;margin:6px 0}
.foot{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}
.foot b{color:#374151}.empty{color:var(--mut);text-align:center;padding:40px}
</style>"""

_FOOTER = """<div class="foot">
<b>怎么用</b>：每天 18:00 前后微信收到推送，次日开盘按『操作计划』的价格带手动跟单（每条已标注所属策略）；没收到心跳=系统故障，当天别跟单。<br>
<b>观察期纪律</b>：第0-2周只看不投；满季度后若赛马正常，5万低风险参考配比 = 大盘网格30%+ETF轮动25%+红利低波25%+行业轮动10%+现金10%（S3/S4仅观察）。任何策略熔断→该部分转现金等复核。<br>
<b>数据来源</b>：sina/baostock/东财 免费源，每交易日17:40自动更新；页面顶部横幅提示数据新鲜度。<br>
<b>免责</b>：本页由 report_html.py 自动生成，零外部依赖可离线打开；模拟/历史表现不代表未来，不构成投资建议，请仅用可承受损失的资金。
</div>"""

# 实时价渐进增强(腾讯行情 qt.gtimg.cn):<script>跨域取数,失败/超时3s静默回昨收。全程 try/catch,无 Promise 悬挂。
_LIVE_JS = """<script>
(function(){
  try{
    var ops=document.querySelectorAll('.op[data-code]');
    if(!ops.length) return;
    var set={}, codes=[];
    ops.forEach(function(el){var c=el.getAttribute('data-code'); if(!set[c]){set[c]=1;codes.push(c);}});
    var done=false;
    function mark(){ops.forEach(function(el){var q=el.querySelector('.q'); if(q&&q.innerHTML.indexOf('昨收参考')<0){q.innerHTML+=" <span class='stale'>(昨收参考)</span>";}});}
    function ft(t){return (t&&t.length>=12)?(t.substr(8,2)+':'+t.substr(10,2)):'';}
    function apply(){
      ops.forEach(function(el){try{
        var code=el.getAttribute('data-code'); var v=window['v_'+code];
        var q=el.querySelector('.q'); if(!v){return;}
        var f=v.split('~'); var cur=parseFloat(f[3]); var prev=parseFloat(f[4]);
        if(!(cur>0)){return;}
        var chg=prev>0?(cur/prev-1):0; var col=chg>=0?'var(--up)':'var(--down)'; var sg=chg>=0?'+':'';
        var line="实时价 "+cur.toFixed(3)+" <span style='color:"+col+"'>"+sg+(chg*100).toFixed(2)+"%</span> "+ft(f[30]);
        if(el.getAttribute('data-side')==='buy'){
          var amt=parseFloat(el.getAttribute('data-amount'))||0;
          var ref=parseFloat(el.getAttribute('data-ref'))||prev;
          var sh=Math.floor(amt/cur/100)*100;
          line+=" · 约"+sh+"股 ≈"+Math.round(sh*cur)+"元";
          if(ref>0 && cur>ref*1.02){line+=" <span class='warn'>⚠已超跟单价格带，建议减半或放弃</span>";}
        }
        if(q){q.innerHTML=line;}
      }catch(e){}});
    }
    var timer=setTimeout(function(){if(!done){done=true;mark();}},3000);
    var s=document.createElement('script');
    s.src='https://qt.gtimg.cn/q='+codes.join(',');
    s.charset='gbk';
    s.onload=function(){if(done)return;done=true;clearTimeout(timer);apply();};
    s.onerror=function(){if(done)return;done=true;clearTimeout(timer);mark();};
    document.head.appendChild(s);
  }catch(e){}
})();
</script>"""

_TRADES_STYLE = """<style>
:root{--bg:#f6f7f9;--fg:#1f2937;--mut:#6b7280;--card:#fff;--line:#e5e7eb;--up:#d92b2b;--down:#0a9e6b}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--fg);font-size:14px;line-height:1.5}
.wrap{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}.sub{color:var(--mut);font-size:13px;margin-bottom:14px}a{color:#2563eb;text-decoration:none}
details{background:var(--card);border-radius:10px;margin:10px 0;padding:6px 12px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
summary{cursor:pointer;font-weight:600;padding:8px 0}
.t{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0}
.t th,.t td{padding:6px 5px;border-bottom:1px solid var(--line);text-align:center}
.t th{background:#f0f2f5;color:var(--mut)}
.t td.rs{white-space:normal;text-align:left;color:var(--mut);min-width:120px}
.t tr.buy td:nth-child(2){color:var(--up);font-weight:600}.t tr.sell td:nth-child(2){color:var(--down);font-weight:600}
.tw{overflow-x:auto}.empty{color:var(--mut);text-align:center;padding:24px}
.subhead{margin:16px 2px 6px;font-size:14px;font-weight:600;color:#334155}
.foot{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}
</style>"""


if __name__ == "__main__":
    print("已生成:", generate())
