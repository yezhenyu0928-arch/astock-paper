# -*- coding: utf-8 -*-
"""消息面分析引擎(SPEC_NEWS N2/N3)。L0 规则档(默认零成本)+ L1 大模型档(可选,P14)。
铁律:消息永不直接产生买入信号;只做三件事——降敞口/冻结开仓/持仓黑天鹅强卖。利好不追。
接口:
  scan_market(date, conn) -> (score, evidence)      市场分 -2..+2,落 news_signal
  scan_holdings(date, holdings, conn) -> {code:(score,evidence)}  个股 0/-1/-2
  market_exposure_mult(date, ctx, cfg) -> float     供 risk.post_check 第6步
  blackswan_sells(date, accounts, cfg, conn) -> [Order]  黑天鹅强制清仓单
"""
import json
import logging

import conf
import util
import news_adapter as na
from db import get_conn
from models import Order

log = logging.getLogger("news_engine")

# 市场级词表(权重)
MARKET_NEG = {"印花税上调": -2, "注册制暂停": -2, "地缘冲突升级": -2, "制裁": -2, "开战": -2,
              "熔断": -2, "千股跌停": -2, "流动性收紧": -1, "超预期加息": -1, "加息": -1,
              "汇率破位": -1, "大幅贬值": -1, "违约潮": -1}
MARKET_POS = {"降准": 1, "降息": 1, "超预期宽松": 2, "平准基金": 2, "汇金增持": 2,
              "重大利好": 1, "政策落地": 1}
# 个股级词表
STOCK_BLACKSWAN = ["立案调查", "被立案", "留置", "失联", "财务造假", "无法表示意见",
                   "债务违约", "资金占用", "退市风险警示", "实控人被"]
STOCK_WARNING = ["减持计划", "质押平仓", "业绩预亏", "商誉减值", "业绩变脸", "问询函"]


def _match(text, words):
    hits = []
    for w in (words if isinstance(words, (list, tuple)) else words.keys()):
        if w in text:
            hits.append(w)
    return hits


def scan_market(date, conn=None):
    """扫当日快讯标题,L0 打分。返回 (score, evidence)。"""
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)
    rows = conn.execute("SELECT title FROM news_raw WHERE substr(ts,1,10)=? OR ts LIKE ?",
                        (date, date + "%")).fetchall()
    titles = [r[0] or "" for r in rows]
    score, ev = 0, []
    for t in titles:
        for w, wt in {**MARKET_NEG, **MARKET_POS}.items():
            if w in t:
                score += wt
                ev.append(f"{w}({wt:+d}):{t[:30]}")
    score = max(-2, min(2, score))
    level = "L0"
    # L1 大模型档:与 L0 取更保守者(更低分)
    cfg = conf.load_config()
    l1 = _l1_market(date, cfg, titles)
    if l1 is not None:
        l1s = l1.get("market_score", 0)
        if l1s < score:
            ev.append(f"[L1更保守 {l1s}] " + "；".join(l1.get("top_risks", [])[:2]))
            score = l1s
            level = "L1"
    na.store_signal(date, "market", score, level, ev[:20], conn=conn)
    if own:
        conn.close()
    return score, ev


def scan_holdings(date, holdings, conn=None):
    """对持仓个股扫新闻,识别黑天鹅(-2)/警示(-1)。返回 {code:(score,evidence)}。"""
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)
    out = {}
    for code in holdings:
        if util.bare(code)[0] in ("5", "1"):     # ETF 跳过
            continue
        df = na.fetch_stock_news(code, days=3)
        if df is None or df.empty:
            continue
        na.store_news(df, conn=conn)
        text = " ".join((df["title"].astype(str) + df["content"].astype(str)).tolist())
        bs = _match(text, STOCK_BLACKSWAN)
        wn = _match(text, STOCK_WARNING)
        if bs:
            out[code] = (-2, bs)
            na.store_signal(date, code, -2, "L0", bs, conn=conn)
        elif wn:
            out[code] = (-1, wn)
            na.store_signal(date, code, -1, "L0", wn, conn=conn)
    if own:
        conn.close()
    return out


def _l1_market(date, cfg, titles):
    """L1 大模型档(P14)。失败/未启用返回 None(回退 L0)。"""
    if not (cfg.get("news_layer") or {}).get("llm"):
        return None
    try:
        import news_llm
        return news_llm.market_score(date, titles, cfg)
    except Exception as e:
        log.warning("L1 失败,回退 L0: %s", e)
        return None


def market_exposure_mult(date, ctx, cfg):
    """由市场分映射敞口系数(只降不升)。读 news_signal;无信号=1.0。"""
    if not (cfg.get("news_layer") or {}).get("enabled"):
        return 1.0
    date = util.to_date_str(date)
    r = ctx.conn.execute("SELECT score FROM news_signal WHERE signal_date=? AND scope='market'",
                         (date,)).fetchone()
    if r is None:
        return 1.0
    emap = (cfg.get("news_layer") or {}).get("exposure_map", {-2: 0.0, -1: 0.5, 0: 1.0, 1: 1.0, 2: 1.0})
    score = int(round(r[0]))
    # yaml 的 key 可能是 int
    return float(emap.get(score, emap.get(str(score), 1.0)))


def blackswan_sells(date, accounts, cfg, conn=None):
    """对持仓中命中黑天鹅(-2)的个股生成强制清仓单 + 返回警示(-1)列表。"""
    if not (cfg.get("news_layer") or {}).get("enabled"):
        return [], []
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)
    holdings = set()
    for acct in accounts.values():
        holdings |= set(acct.positions.keys())
    flags = scan_holdings(date, holdings, conn=conn)
    sells, warns = [], []
    for sid, acct in accounts.items():
        for code in list(acct.positions.keys()):
            if code in flags:
                score, ev = flags[code]
                if score == -2:
                    sells.append(Order(sid, code, "sell", 0.0,
                                       f"黑天鹅强卖:{'/'.join(ev[:3])}", date))
                elif score == -1:
                    warns.append((sid, code, ev))
    if own:
        conn.close()
    return sells, warns


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    na.ensure()
    s, ev = scan_market(util.today_str())
    print("市场分:", s, "证据数:", len(ev))
