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
        "tagline": "每周持有近期最强的一只宽基/商品ETF，市场整体走弱时自动切入国债ETF避险。macro_score调节仓位：紧缩期60%、扩张期满仓。",
        "factors": [("20日收益率排名", "50%"), ("60日收益率排名", "50%"),
                    ("绝对动量门槛", "最强者20日收益<0 → 全仓切国债ETF"),
                    ("宏观调节(macro_score)", "紧缩降仓60%/扩张满仓+M2数据附操作理由")],
        "rebalance": "每周最后交易日 · 持1只 · 池=沪深300/中证500/红利低波/黄金/纳指/国债 6只ETF"},
    "s1_dividend@v1": {
        "name": "红利低波", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "买入高股息且股价波动小的大盘股并长期持有，靠分红+低回撤积累收益（同类指数近6年年化约13%）。",
        "factors": [("股息率排名", "50%"), ("低波动排名(250日)", "50%"),
                    ("入选门槛", "股息率≥4% + 连续3年现金分红 + 波动率位于池内最低30%")],
        "rebalance": "每月最后交易日 · 等权约6-10只 · 池=沪深300"},
    "s1_dividend@v3": {
        "name": "红利低波·Barra7因子增强", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "在红利低波基础上叠加 7 个 Barra 风格因子（VALUE/QUALITY/-VOLATILITY/SIZE/-BETA/EARNINGS_YIELD/-LEVERAGE）进行去极值·标准化·正交化复合评分，宏观regime自适应权重，行业约束降低集中度。",
        "factors": [("VALUE(EP+BP+DY复合)", "20%"), ("QUALITY(ROE+低杠杆)", "15%"), ("低波动(-VOLATILITY)", "15%"),
                    ("SIZE(偏大盘)", "10%"), ("低Beta(-BETA,防御)", "15%"), ("盈利收益(EARNINGS_YIELD)", "15%"),
                    ("低杠杆(-LEVERAGE)", "10%"),
                    ("入选门槛", "股息率≥4% + 连续3年分红 + 连续3年ROE>8%且净利>0"),
                    ("数据处理", "MAD去极值→z-score标准化→Gram-Schmidt正交化(消除共线性)"),
                    ("宏观自适应", "收缩期自动提高LOW_VOL/BETA/LEVERAGE负向权重"),
                    ("风险控制", "特质风险>30%降仓+行业≤2只")],
        "rebalance": "每月最后交易日 · 等权约6-10只 · 池=沪深300"},
    "s4_smallcap@v2": {
        "name": "多因子价值增强(沪深300)·7因子", "risk": "★★★★☆ 中高", "fit": "≥5万",
        "tagline": "在沪深300内用 7 个 Barra 因子选小市值+价值+动量+流动性+高弹性+质优股，宏观regime自适+行业动量倾斜，残差波动帽过滤。",
        "factors": [("-SIZE(小市值,负向)", "20%"), ("MOMENTUM(RSTR 12-1月)", "20%"), ("VALUE(EP+BP+DY)", "15%"),
                    ("LIQUIDITY(流动性)", "10%"), ("BETA(牛市弹性)", "15%"), ("EARNINGS_YIELD(盈利收益)", "10%"),
                    ("QUALITY(ROE+低杠杆)", "10%"),
                    ("风险过滤", "剔除残差波动z>1.28的高波股"),
                    ("宏观自适应", "扩张期提MOMENTUM/BETA权重，收缩期提VALUE/QUALITY防御"),
                    ("行业动量倾斜", "所属行业近60日涨幅前30%获加分"),
                    ("行业约束", "每行业≤3只")],
        "rebalance": "每月最后交易日 · 等权约6只 · 池=沪深300"},
    "s1_dividend@v2": {
        "name": "红利低波·质量增强", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "在红利低波基础上再加盈利质量门槛：只买连续3年ROE>8%、净利润为正的高股息低波股，剔除“高股息陷阱”。",
        "factors": [("股息率排名", "40%"), ("低波动排名(250日)", "30%"), ("ROE盈利质量排名", "30%"),
                    ("入选门槛", "股息率≥4% + 连续3年分红 + 连续3年ROE>8%且净利>0 + 低波后30%")],
        "rebalance": "每月最后交易日 · 等权约6-10只 · 池=沪深300"},
    "s3_ma_trend@v1": {
        "name": "双均线趋势", "risk": "★★★☆☆ 中", "fit": "≥3万",
        "tagline": "20日均线上穿60日均线且放量时买入，跌破20日均线立即卖出。macro_score调节放量阈值：紧缩1.5x防假突破、扩张0.7x积极入场。",
        "factors": [("入场规则", "MA20上穿MA60 + 当日成交量>20日均量×阈值"),
                    ("出场规则", "收盘跌破MA20 清仓该票"), ("排序", "站上MA60幅度(强度)"),
                    ("宏观调节(macro_score)", "紧缩vol_mult×1.5/扩张×0.7+M2数据附买入理由")],
        "rebalance": "每日检查 · 最多持约6只(按资金自适应) · 池=沪深300成分"},
    "s4_smallcap@v1": {
        "name": "沪深300价值精选(小市值演示档)", "risk": "★★★★☆ 中高", "fit": "≥5万",
        "tagline": "在沪深300内选市值偏小、估值偏低、近期不追高的股票。注：受免费数据限制，当前池为沪深300，非真·小盘。",
        "factors": [("总市值(小优先)", "50%"), ("市净率PB(低优先)", "30%"), ("20日动量", "20%")],
        "rebalance": "每月最后交易日 · 等权约6只 · 池=沪深300(过滤后市值最小400只再打分)"},
    "s5_grid@v1": {
        "name": "大盘估值网格", "risk": "★☆☆☆☆ 低", "fit": "≥1万",
        "tagline": "只做沪深300ETF：估值便宜时越跌越买，贵时越涨越卖。macro_score调节步长(紧缩放宽/扩张收窄)+动态档数(±2档)。",
        "factors": [("PE十年分位择时", "<30%只买不卖 / >70%只卖不买"),
                    ("网格步长", "±2%基准 · macro_score调节±50%"),
                    ("总档数", "5档基准 · macro_score调节±2档"),
                    ("宏观调节(macro_score)", "步长+档数+M2数据")],
        "rebalance": "每日检查 · 标的=沪深300ETF(510300)"},
    "s6_sector@v1": {
        "name": "行业ETF轮动", "risk": "★★★☆☆ 中", "fit": "≥1万",
        "tagline": "每月持有近3-6月最强的行业ETF。macro_score调节仓位(紧缩50%/扩张满仓)+避险阈值(紧缩0收益就切/扩张-3%才切)。",
        "factors": [("60日动量排名", "40%"), ("120日动量排名", "40%"), ("60日低波动排名", "20%"),
                    ("绝对动量门槛", "最强者60日收益<阈值 → 全仓切国债ETF"),
                    ("宏观调节(macro_score)", "仓位调节+避险阈值+M2数据")],
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
    """加载 state/*.json 中的策略账户。同时补入 registry 中已注册但尚无 state 文件的策略（占位，净值1.0）。"""
    out = {}
    for f in glob.glob(str(conf.STATE_DIR / "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
            if "strategy_id" in d:
                out[d["strategy_id"]] = d
        except Exception:
            pass
    # 补入 registry 中已注册但无 state 的新策略（占位展示）
    try:
        reg = conf.load_registry()
        for sid, entry in reg.items():
            if sid not in out:
                out[sid] = {
                    "strategy_id": sid, "cash": 50000, "nav": 1.0, "nav_history": [],
                    "positions": {}, "pending": [], "frozen": False,
                    "init_capital": 50000, "_placeholder": True,
                }
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


def _load_factor_exposures():
    """读取 state/factor_exposure.json。不存在或异常返回 None。"""
    path = conf.STATE_DIR / "factor_exposure.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _exposure_html(sid, exp_data):
    """为某个策略生成因子暴露区 HTML（CSS条形图+表格+Chart.js画布）。
    etf_only 策略显示说明；数据缺失时返回空字符串。"""
    if exp_data is None:
        return ""
    strats = exp_data.get("strategies", {})
    if sid not in strats:
        return ""
    se = strats[sid]
    if se.get("etf_only"):
        return ('<details class="factor-exposure"><summary>⚖️ 风格暴露（ETF策略）</summary>'
                '<div class="exp-note">ETF策略持仓不映射个股风格因子，无法计算风格暴露。</div></details>')
    exposures = se.get("exposures", {})
    pred_vol = se.get("pred_vol")
    factors_order = ["size", "beta", "momentum", "resvol", "liquidity", "btop"]
    factor_labels = {
        "size": "市值(Size)", "beta": "贝塔(Beta)", "momentum": "动量(Mom)",
        "resvol": "残差波动(ResVol)", "liquidity": "流动性(Liq)", "btop": "账面市值比(BTOP)"
    }
    bars = ""
    for f in factors_order:
        val = exposures.get(f)
        if val is None:
            continue
        clamped = max(-2.0, min(2.0, val))
        pct = abs(clamped) / 4.0 * 100  # 全幅 [-2,2] → 100% 条宽
        if clamped >= 0:
            bar = f'<div class="exp-fill exp-pos" style="width:{pct}%;left:50%"></div>'
        else:
            bar = f'<div class="exp-fill exp-neg" style="width:{pct}%;left:{50-pct}%"></div>'
        bars += (f'<div class="exp-bar-row">'
                f'<span class="exp-label">{factor_labels.get(f, f)}</span>'
                f'<div class="exp-bar-wrap">{bar}<div class="exp-zero-line"></div></div>'
                f'<span class="exp-val">{val:+.2f}</span></div>')
    vol_str = f"（年化 {pred_vol*100:.1f}%）" if pred_vol is not None else ""
    vol_line = f'<div class="exp-vol">预测年化波动：{pred_vol*100:.1f}%</div>' if pred_vol is not None else ""
    chart_id = f"exposureChart_{sid.replace('@','_').replace('.','_')}"
    tbl_rows = "".join(
        f"<tr><td>{factor_labels.get(f, f)}</td><td>{exposures.get(f, 0):+.2f}</td></tr>"
        for f in factors_order if f in exposures)
    return (f'<details class="factor-exposure">'
            f'<summary>⚖️ 风格暴露与预测波动{vol_str}</summary>'
            f'{vol_line}'
            f'<div class="exp-chart-container">'
            f'<canvas id="{chart_id}" width="400" height="200"></canvas></div>'
            f'<div class="exp-bars">{bars}</div>'
            f'<table class="exposure-table"><thead><tr><th>因子</th><th>暴露值(z分)</th></tr></thead>'
            f'<tbody>{tbl_rows}</tbody></table>'
            f'<div class="exp-note"><a href="methodology.html#risk-model">暴露值如何解读？→</a> · '
            f'策略暴露数据将于下次策略运行时更新</div>'
            f'</details>')


def _exposure_chart_js(exp_data):
    """生成 Chart.js 渲染脚本（内嵌暴露数据）。Chart.js CDN 未加载时静默跳过。"""
    if exp_data is None:
        return ""
    strats = exp_data.get("strategies", {}) if exp_data else {}
    factors_order = exp_data.get("factors", ["size", "beta", "momentum", "resvol", "liquidity", "btop"])
    chart_entries = []
    for sid, se in strats.items():
        if se.get("etf_only"):
            continue
        exposures = se.get("exposures", {})
        if not exposures:
            continue
        chart_id = f"exposureChart_{sid.replace('@','_').replace('.','_')}"
        labels = json.dumps([f[:4] for f in factors_order if f in exposures], ensure_ascii=False)
        values = [exposures.get(f, 0) for f in factors_order if f in exposures]
        colors = ["#3b82f6" if v >= 0 else "#ef4444" for v in values]
        chart_entries.append(f"""
  (function(){{
    var c=document.getElementById('{chart_id}');
    if(!c)return;
    try{{
      new Chart(c,{{type:'bar',
        data:{{labels:{labels},datasets:[{{data:{json.dumps(values)},backgroundColor:{json.dumps(colors)}}}]}},
        options:{{responsive:true,maintainAspectRatio:false,
          plugins:{{legend:{{display:false}},title:{{display:true,text:'{html.escape(_cn(sid))} 风格暴露',font:{{size:13}}}}}},
          scales:{{y:{{title:{{display:true,text:'z分'}},min:-2,max:2}}}}}});
    }}catch(e){{}}
  }})();""")
    if not chart_entries:
        return ""
    # 同时生成"数据生成中"占位图（chart_entries 为空时）
    return ("<script>"
            "(function(){if(typeof Chart==='undefined')return;"
            + "".join(chart_entries)
            + "})();</script>")


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


# ---------------- 大盘指数（东方财富卡片风）---------------
MARKET_INDEX_CACHE = conf.STATE_DIR / "market_index.json"
MARKET_INDICES = {
    "sh.000001": {"label": "上证指数", "code_short": "SH"},
    "sz.399001": {"label": "深证成指", "code_short": "SZ"},
    "sz.399006": {"label": "创业板指", "code_short": "CYB"},
}


def _load_market_index(force_refresh=False):
    """加载上证/深证/创业板指日线。优先读缓存 JSON，不存在或 force_refresh 时通过 baostock 拉取。
    返回 {code: [(date, close), ...], ...} 或空 dict。"""
    if not force_refresh and MARKET_INDEX_CACHE.exists():
        try:
            raw = json.loads(MARKET_INDEX_CACHE.read_text(encoding="utf-8"))
            out = {}
            for k, v in raw.items():
                if k in MARKET_INDICES:
                    out[k] = [(d, float(c)) for d, c in v]
            if len(out) == len(MARKET_INDICES):
                return out
        except Exception:
            pass
    # 尝试 baostock
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            bs.logout()
            return {}
        out = {}
        for code, meta in MARKET_INDICES.items():
            rs = bs.query_history_k_data_plus(code, "date,close",
                                              start_date="2025-01-01",
                                              end_date=util.today_str(),
                                              frequency="d")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                out[code] = [(r[0], float(r[1])) for r in rows if r[1]]
        bs.logout()
        if out:
            cache = {}
            for k, v in out.items():
                cache[k] = [(d, round(c, 2)) for d, c in v]
            try:
                MARKET_INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
                MARKET_INDEX_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return out
    except Exception:
        return {}


def _market_index_cards(index_data):
    """生成东方财富风格的三大指数卡片：当前点位、涨跌额、涨跌幅、红绿颜色。
    涨跌基于最近一个交易日 vs 前一交易日。"""
    if not index_data:
        return '<div class="pos-empty">指数数据暂不可用（baostock 离线或网络不通），下次生成看板时将自动重试。</div>'

    cards_html = ""
    for code, meta in MARKET_INDICES.items():
        rows = index_data.get(code, [])
        if not rows or len(rows) < 2:
            continue
        last_date, last_close = rows[-1]
        prev_date, prev_close = rows[-2]
        chg = last_close - prev_close
        chg_pct = (chg / prev_close) if prev_close > 0 else 0
        color_class = "up" if chg >= 0 else "down"
        color = "var(--up)" if chg >= 0 else "var(--down)"
        sign = "+" if chg >= 0 else ""

        cards_html += (
            f'<div class="idx-card {color_class}">'
            f'<div class="idx-name">{meta["label"]}<span class="idx-code">{meta["code_short"]}</span></div>'
            f'<div class="idx-price" style="color:{color}">{last_close:,.2f}</div>'
            f'<div class="idx-chg" style="color:{color}">'
            f'<span class="idx-chg-val">{sign}{chg:,.2f}</span>'
            f'<span class="idx-chg-pct">{sign}{chg_pct:.2f}%</span>'
            f'</div>'
            f'</div>')

    return (
        f'<div class="idx-cards">'
        f'{cards_html}'
        f'<div class="idx-date">数据更新至 {rows[-1][0] if rows else "—"}</div>'
        f'</div>')


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

    # ===== 大盘指数（东方财富风卡片）=====
    market_data = _load_market_index()
    market_section = ""
    if market_data:
        market_section = (
            f"<div class='sec'>📈 大盘指数</div>"
            f"{_market_index_cards(market_data)}")
    else:
        market_section = (
            f"<div class='sec'>📈 大盘指数</div>"
            f"<div class='pos-empty'>指数数据暂不可用（baostock 离线或网络不通），"
            f"下次生成看板时将自动重试。</div>")

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
    exp_data = _load_factor_exposures()
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
        # 策略逻辑折叠 + 方法论链接
        logic_block = (
            f"<details class='strategy-logic'><summary>📐 策略逻辑说明（点击展开）</summary>"
            f"{_factor_block(meta)}"
            f"<a class='logic-link' href='methodology.html#{sid}'>完整方法论 →</a>"
            f"</details>")
        # 因子暴露区域(放在持仓表之后)
        exposure_html = _exposure_html(sid, exp_data)
        cards += (
            f"<div class='card'>"
            f"<div class='card-h'><b>{meta['name']}</b><span class='risk'>{meta['risk']}</span>"
            f"<span class='stat'>{st}</span></div>"
            f"{logic_block}"
            f"<details><summary>📈 实盘收益率曲线（07-06 起）当前 "
            f"<span style='color:{up_color};font-weight:700'>{cur_txt}</span></summary>{chart}{bt_html}</details>"
            f"<div class='sub2'>最新持仓</div>{_positions_table(conn, a, sid, log_rows)}"
            f"<div class='sub2'>操作计划</div>{ops}"
            f"{exposure_html}"
            f"</div>")
    if not accts:
        cards = "<p class='empty'>暂无策略状态。请先运行 run_daily.py 或回测生成 state/。</p>"

    # 顶部导航
    nav = ('<nav><a href="index.html">📊 策略看板</a>'
           '<a href="methodology.html">📐 策略方法论</a>'
           '<a href="methodology.html#risk-model">📈 因子风险模型</a>'
           '</nav>')
    body = (
        f"{nav}<h1>📊 A股模拟跟单看板</h1>"
        f"<div class='sub'>生成 {today} · 数据最新 {last} · 实盘模拟期自 2026-07-06 起 · 模拟/历史不代表未来，非投资建议，人工跟单</div>"
        f"{banner}"
        f"{market_section}"
        f"<div class='sec'>{ops_title}</div>{ops_section}"
        f"<div class='sec'>实盘赛马总览（2026-07-06 起算）</div>{overview}"
        f"<div class='sec'>各策略详情</div>{cards}"
        f"<div class='sec'><a href='trades.html'>📜 查看全部历史交易记录 →</a></div>"
        f"{_FOOTER}")
    chart_js = _exposure_chart_js(exp_data)
    html_doc = f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>" \
               f"<meta name='viewport' content='width=device-width, initial-scale=1'>" \
               f"<title>A股模拟跟单看板</title>" \
               f"<script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js'>" \
               f"</script>{_STYLE}</head><body><div class='wrap'>{body}</div>{chart_js}{_LIVE_JS}</body></html>"

    out_path = out_path or (conf.ROOT / "docs" / "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    try:
        generate_trades(conn)
    except Exception:
        pass
    try:
        generate_methodology(out_path=conf.ROOT / "docs" / "methodology.html")
    except Exception:
        pass
    if conn:
        conn.close()
    return str(out_path)


# ---------------- 方法论页 ----------------
_METHODOLOGY_STYLE = """<style>
:root{--bg:#f6f7f9;--fg:#1f2937;--mut:#6b7280;--card:#fff;--line:#e5e7eb;--up:#d92b2b;--down:#0a9e6b}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--fg);font-size:15px;line-height:1.6}
.wrap{max-width:800px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0}h2{font-size:18px;margin:24px 0 10px;border-bottom:2px solid var(--line);padding-bottom:6px}
h3{font-size:15.5px;margin:16px 0 8px;color:#334155}
p,li{font-size:14px}ul{padding-left:20px}
a{color:#2563eb;text-decoration:none}
/* 导航 */
nav{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
nav a{display:inline-block;padding:6px 14px;background:#2563eb;color:#fff;border-radius:8px;
text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
nav a:hover{background:#1d4ed8}
/* 目录 */
.toc{background:var(--card);border-radius:10px;padding:14px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.toc a{display:block;padding:3px 0;font-size:13.5px}
/* 策略块 */
.strat-block{background:var(--card);border-radius:10px;padding:14px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.strat-block summary{cursor:pointer;font-weight:600;font-size:15px;padding:4px 0;color:#1f2937}
.strat-block details{margin:0}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;font-size:13px;margin:8px 0}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{background:#f0f2f5;color:var(--mut);font-weight:600;font-size:12px}
.note{color:var(--mut);font-size:12px;margin:4px 0}
.foot{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}
.diff{background:#fef9c3;padding:8px 12px;border-radius:6px;font-size:12.5px;margin:6px 0}
.risk-badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;
background:#fef3c7;color:#92400e;margin-left:6px}
</style>"""


def _methodology_toc():
    """目录锚点导航。"""
    items = ""
    for sid, meta in sorted(STRAT_META.items()):
        items += f'<a href="#{sid}">{meta["name"]}</a>\n'
    items += '<a href="#risk-model">因子与风险模型</a>\n'
    return f'<div class="toc"><b>📑 目录</b>\n{items}</div>'


def _methodology_strat_block(sid):
    """单个策略的方法论区块（含完整投资逻辑、因子表、适用环境、风险提示）。"""
    meta = _meta(sid)
    factors = meta.get("factors", [])
    factor_rows = ""
    if factors:
        factor_rows = "".join(
            f"<tr><td>{html.escape(str(n))}</td><td>{html.escape(str(wt))}</td></tr>"
            for n, wt in factors)
    # 适用环境与风险提示（按策略类型分）
    env_risk = {
        "s2_etf@v1": ("<b>适用环境</b>：趋势明确的市场（牛市/熊市均可），震荡市表现一般。"
                       "当市场连续下跌时国债ETF提供避险保护。<br>"
                       "<b>风险提示</b>：单品种集中持仓，轮动时点决定收益差距；"
                       "动量策略在趋势反转拐点可能滞后切换。"),
        "s1_dividend@v1": ("<b>适用环境</b>：震荡市或慢牛市中表现突出，高股息股在利率下行期有防御价值。"
                          "<br><b>风险提示</b>：高股息陷阱——部分股票因股价暴跌导致股息率虚高；"
                          "利率上行周期高股息股相对吸引力下降。"),
        "s1_dividend@v2": ("<b>适用环境</b>：与v1相同，额外过滤了盈利质量不足的高股息股，减少股息陷阱风险。"
                          "<br><b>风险提示</b>：ROE筛选可能剔除周期底部的高股息机会；"
                          "A股多数公司ROE波动大，连续3年门槛可能使候选池过小。"),
        "s3_ma_trend@v1": ("<b>适用环境</b>：趋势明确的中期行情（牛熊均可，急涨急跌最好）。"
                          "<br><b>风险提示</b>：震荡市频繁假突破·假跌破，磨损成本高；"
                          "均线信号滞后于价格，顶部区域可能在跌破均线前已回吐大量利润。"),
        "s4_smallcap@v1": ("<b>适用环境</b>：风险偏好较高的市场环境，小市值因子溢价周期。"
                          "当前池为沪深300(非真小盘)，因子暴露偏向'大盘内选中小'。<br>"
                          "<b>风险提示</b>：小市值天然波动大；流动性风险——极端行情可能无法按预期价格成交；"
                          "免费数据限制使池仅为沪深300，非真正的小盘精选。"),
        "s5_grid@v1": ("<b>适用环境</b>：震荡/慢牛市场，PE估值在合理区间（十年20-70%分位）时效果最佳。"
                      "<br><b>风险提示</b>：极端单边行情（如2007/2015大牛）过早卖出导致踏空；"
                      "PE分位依赖历史数据，估值中枢可能永久性变化（如市场制度改革）。"),
        "s6_sector@v1": ("<b>适用环境</b>：有明确产业主线的市场（政策驱动、景气周期），"
                        "行业轮动规律明显时表现好。<br>"
                        "<b>风险提示</b>：行业集中度高、单品种持仓；"
                        "政策变化或景气拐点可能引发剧烈回撤；弱市切国债提供部分保护但非保本。"),
    }
    er = env_risk.get(sid, "")
    v3_diff = ""
    if sid == "s1_dividend@v3":
        v3_diff = ('<div class="diff"><b>P0升级(2026-07-06)</b>：4因子→7因子(BETA反向/EARNINGS_YIELD/LEVERAGE反向)+macro regime自适应权重。'
                   '扩张期提QUALITY/EARNINGS_YIELD；收缩期自动提高LOW_VOL/BETA/LEVERAGE负向权重增强防御。'
                   'BETA负向=偏好低Beta防御股, LEVERAGE负向=偏好低杠杆公司。'
                   '与v2核心差异：排名法→去极值+标准化+正交化复合(ResVol⊥Beta⊥Size)；取消低波后30%硬截断。</div>')
    elif sid == "s4_smallcap@v2":
        v3_diff = ('<div class="diff"><b>P0升级(2026-07-06)</b>：4因子→7因子(+BETA/EARNINGS_YIELD/QUALITY)+macro regime自适+行业动量倾斜。'
                   '扩张期自动提高MOMENTUM/BETA弹性权重；收缩期提高VALUE/EARNINGS_YIELD/QUALITY防御权重。'
                   '所属行业近60日涨幅前30%获行业动量加分。'
                   '与v1核心差异：20日动量→RSTR 12-1月动量；PB排名→BTOP z分并加ETOP/ROE/QUALITY；新增残差波动帽。</div>')
    return (f'<div class="strat-block" id="{sid}">'
            f'<details><summary>{meta["name"]}<span class="risk-badge">{meta["risk"]}</span></summary>'
            f'<p class="tagline">{html.escape(meta["tagline"])}</p>'
            f'<h3>因子构成</h3>'
            f'<table><thead><tr><th>因子 / 规则</th><th>权重 / 说明</th></tr></thead>'
            f'<tbody>{factor_rows}</tbody></table>'
            f'<p class="note">调仓：{html.escape(meta["rebalance"])} · 适合资金：{html.escape(meta["fit"])}</p>'
            f'{v3_diff}'
            f'<p class="note">{er}</p>'
            f'</details></div>')


def _methodology_risk_model():
    """因子与风险模型章节（id=risk-model）。"""
    return '''<h2 id="risk-model">因子与风险模型</h2>

<h3>因子体系总览</h3>
<p>本项目参考 MSCI Barra 中国A股模型（CNE5/CNE6）与 Axioma Robust Risk Model，
按免费数据现实裁剪，实现 10 个风格因子。每个因子由 1-3 个描述符加权复合。
当前 S1 v3（红利7因子）和 S4 v2（多因子7因子）各使用其中 7 个因子。</p>
<table>
<thead><tr><th>因子</th><th>描述符</th><th>S1 v3</th><th>S4 v2</th><th>说明</th></tr></thead>
<tbody>
<tr><td>Size（市值）</td><td>ln(总市值)</td><td>✅ 正向</td><td>✅ 负向</td><td>S1偏大盘防御，S4偏小盘弹性</td></tr>
<tr><td>Beta（贝塔）</td><td>60日滚动超额收益对市场回归斜率</td><td>✅ 负向</td><td>✅ 正向</td><td>S1偏好低Beta防御，S4牛市好高弹性</td></tr>
<tr><td>Momentum（动量）</td><td>RSTR+6月+3月</td><td>—</td><td>✅ 正向</td><td>12-1月动量+多窗口复合</td></tr>
<tr><td>Value（价值）</td><td>1/PE+1/PB+DY</td><td>✅ 正向</td><td>✅ 正向</td><td>三描述符合成，红利策略核心因子</td></tr>
<tr><td>Volatility（波动）</td><td>DASTD+ATR</td><td>✅ 负向</td><td>✅ 过滤</td><td>S1低波加分，S4剔除高残差波动股</td></tr>
<tr><td>Quality（质量）</td><td>ROE-杠杆代理</td><td>✅ 正向</td><td>✅ 正向</td><td>高ROE+低杠杆=盈利可持续</td></tr>
<tr><td>Growth（成长）</td><td>净利润5年趋势</td><td>—</td><td>—</td><td>待计入策略（数据覆盖不足）</td></tr>
<tr><td>Liquidity（流动性）</td><td>STOM(月换手率对数)</td><td>—</td><td>✅ 正向</td><td>偏好高流动性股票</td></tr>
<tr><td>Leverage（杠杆）</td><td>1-1/PB近似</td><td>✅ 负向</td><td>—</td><td>红利策略偏好低负债公司</td></tr>
<tr><td>Earnings Yield（盈利收益）</td><td>1/PE(TTM)</td><td>✅ 正向</td><td>✅ 正向</td><td>与VALUE互补衡量估值</td></tr>
</tbody></table>

<h3>数据处理管线</h3>
<ol>
<li><b>去极值（MAD Winsorize）</b>：对每个因子截面，用中位数绝对偏差（MAD）设定上下界，
越界值截断到边界。公式：bound = median(x) +/- 5 x 1.4826 x MAD(x)。防止个别的极端数值扭曲整体评估。</li>
<li><b>标准化（Z-score）</b>：z = (x - mu_w) / sigma_eq，其中 mu_w 为市值加权均值，sigma_eq 为等权标准差。
处理后，因子分布以市值加权组合为中心(暴露约=0)，等权标准差约=1。</li>
<li><b>正交化（Gram-Schmidt）</b>：按固定顺序(BETA→SIZE→VALUE→...→EARNINGS_YIELD)依次对前一因子做WLS回归取残差。
消除因子间的共线性——例如残差波动与Beta天然相关，正交化后残差波动不再包含Beta已解释的部分。</li>
<li><b>缺失处理</b>：缺失的描述符在复合时按可得权重重归一，标准化后NaN填0（池中性）。</li>
</ol>

<h3>风险模型（Barra 横截面法）</h3>
<p>结构模型：<b>r = Xf + u</b>（个股收益 = 因子暴露 x 因子收益 + 特异收益）。</p>
<ol>
<li><b>暴露矩阵 X</b>：N只股票 x 6个风险因子(size, beta, momentum, resvol, liquidity, btop)的当天暴露值。</li>
<li><b>因子收益估计</b>：对每个交易日，用t-1日暴露对t日个股收益做WLS（权=sqrt(市值)）横截面回归，得因子收益 f_t。</li>
<li><b>因子协方差 F</b>：f_t 的EWMA协方差（半衰期90日），x252年化。</li>
<li><b>特异波动 σ_i</b>：残差 u_i 的EWMA标准差（半衰期42日），xsqrt(252)年化。</li>
<li><b>组合预测波动</b>：σ_p = sqrt( h\'X F X\'h + Σ h_i^2 σ_i^2 )，h=各持仓市值权重。</li>
<li><b>组合暴露</b>：X_p = Σ h_i · z_i。因标准化以市值加权均值为中心，X_p 本身即为主动暴露（相对于市值加权基准）。</li>
</ol>

<h3>对称性说明（暴露怎么看）</h3>
<p>暴露值为正 → 组合在该因子上比市值加权基准偏多（如正Beta = 比市场Beta更高）。<br>
暴露值为负 → 组合在该因子上比基准偏少。<br>
暴露值在[-0.5, 0.5] → 基本中性，无明显偏离。<br>
暴露值>|1| → 显著偏离，需要注意该维度的集中风险。</p>

<h3>与 Barra CNE6 / Axioma 的差异声明</h3>
<ul>
<li><b>Beta</b>：使用60日窗口(vs CNE6的504日/252日)，因本项目数据覆盖较短(2018年起)，声明短窗差异。</li>
<li><b>动量(RSTR)</b>：不做CNE6的11日滞后平均处理，直接使用252日剔除最近21日的指数衰减累积超额收益。</li>
<li><b>残差波动</b>：无CMRA(月累计收益范围)描述符，仅用日收益标准差DASTD与ATR近似。</li>
<li><b>流动性</b>：仅有STOM(月换手率对数)，无STOQ(季度)/STOA(年度)三个档位。</li>
<li><b>质量</b>：无季频ATO(资产周转率)/GPM(毛利率)数据，仅用年报ROE加杠杆代理。</li>
<li><b>成长</b>：仅净利润增长率，无营收增长率数据。</li>
<li><b>杠杆</b>：无资产负债表数据，用1-1/PB近似资产负债率，属于粗粒度代理。</li>
</ul>

<h3>免费数据局限</h3>
<ul>
<li><b>无分析师预期数据</b>：无法构建Analyst Sentiment、预期EP、预期股息因子。</li>
<li><b>无季频资产负债表与现金流</b>：无法严格构建Leverage、Investment Quality、Earnings Quality因子。</li>
<li><b>无真实流通股本</b>：换手率为amount x 100 / market_cap反推近似，市值加权也只能用总市值而非流通市值。</li>
<li><b>市场代理为sh510300 ETF</b>：库内无sh000300指数日线，用沪深300ETF后复权收益替代。</li>
<li><b>无真实宏观数据</b>：宏观因子(利率变化、PMI意外)在本模型中占位为0；已通过 macro.py detect_regime() 用PE分位+MA方向做regime自适应补偿。</li>
</ul>

<h3>宏观 Regime 检测</h3>
<p><b>macro.py detect_regime()</b>：基于沪深300 PE十年分位 + MA20/MA60均线方向判断市场状态。</p>
<ul>
<li><b>扩张(expansion)</b>：PE分位≤50% 且 MA20>MA60（估值合理+趋势向上）→ S4提MOMENTUM/BETA弹性权重, S1提QUALITY/EARNINGS_YIELD</li>
<li><b>收缩(contraction)</b>：PE分位>70% 或 MA20<MA60且PE>50%（高估或下行）→ S1提LOW_VOL/BETA/LEVERAGE负向权重增强防御; S4降MOMENTUM/BETA提VALUE/QUALITY</li>
<li><b>中性(neutral)</b>：其余情况 → 使用默认因子权重</li>
</ul>

<h3>行业动量倾斜</h3>
<p><b>macro.py industry_momentum()</b>：计算申万31行业近60日等权涨幅排名。S4 v2 对属于涨幅前30%行业的个股给予评分加分(+0.15)，实现基本面层面的行业景气度倾斜——不依赖政策文本，利用市场数据体现行业轮动规律。</p>
'''


def generate_methodology(out_path=None):
    """生成独立方法论页 docs/methodology.html。"""
    nav = ('<nav><a href="index.html">📊 策略看板</a>'
           '<a href="methodology.html">📐 策略方法论</a>'
           '<a href="methodology.html#risk-model">📈 因子风险模型</a>'
           '</nav>')
    toc = _methodology_toc()
    strat_blocks = ""
    for sid in sorted(STRAT_META.keys()):
        strat_blocks += _methodology_strat_block(sid)
    # v3 的策略(如果 registry 有但 STRAT_META 还没有，加占位)
    risk = _methodology_risk_model()
    today = util.today_str()
    body = (f"{nav}<h1>📐 策略方法论</h1>"
            f'<p class="note">生成 {today} · 文档随策略版本同步更新 · 所有分析基于免费公开数据</p>'
            f"{toc}"
            f'<h2>各策略详解</h2>{strat_blocks}'
            f"{risk}"
            f'<div class="foot"><b>免责</b>：本页由 report_html.py 自动生成；'
            f'模拟/历史表现不代表未来，不构成投资建议，请仅用可承受损失的资金。因子模型参考 MSCI Barra CNE5/CNE6 公开文献与'
            f'Axioma V4 Handbook，按本项目免费数据现实裁剪。</div>')
    doc = (f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>策略方法论 - A股模拟跟单</title>{_METHODOLOGY_STYLE}</head>"
           f"<body><div class='wrap'>{body}</div></body></html>")
    out_path = out_path or (conf.ROOT / "docs" / "methodology.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
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
/* 大盘指数卡片（东方财富风） */
.idx-cards{display:flex;gap:10px;margin:8px 0 4px;flex-wrap:wrap}
.idx-card{flex:1;min-width:180px;background:linear-gradient(135deg,#f8fafc 0%,#fff 100%);
border-radius:12px;padding:14px 16px;border:1px solid var(--line);text-align:center}
.idx-card.up{border-left:3px solid var(--up)}.idx-card.down{border-left:3px solid var(--down)}
.idx-name{font-size:13px;color:var(--mut);font-weight:600;margin-bottom:2px}
.idx-code{font-size:10px;color:var(--mut);margin-left:4px;opacity:0.7}
.idx-price{font-size:24px;font-weight:700;margin:4px 0;letter-spacing:-0.5px}
.idx-chg{font-size:13px;display:flex;justify-content:center;gap:8px}
.idx-chg-val{font-weight:600}.idx-chg-pct{font-weight:600}
.idx-date{width:100%;text-align:center;color:var(--mut);font-size:11px;margin-top:2px}
.bt{background:#f0f7ff;color:#1e40af;font-size:12px;padding:6px 10px;border-radius:8px;margin:6px 0}
.foot{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}
.foot b{color:#374151}.empty{color:var(--mut);text-align:center;padding:40px}
/* 顶部导航 */
nav{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
nav a{display:inline-block;padding:6px 14px;background:#2563eb;color:#fff;border-radius:8px;
text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
nav a:hover{background:#1d4ed8}
/* 折叠区域增强 */
details.strategy-logic,details.factor-exposure,details.usage-instructions{margin:8px 0}
details.strategy-logic summary,details.factor-exposure summary,details.usage-instructions summary{cursor:pointer;
padding:8px 12px;background:rgba(0,0,0,0.03);border-radius:4px;user-select:none}
details.strategy-logic summary:hover,details.factor-exposure summary:hover,
details.usage-instructions summary:hover{background:rgba(0,0,0,0.06)}
details[open].strategy-logic summary,details[open].factor-exposure summary,
details[open].usage-instructions summary{margin-bottom:8px;border-bottom:1px solid var(--line)}
/* 策略逻辑链接 */
.logic-link{display:block;margin-top:8px;font-size:12.5px;color:#2563eb;text-decoration:none}
/* 因子暴露 CSS 条形图 */
.exp-vol{font-size:12.5px;color:var(--mut);margin:6px 0}
.exp-chart-container{width:100%;max-width:400px;height:200px;margin:8px auto;position:relative}
.exp-no-data{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:var(--mut);font-size:13px}
.exp-bar-row{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:12px}
.exp-label{width:100px;text-align:right;color:var(--mut);flex-shrink:0;font-size:11.5px}
.exp-bar-wrap{flex:1;height:14px;background:#f0f2f5;border-radius:7px;position:relative;overflow:hidden}
.exp-zero-line{position:absolute;left:50%;top:0;width:1px;height:100%;background:rgba(0,0,0,0.15)}
.exp-fill{position:absolute;top:0;height:100%;border-radius:7px}
.exp-fill.exp-pos{background:#3b82f6}
.exp-fill.exp-neg{background:#ef4444}
.exp-val{width:42px;text-align:left;font-size:11.5px;font-weight:600;flex-shrink:0}
.exp-note{color:var(--mut);font-size:11.5px;margin-top:6px}
.exp-note a{color:#2563eb;text-decoration:none}
.exposure-table{font-size:12px;margin-top:8px}
.exposure-table th,.exposure-table td{padding:4px 8px}
</style>"""

_FOOTER = """<div class="foot">
<details class="usage-instructions"><summary>📖 使用说明（点击展开）</summary>
<b>怎么用</b>：每天 18:00 前后微信收到推送，次日开盘按『操作计划』的价格带手动跟单（每条已标注所属策略）；没收到心跳=系统故障，当天别跟单。<br>
<b>观察期纪律</b>：第0-2周只看不投；满季度后若赛马正常，5万低风险参考配比 = 大盘网格30%+ETF轮动25%+红利低波25%+行业轮动10%+现金10%（S3/S4仅观察）。任何策略熔断→该部分转现金等复核。<br>
<b>数据来源</b>：sina/baostock/东财 免费源，每交易日17:40自动更新；页面顶部横幅提示数据新鲜度。<br>
<b>免责</b>：本页由 report_html.py 自动生成，零外部依赖可离线打开；模拟/历史表现不代表未来，不构成投资建议，请仅用可承受损失的资金。
</details>
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
