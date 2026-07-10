# -*- coding: utf-8 -*-
"""看板(SPEC 模块7/M6 + P10 手机适配)。Streamlit 六页:
总览 / 策略详情 / 今日操作 / 操作流水(可回填实盘价) / 消息面 / 系统状态。
运行:streamlit run dashboard.py  (手机同一WiFi访问 http://<电脑IP>:8501)"""
import json
import glob
import os

import pandas as pd
import streamlit as st

import conf
import util
import backtest as bt

st.set_page_config(page_title="A股模拟跟单", layout="centered", initial_sidebar_state="collapsed")

def _load_strat_cn():
    """策略中文名字典(卡L.4 双轨同步):sid 全量动态读自 registry.yaml(只增不改的注册档案,
    新策略注册后自动出现,无需再改本文件);中文名优先取 report_html.STRAT_META(import 安全无
    副作用,已核实),取不到则回退 sid 原文。"""
    try:
        reg = conf.load_registry()
    except Exception:
        reg = {}
    try:
        import report_html
        meta = report_html.STRAT_META
    except Exception:
        meta = {}
    return {sid: (meta.get(sid, {}).get("name") or sid) for sid in reg}


STRAT_CN = _load_strat_cn()


# ---------------- 数据加载 ----------------
@st.cache_data(ttl=60)
def load_accounts():
    out = {}
    for f in glob.glob(str(conf.STATE_DIR / "*.json")):
        if os.path.basename(f) == "trade_log.csv":
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
            if "strategy_id" in d:
                out[d["strategy_id"]] = d
        except Exception:
            pass
    return out


@st.cache_data(ttl=60)
def load_trade_log():
    p = conf.STATE_DIR / "trade_log.csv"
    if p.exists():
        return pd.read_csv(p, dtype=str)
    return pd.DataFrame()


def metrics_of(acc):
    hist = acc.get("nav_history", [])
    if len(hist) < 2:
        return None
    navs = [h[1] for h in hist]
    return bt.compute_metrics(navs)


def cn(sid):
    return STRAT_CN.get(sid, sid)


def fmt_pct(x):
    return f"{x:+.1%}" if x is not None else "—"


# ---------------- 页面 ----------------
def page_overview():
    st.title("📊 总览")
    accts = load_accounts()
    if not accts:
        st.info("暂无策略状态。请先运行 run_daily.py 或回测。")
        return
    rows, curves = [], {}
    for sid, a in sorted(accts.items()):
        m = metrics_of(a)
        rows.append({"策略": cn(sid), "净值": round(a.get("nav", 1), 3),
                     "累计": fmt_pct(m["total"]) if m else "—",
                     "年化": fmt_pct(m["annual"]) if m else "—",
                     "回撤": fmt_pct(-m["max_dd"]) if m else "—",
                     "Calmar": round(m["calmar"], 2) if m else "—",
                     "状态": "🔴熔断" if a.get("frozen") else "🟢正常"})
        hist = a.get("nav_history", [])
        if hist:
            curves[cn(sid)] = pd.Series({h[0]: h[1] for h in hist})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if curves:
        st.subheader("净值曲线")
        df = pd.DataFrame(curves)
        st.line_chart(df, height=300)
    st.caption("模拟/历史表现不代表未来。每次操作以推送与『今日操作』页为准,人工跟单。")


def page_detail():
    st.title("🔍 策略详情")
    accts = load_accounts()
    if not accts:
        st.info("暂无数据")
        return
    sid = st.selectbox("选择策略", list(accts.keys()), format_func=cn)
    a = accts[sid]
    m = metrics_of(a)
    c1, c2, c3 = st.columns(3)
    c1.metric("净值", round(a.get("nav", 1), 3))
    c2.metric("累计收益", fmt_pct(m["total"]) if m else "—")
    c3.metric("最大回撤", fmt_pct(-m["max_dd"]) if m else "—")
    if m:
        c1, c2, c3 = st.columns(3)
        c1.metric("年化", fmt_pct(m["annual"]))
        c2.metric("Calmar", round(m["calmar"], 2))
        c3.metric("胜率", fmt_pct(m["win"]))
    hist = a.get("nav_history", [])
    if hist:
        st.line_chart(pd.Series({h[0]: h[1] for h in hist}, name="净值"), height=260)
    st.subheader("当前持仓")
    pos = a.get("positions", {})
    if pos:
        st.dataframe(pd.DataFrame([{"代码": util.bare(c), "股数": p["shares"],
                                    "成本": p["avg_cost"], "买入日": p.get("buy_date", "")}
                                   for c, p in pos.items()]), hide_index=True, use_container_width=True)
    else:
        st.write("空仓")
    # 五关报告
    rp = conf.REPORTS_DIR / f"{sid.replace('@','_at_')}.md"
    if rp.exists():
        with st.expander("📄 五关验证报告"):
            st.markdown(rp.read_text(encoding="utf-8"))
    vp = conf.REPORTS_DIR / f"{sid.replace('@','_at_')}_validate.md"
    if vp.exists():
        with st.expander("🧪 稳健性验证(蒙特卡洛)"):
            st.markdown(vp.read_text(encoding="utf-8"))


def page_today():
    st.title("📌 今日操作(计划)")
    accts = load_accounts()
    any_op = False
    for sid, a in sorted(accts.items()):
        pend = a.get("pending", [])
        if not pend:
            continue
        any_op = True
        st.subheader(f"【{cn(sid)}】")
        for o in pend:
            act = "🔴卖出" if o["side"] == "sell" else "🟢买入"
            st.write(f"{act} **{util.bare(o['code'])}** · 信号日{o['signal_date']} · {o.get('reason','')}")
    if not any_op:
        st.success("今日各策略无待执行操作(空仓或未到调仓日)")
    st.caption("买卖以次日开盘价附近跟单;成交后到『操作流水』回填实盘价。")


def page_trades():
    st.title("🧾 操作流水(回填实盘价)")
    df = load_trade_log()
    if df.empty:
        st.info("暂无成交记录")
        return
    df = df[df["status"].isin(["filled", "cut_liquidity"])].copy()
    df["策略"] = df["strategy_id"].map(cn)
    show = df[["trade_date", "策略", "side", "code", "shares", "sim_price", "real_price", "reason"]].copy()
    show.columns = ["日期", "策略", "方向", "代码", "股数", "模拟价", "实盘价", "理由"]
    st.caption("在『实盘价』列填入你的真实成交价,点保存。用于对比模拟与实盘偏差。")
    edited = st.data_editor(show, use_container_width=True, hide_index=True,
                            disabled=["日期", "策略", "方向", "代码", "股数", "模拟价", "理由"])
    if st.button("💾 保存实盘价"):
        full = load_trade_log()
        fmask = full["status"].isin(["filled", "cut_liquidity"])
        full.loc[fmask, "real_price"] = edited["实盘价"].values
        full.to_csv(conf.STATE_DIR / "trade_log.csv", index=False, encoding="utf-8")
        st.success("已保存")
        st.cache_data.clear()


def page_news():
    st.title("📰 消息面")
    from db import get_conn
    try:
        conn = get_conn()
        sig = pd.read_sql_query(
            "SELECT signal_date,scope,score,level,evidence FROM news_signal ORDER BY signal_date DESC LIMIT 100", conn)
        conn.close()
    except Exception:
        sig = pd.DataFrame()
    if sig.empty:
        st.info("暂无消息面信号(消息层未启用或无数据)")
        return
    mkt = sig[sig["scope"] == "market"]
    if not mkt.empty:
        st.subheader("市场风险分(负=降敞口)")
        s = pd.Series({r["signal_date"]: r["score"] for _, r in mkt.iterrows()}).sort_index()
        st.bar_chart(s, height=200)
    st.subheader("信号明细")
    st.dataframe(sig, use_container_width=True, hide_index=True)


def page_system():
    st.title("⚙️ 系统状态")
    from db import get_conn
    try:
        conn = get_conn()
        last = conn.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
        nbar = conn.execute("SELECT count(*) FROM daily_bar").fetchone()[0]
        ncode = conn.execute("SELECT count(DISTINCT code) FROM daily_bar").fetchone()[0]
        nfund = conn.execute("SELECT count(*) FROM fundamental").fetchone()[0] if _has(conn, "fundamental") else 0
        conn.close()
    except Exception as e:
        st.error(f"数据库读取失败: {e}")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("数据最新日", last or "—")
    c2.metric("K线条数", f"{nbar:,}")
    c3.metric("标的数", ncode)
    st.metric("基本面记录", f"{nfund:,}")
    today = util.today_str()
    st.write(f"今日 {today}:", "✅交易日" if _is_td(today) else "⭕非交易日")
    st.caption("数据陈旧(最新日落后当前多日)或推送失败时,请检查 GitHub Actions 运行日志。")


def _has(conn, t):
    return conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def _is_td(d):
    try:
        import trade_calendar as cal
        return cal.is_trade_day(d)
    except Exception:
        return False


PAGES = {"总览": page_overview, "策略详情": page_detail, "今日操作": page_today,
         "操作流水": page_trades, "消息面": page_news, "系统状态": page_system}


def main():
    st.sidebar.title("A股模拟跟单")
    choice = st.sidebar.radio("页面", list(PAGES.keys()))
    st.sidebar.caption("⚠ 仅供个人模拟研究,非投资建议")
    PAGES[choice]()


main()   # streamlit 直接自顶向下执行
