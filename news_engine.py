# -*- coding: utf-8 -*-
"""消息面分析引擎(SPEC_NEWS N2/N3)。L0 规则档(默认零成本)+ L1 大模型档(可选,P14)。
**升级**:新增产业主题扫描(scan_industry_themes)+个股语义分析(scan_stock_sentiment)。
铁律(扩展):消息面不仅降险,也提供**产业逻辑驱动**的正面信号,供策略叠加使用。
接口:
  scan_market(date, conn) -> (score, evidence)      市场分 -2..+2,落 news_signal
  scan_holdings(date, holdings, conn) -> {code:(score,evidence)}  个股 0/-1/-2
  scan_industry_themes(date, conn) -> dict           产业主题+行业ETF信号
  scan_stock_sentiment(date, code, conn) -> dict     个股语义分析
  market_exposure_mult(date, ctx, cfg) -> float      供 risk.post_check 第6步
  get_sector_boost(date, etf_code, conn) -> float    行业ETF加分(供S6/S2等策略)
  get_stock_sentiment_score(date, code, conn) -> float  个股语义分(供全策略)
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
    l0_score = score
    # L1 大模型档:正式档与 L0 取更保守者;影子档(卡F)只记录不干预
    cfg = conf.load_config()
    nl = cfg.get("news_layer") or {}
    l1 = _l1_market(date, cfg, titles)
    if l1 is not None:
        l1s = l1.get("market_score", 0)
        if nl.get("llm_shadow") and not nl.get("llm"):
            _log_shadow(date, l0_score, l1s, l1.get("top_risks", []))   # 影子:signal 仍用 L0
        elif l1s < score:
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
    """L1 大模型档(P14)。正式档(llm)或影子档(llm_shadow,卡F)启用时调用;失败/未启用返回 None。"""
    nl = cfg.get("news_layer") or {}
    if not (nl.get("llm") or nl.get("llm_shadow")):
        return None
    try:
        import news_llm
        return news_llm.market_score(date, titles, cfg)
    except Exception as e:
        log.warning("L1 失败,回退 L0: %s", e)
        return None


def _log_shadow(date, l0, l1, top_risks):
    """卡F 影子模式:把 (日期, L0分, L1分, L1更保守?, 证据) 追加到 state/news_shadow.csv,供 eval_news.py 评估。"""
    import csv as _csv
    p = conf.STATE_DIR / "news_shadow.csv"
    exists = p.exists()
    try:
        with open(p, "a", encoding="utf-8", newline="") as f:
            w = _csv.writer(f)
            if not exists:
                w.writerow(["date", "l0_score", "l1_score", "l1_stricter", "top_risks"])
            w.writerow([date, l0, l1, int(l1 < l0), "；".join((top_risks or [])[:3])])
    except Exception as e:
        log.warning("影子日志写入失败:%s", e)


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


# ============ 产业主题扫描(新增) ============

def scan_industry_themes(date, conn=None):
    """扫描当日新闻中的产业主题和行业ETF信号。

    Returns:
        {
            "themes": [{"name", "strength", "duration", "etf_codes", "reason"}],
            "sector_score": {"sh512480": 2, ...},
            "summary": "..."
        }
    """
    cfg = conf.load_config()
    nl = cfg.get("news_layer") or {}
    if not (nl.get("llm") or nl.get("llm_shadow")):
        return {"themes": [], "sector_score": {}, "summary": "LLM未启用"}

    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)

    # 获取当日新闻标题
    rows = conn.execute("SELECT title FROM news_raw WHERE substr(ts,1,10)=? OR ts LIKE ?",
                        (date, date + "%")).fetchall()
    titles = [r[0] or "" for r in rows]

    if not titles:
        if own:
            conn.close()
        return {"themes": [], "sector_score": {}, "summary": "无新闻"}

    # 调用 LLM 分析产业主题
    try:
        import news_llm
        result = news_llm.industry_themes(date, titles, cfg)
    except Exception as e:
        log.warning("产业主题分析失败: %s", e)
        result = {"themes": [], "sector_score": {}, "summary": f"分析失败: {e}"}

    # 落库存储行业信号
    for etf_code, score in result.get("sector_score", {}).items():
        na.store_signal(date, f"sector:{etf_code}", score, "L1",
                        [t.get("name", "") for t in result.get("themes", [])],
                        conn=conn)

    if own:
        conn.close()
    return result


def get_sector_boost(date, etf_code, conn=None):
    """获取某行业ETF的新闻面加分。供策略(S6/S2等)调用。

    Returns:
        float: -2..+2 的分数, 0=无信号
    """
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)

    r = conn.execute("SELECT score FROM news_signal WHERE signal_date=? AND scope=?",
                     (date, f"sector:{etf_code}")).fetchone()
    score = float(r[0]) if r else 0.0

    if own:
        conn.close()
    return score


def get_all_sector_boosts(date, conn=None):
    """获取所有行业ETF的新闻面加分。

    Returns:
        dict: {etf_code: score}
    """
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)

    rows = conn.execute("SELECT scope, score FROM news_signal WHERE signal_date=? AND scope LIKE 'sector:%'",
                        (date,)).fetchall()
    boosts = {}
    for scope, score in rows:
        etf_code = scope.replace("sector:", "")
        boosts[etf_code] = float(score)

    if own:
        conn.close()
    return boosts


# ============ 个股语义分析(新增) ============

def scan_stock_sentiment(date, code, conn=None):
    """对单只股票进行语义分析。

    Returns:
        {"sentiment": "positive/neutral/negative", "score": -2..2,
         "key_events": [...], "risk_level": "low/medium/high", "reason": "..."}
    """
    cfg = conf.load_config()
    nl = cfg.get("news_layer") or {}
    if not (nl.get("llm") or nl.get("llm_shadow")):
        return {"sentiment": "neutral", "score": 0, "key_events": [], "risk_level": "low"}

    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)

    # 获取该股票的近期新闻
    df = na.fetch_stock_news(code, days=3)
    if df is None or df.empty:
        if own:
            conn.close()
        return {"sentiment": "neutral", "score": 0, "key_events": [], "risk_level": "low"}

    # 合并新闻文本
    news_text = "\n".join([f"- {t}" for t in (df["title"].astype(str)).tolist()])

    # 调用 LLM 分析
    try:
        import news_llm
        result = news_llm.stock_sentiment(date, code, news_text, cfg)
    except Exception as e:
        log.warning("个股语义分析失败 %s: %s", code, e)
        result = {"sentiment": "neutral", "score": 0, "key_events": [], "risk_level": "low"}

    # 落库存储个股信号
    na.store_signal(date, f"stock:{code}", result.get("score", 0), "L1",
                    result.get("key_events", []), conn=conn)

    if own:
        conn.close()
    return result


def get_stock_sentiment_score(date, code, conn=None):
    """获取个股的新闻语义分。供策略调用。

    Returns:
        float: -2..+2 的分数, 0=无信号
    """
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)

    r = conn.execute("SELECT score FROM news_signal WHERE signal_date=? AND scope=?",
                     (date, f"stock:{code}")).fetchone()
    score = float(r[0]) if r else 0.0

    if own:
        conn.close()
    return score


# ============ 综合信号接口(供策略调用) ============

def get_composite_signal(date, code, etf_code=None, conn=None):
    """获取综合新闻信号(市场面+个股面+行业面)。供策略使用。

    Args:
        date: 日期
        code: 股票代码
        etf_code: 行业ETF代码(如有)
        conn: 数据库连接

    Returns:
        {
            "market_score": float,     # 市场面分
            "stock_score": float,      # 个股语义分
            "sector_score": float,     # 行业分
            "composite": float,        # 综合分(加权平均)
            "direction": "positive/neutral/negative"
        }
    """
    own = conn is None
    if own:
        conn = get_conn()
    date = util.to_date_str(date)

    # 市场面
    r = conn.execute("SELECT score FROM news_signal WHERE signal_date=? AND scope='market'",
                     (date,)).fetchone()
    market_score = float(r[0]) if r else 0.0

    # 个股面
    r = conn.execute("SELECT score FROM news_signal WHERE signal_date=? AND scope=?",
                     (date, f"stock:{code}")).fetchone()
    stock_score = float(r[0]) if r else 0.0

    # 行业面
    sector_score = 0.0
    if etf_code:
        r = conn.execute("SELECT score FROM news_signal WHERE signal_date=? AND scope=?",
                         (date, f"sector:{etf_code}")).fetchone()
        sector_score = float(r[0]) if r else 0.0

    # 综合分:市场30% + 个股40% + 行业30%
    composite = market_score * 0.3 + stock_score * 0.4 + sector_score * 0.3

    if composite > 0.5:
        direction = "positive"
    elif composite < -0.5:
        direction = "negative"
    else:
        direction = "neutral"

    if own:
        conn.close()

    return {
        "market_score": market_score,
        "stock_score": stock_score,
        "sector_score": sector_score,
        "composite": round(composite, 2),
        "direction": direction,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    na.ensure()
    s, ev = scan_market(util.today_str())
    print("市场分:", s, "证据数:", len(ev))
