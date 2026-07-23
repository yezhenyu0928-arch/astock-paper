# -*- coding: utf-8 -*-
"""全策略共享: 市场 / 行业 / 个股 公告与动态「每日跟踪」守卫(全量接入, SPEC_NEWS)。

设计铁律: 策略内的守卫**只读取已落库信号**, 绝不在 generate_orders 中主动 fetch 外部新闻。
外部新闻由 run_daily / run_intraday 在数据阶段扫描并落库(db.news_raw / news_signal)。

为什么这样设计(关键):
- 回测库(市场 CI 构建的 market.sqlite)中 news_* 表为空, 若在策略里 fetch 会把"今天的真实新闻"
  泄漏进历史回测日 = 未来函数, 且 CI 沙箱无外网会超时。
- 因此用 `news_raw 当日是否有行` 作为「实盘开关」: 只有 run_daily 已落库当日快讯(说明是实盘新闻模式),
  策略才对候选股做 L0 关键词扫描; 回测库恒空 → 自动跳过, 不影响回测复现。

四道防线(均由策略在 generate_orders 内调用, 叠加于各自选股逻辑之上):
  A. guard_candidates(date, codes, conn, cfg)   候选股公告排雷(立案/预亏/减持/商誉减值/债务违约/退市风险/问询函)
  B. guard_holdings(date, holdings, conn, cfg)   持仓黑天鹅即时退出(策略侧溯源, 与 risk.blackswan_sells 双保险)
  C. guard_industry(date, codes, conn, cfg, ind_of)  行业负面动态回避(行业内个股负面占比过高→整行业回避, 无LLM也有效)
  D. structural_ban(date, code, ctx)            结构性排雷(业绩由盈转亏/同比暴跌>50%/当日跌停, 用 DB 字段, **回测可生效**)
  E. market_exposure(date, ctx, cfg)            市场分调仓(复用 news_engine.market_exposure_mult 缩放新开仓权重)

文本新闻维度仅在实盘每日生效; 结构性排雷(D)可在回测中生效(见各策略 CI 报告)。
"""
import logging
import time

log = logging.getLogger("news_guard")

# 单策略候选股新闻扫描超时预算(秒)。实盘 run_daily 在 generate_orders 内执行, 超时即停剩余候选(降级)。
CANDIDATE_SCAN_BUDGET = 75
NEWS_LOOKBACK_DAYS = 3


def _flash_present(date, conn):
    """实盘开关: news_raw 当日是否有行。回测库恒空 → False(跳过 fetch, 防未来函数泄漏)。"""
    try:
        n = conn.execute(
            "SELECT count(*) FROM news_raw WHERE substr(ts,1,10)=? OR ts LIKE ?",
            (date, date + "%")).fetchone()[0]
        return n > 0
    except Exception:
        return False


def _l0_scan(code, date, conn):
    """对单只候选股做 L0 关键词扫描, 返回 (score, evidence): -2 黑天鹅 / -1 警示 / 0 无。
    落库 news_signal(stock:{code}) 供 get_stock_sentiment_score 复用(修复 s1/s4 该分恒为0的缺口)。
    幂等: 已落库则直接读, 避免多策略重叠候选重复 fetch 外部接口。
    """
    import news_engine as ne
    scope = f"stock:{code}"
    r = conn.execute("SELECT score FROM news_signal WHERE signal_date=? AND scope=?",
                     (date, scope)).fetchone()
    if r is not None:
        sc = int(r[0])
        return (sc, ["(已扫描)"]) if sc < 0 else (0, [])
    try:
        import news_adapter as na
        df = na.fetch_stock_news(code, days=NEWS_LOOKBACK_DAYS)
        if df is None or df.empty:
            return 0, []
        na.store_news(df, conn=conn)
        text = " ".join((df["title"].astype(str) + df["content"].astype(str)).tolist())
        bs = ne._match(text, ne.STOCK_BLACKSWAN)
        wn = ne._match(text, ne.STOCK_WARNING)
        if bs:
            na.store_signal(date, scope, -2, "L0", bs, conn=conn)
            return -2, bs
        if wn:
            na.store_signal(date, scope, -1, "L0", wn, conn=conn)
            return -1, wn
        return 0, []
    except Exception as e:
        log.debug("候选股 %s L0扫描失败: %s", code, e)
        return 0, []


def guard_candidates(date, codes, conn, cfg):
    """候选股公告排雷。

    Returns: (banned: set[code], reasons: dict[code->str])
    回测(news_raw 空) / news_layer 未启用 / 超时 → 返回空(不剔除任何候选, 不影响回测复现)。
    """
    nl = (cfg.get("news_layer") or {}) if cfg else {}
    if not nl.get("enabled"):
        return set(), {}
    if not _flash_present(date, conn):
        return set(), {}   # 回测 / 无新闻模式: 不扫描, 不影响回测复现
    date = str(date)[:10]
    banned, reasons = set(), {}
    start = time.time()
    done = 0
    for code in codes:
        done += 1
        if time.time() - start > CANDIDATE_SCAN_BUDGET:
            log.warning("候选股新闻扫描超时(>%ds), 剩余%d只降级跳过",
                        CANDIDATE_SCAN_BUDGET, len(codes) - done + 1)
            break
        try:
            score, ev = _l0_scan(code, date, conn)
        except Exception as e:
            log.debug("候选股 %s 扫描异常: %s", code, e)
            continue
        if score == -2:
            banned.add(code)
            reasons[code] = f"公告黑天鹅({'/'.join(ev[:2])})"
        elif score == -1 and nl.get("warn_action", "ban") == "ban":
            banned.add(code)
            reasons[code] = f"公告警示({'/'.join(ev[:2])})"
        # warn_action=penalize(默认): -1 警示不硬剔,信号已落库,由 mf_core._news_score 自然降权后排;
        # 既避免问询函等常态公告误剔好票,又保留"排雷后排"的效应。
    if banned:
        log.info("候选股公告排雷剔除 %d 只: %s", len(banned),
                 {c: reasons[c] for c in list(banned)[:5]})
    return banned, reasons


def guard_holdings(date, holdings, conn, cfg):
    """持仓黑天鹅即时退出(策略侧溯源)。读 run_daily/run_intraday 已落库的 stock:{code} 信号。
    Returns: {code: reason} (仅含 score<=-2)。与 risk.blackswan_sells 双保险。
    """
    nl = (cfg.get("news_layer") or {}) if cfg else {}
    if not nl.get("enabled"):
        return {}
    date = str(date)[:10]
    out = {}
    try:
        rows = conn.execute(
            "SELECT scope FROM news_signal WHERE signal_date=? AND scope LIKE 'stock:%' AND score<=-2",
            (date,)).fetchall()
        flagged = {s.split(":", 1)[1] for (s,) in rows if s.startswith("stock:")}
        for code in holdings:
            if code in flagged:
                out[code] = "新闻黑天鹅(个股风险信号, 策略侧同步清仓)"
    except Exception as e:
        log.debug("持仓黑天鹅读取失败(降级): %s", e)
    return out


def guard_industry(date, codes, conn, cfg, industry_of):
    """行业负面动态回避(无LLM也有效): 行业内已扫描个股负面占比过高 → 整行业回避。
    依赖 run_daily/策略内已落库的 stock:{code} 信号聚合同行业负面密度。
    industry_of: dict[code->行业名]; 返回 banned: set[code] (应剔除的候选股)。
    """
    nl = (cfg.get("news_layer") or {}) if cfg else {}
    if not nl.get("enabled"):
        return set()
    date = str(date)[:10]
    try:
        from collections import defaultdict
        rows = conn.execute(
            "SELECT scope, score FROM news_signal WHERE signal_date=? AND scope LIKE 'stock:%'",
            (date,)).fetchall()
        code_score = {s.split(":", 1)[1]: sc for (s, sc) in rows if s.startswith("stock:")}
        ind_total, ind_neg = defaultdict(int), defaultdict(int)
        for code, ind in industry_of.items():
            sc = code_score.get(code)
            if sc is None:
                continue
            ind_total[ind] += 1
            if sc <= -1:
                ind_neg[ind] += 1
        banned = set()
        for code, ind in industry_of.items():
            t, ng = ind_total.get(ind, 0), ind_neg.get(ind, 0)
            if t >= 3 and ng >= max(2, int(0.5 * t)):
                banned.add(code)
        return banned
    except Exception as e:
        log.debug("行业负面回避失败(降级): %s", e)
        return set()


def structural_ban(date, code, ctx):
    """结构性排雷(**回测可生效**): 业绩由盈转亏 / 同比暴跌>50% / 当日跌停。
    用 ctx 接口 + DB 字段(stock_annual / daily_bar), 不依赖外部新闻。
    Returns: (banned: bool, reason: str)
    """
    try:
        b = ctx.bar(code, str(date)[:10])
        if b:
            # 当日跌停判定: limit_down 字段可能是跌停价(数值>1)或布尔标志(0/1)。
            # 价格模式下,最低价触及跌停价即视为跌停;布尔模式直接取真假。
            ld = b.get("limit_down")
            if ld is not None:
                if isinstance(ld, (int, float)) and ld > 1.0:
                    lo = b.get("low")
                    if lo is not None and lo <= ld * 1.002:   # 触及跌停价(含微小浮点容差)
                        return True, "当日跌停(触及跌停价, 流动性/弱势风险, 结构性排雷)"
                elif ld:                                       # 布尔/0-1 标志
                    return True, "当日跌停(流动性/弱势风险, 结构性排雷)"
        rows = ctx.conn.execute(
            "SELECT net_profit FROM stock_annual WHERE code=? AND pub_date<=? "
            "ORDER BY stat_year DESC LIMIT 2",
            (code, str(date)[:10])).fetchall()
        if len(rows) >= 2:
            np0, np1 = rows[0][0], rows[1][0]
            if np0 is not None and np1 is not None:
                if np1 > 0 and np0 <= 0:
                    return True, "业绩由盈转亏(盈利硬着陆, 结构性排雷)"
                if np1 > 0 and np0 < np1 * 0.5:
                    return True, f"业绩同比暴跌{(np0 / np1 - 1) * 100:.0f}%(结构性排雷)"
    except Exception as e:
        log.debug("结构性排雷 %s 失败: %s", code, e)
    return False, ""


def market_exposure(date, ctx, cfg):
    """市场分调仓系数(只降不升)。复用 news_engine.market_exposure_mult; 无信号=1.0。"""
    try:
        import news_engine as ne
        return ne.market_exposure_mult(date, ctx, cfg)
    except Exception:
        return 1.0
