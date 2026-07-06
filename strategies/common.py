# -*- coding: utf-8 -*-
"""策略共享助手(非冻结)。持仓数随资金自适应、权重计算等。SPEC_FILL F2.2。
**升级**:新增新闻/产业信号辅助函数,供全策略调用。
"""
import math
import logging

log = logging.getLogger("strategies.common")


def effective_hold_n(hold_n, capital, cfg, sid):
    """effective_hold_n = min(registry.hold_n, floor(capital×0.98/min_ticket));
    custom.hold_n_override[sid] 可手动锁定(仍受上限约束)。"""
    custom = cfg.get("custom", {}) or {}
    min_ticket = custom.get("min_ticket", 8000)
    cap_limit = int(math.floor(capital * 0.98 / min_ticket)) if min_ticket else hold_n
    eff = min(hold_n, max(1, cap_limit))
    override = (custom.get("hold_n_override") or {}).get(sid)
    if override:
        eff = min(int(override), max(1, cap_limit))
    return max(1, eff)


def target_weight(eff_hold_n, buffer=0.98):
    """等权目标权重(留 2% 现金缓冲)。"""
    return round(buffer / eff_hold_n, 6)


def returns_over(ctx, code, windows):
    """各窗口收益率 {w: r};数据不足的窗口返回 None。r_w = close[-1]/close[-(w+1)]-1(后复权)。"""
    maxw = max(windows)
    c = ctx.close(code, maxw + 1)
    out = {}
    for w in windows:
        if len(c) >= w + 1 and c[-(w + 1)]:
            out[w] = c[-1] / c[-(w + 1)] - 1
        else:
            out[w] = None
    return out


# ============ 新闻/产业信号辅助函数(新增) ============

def apply_news_boost(date, code, etf_code=None, weight=1.0, conn=None):
    """应用新闻面加分到权重。

    Args:
        date: 日期
        code: 股票代码
        etf_code: 行业ETF代码(如有)
        weight: 原始权重
        conn: 数据库连接

    Returns:
        (adjusted_weight, reason): 调整后的权重和原因
    """
    try:
        import news_engine as ne
        signal = ne.get_composite_signal(date, code, etf_code, conn=conn)
        composite = signal["composite"]

        if abs(composite) < 0.3:
            return weight, ""

        # 调整系数:利好加权(最多+30%),利空减权(最多-50%)
        if composite > 0:
            boost = min(0.3, composite * 0.15)
            adjusted = weight * (1 + boost)
            reason = f"新闻面利好({signal['direction']},综合{composite:+.1f})"
        else:
            cut = min(0.5, abs(composite) * 0.25)
            adjusted = weight * (1 - cut)
            reason = f"新闻面利空({signal['direction']},综合{composite:+.1f})"

        return round(adjusted, 6), reason

    except Exception as e:
        log.debug("新闻加分失败 %s: %s", code, e)
        return weight, ""


def apply_sector_boost(date, etf_code, weight=1.0, conn=None):
    """应用行业ETF的产业信号加分。

    Args:
        date: 日期
        etf_code: 行业ETF代码
        weight: 原始权重
        conn: 数据库连接

    Returns:
        (adjusted_weight, reason): 调整后的权重和原因
    """
    try:
        import news_engine as ne
        boost = ne.get_sector_boost(date, etf_code, conn=conn)

        if abs(boost) < 0.3:
            return weight, ""

        # 行业加分:利好最多+40%,利空最多-60%
        if boost > 0:
            adj = min(0.4, boost * 0.2)
            adjusted = weight * (1 + adj)
            reason = f"产业利好({etf_code},分{boost:+.1f})"
        else:
            adj = min(0.6, abs(boost) * 0.3)
            adjusted = weight * (1 - adj)
            reason = f"产业利空({etf_code},分{boost:+.1f})"

        return round(adjusted, 6), reason

    except Exception as e:
        log.debug("行业加分失败 %s: %s", etf_code, e)
        return weight, ""


def get_fundamental_score(ctx, code, date):
    """获取基本面综合评分(供策略排序用)。

    Returns:
        float: 0..1 的分数, 越高越好
    """
    try:
        f = ctx.fundamental(code)
        if not f:
            return 0.5

        score = 0.5  # 基础分

        # PE 估值(越低越好,但排除负值)
        pe = f.get("pe", 0)
        if pe and pe > 0:
            if pe < 15:
                score += 0.15
            elif pe < 25:
                score += 0.05
            elif pe > 50:
                score -= 0.1

        # PB 估值(越低越好)
        pb = f.get("pb", 0)
        if pb and pb > 0:
            if pb < 1.5:
                score += 0.1
            elif pb > 5:
                score -= 0.1

        # 股息率(越高越好)
        dy = f.get("dividend_yield", 0)
        if dy and dy > 0.04:
            score += 0.1
        elif dy and dy > 0.02:
            score += 0.05

        # ROE(越高越好)
        roe = f.get("roe", 0)
        if roe and roe > 0.15:
            score += 0.1
        elif roe and roe > 0.08:
            score += 0.05

        return max(0, min(1, score))

    except Exception as e:
        log.debug("基本面评分失败 %s: %s", code, e)
        return 0.5


def composite_rank_score(tech_rank, fundamental_score, news_boost=1.0, weights=None):
    """计算综合排名分数(技术面+基本面+新闻面)。

    Args:
        tech_rank: 技术面排名(0..1, 越小越好)
        fundamental_score: 基本面分数(0..1, 越大越好)
        news_boost: 新闻面调整系数(1.0=中性, >1=利好, <1=利空)
        weights: 权重 dict {"tech": 0.5, "fund": 0.3, "news": 0.2}

    Returns:
        float: 综合分(越小越好, 用于排序)
    """
    if weights is None:
        weights = {"tech": 0.5, "fund": 0.3, "news": 0.2}

    # 基本面转排名分(越小越好)
    fund_rank = 1 - fundamental_score

    # 新闻面转排名分(越小越好)
    news_rank = 1 - min(1, news_boost)

    # 加权平均
    score = (tech_rank * weights["tech"]
             + fund_rank * weights["fund"]
             + news_rank * weights["news"])

    return score
