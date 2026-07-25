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


# ============ 主板宇宙硬约束(AI投资经理操作手册 v2.0 §股票池) ============

# 主板代码前缀:沪市 600/601/603/605,深市 000/001/002/003。
# 排除:科创板688 / 创业板300-301 / 北交所8xx-4xx / ST / 退市(由 is_tradable + 前缀共同保证)。
_MAIN_BOARD_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")

# 每次调用缓存 security.list_date,避免逐票查库
_LIST_DATE_CACHE = {"conn_id": None, "map": None}


def _bare6(code):
    """取 6 位数字代码(去 sh/sz 前缀)。"""
    s = str(code)
    return s[2:] if s[:2] in ("sh", "sz", "bj") else s


def is_main_board(code):
    """是否主板股票(仅按代码前缀,不含停牌/ST/上市年限判断)。"""
    return _bare6(code)[:3] in _MAIN_BOARD_PREFIX


def _list_date_map(conn):
    """一次性载入 security.list_date 映射(带缓存)。"""
    if _LIST_DATE_CACHE["conn_id"] == id(conn) and _LIST_DATE_CACHE["map"] is not None:
        return _LIST_DATE_CACHE["map"]
    m = {}
    try:
        rows = conn.execute("SELECT code, list_date FROM security").fetchall()
        m = {r[0]: r[1] for r in rows if r[1]}
    except Exception as e:
        log.debug("载入 list_date 失败: %s", e)
    _LIST_DATE_CACHE["conn_id"] = id(conn)
    _LIST_DATE_CACHE["map"] = m
    return m


def main_board_universe(ctx, codes, cfg, date=None):
    """主板宇宙硬约束过滤(手册 §股票池硬约束)。所有策略候选池统一走此函数。

    过滤规则(缺数据的软指标从宽,硬指标从严):
      硬约束: 主板前缀 + is_tradable(非停牌/非ST/非退市/非北交所/非科创创业/上市满60日)
      上市≥2年: 优先用 security.list_date;缺失则用后复权收盘历史长度代理(≥min_list_days 根)
      总市值≥80亿: fundamental.market_cap;缺失(None/0)则从宽保留(不因数据缺失误杀)
      日均成交≥阈值: avg_amount(20);低于阈值剔除(手册流动性硬约束)

    Args:
        ctx: DataContext / SqlContext
        codes: 候选代码列表
        cfg: 配置字典
        date: 交易日(默认取 ctx.date)

    Returns:
        list[str]: 通过主板硬约束的代码
    """
    if date is None:
        date = getattr(ctx, "date", None)
    risk = (cfg.get("risk") or {})
    custom = (cfg.get("custom") or {})
    min_list_days = int(custom.get("min_list_days", 480))          # ≈2年交易日
    min_market_cap = float(risk.get("min_market_cap", 8_000_000_000))  # 80亿元
    min_avg_amount = float(risk.get("min_avg_amount", 80_000_000))     # 8000万元
    two_year_cut = None
    if date:
        y, rest = str(date)[:4], str(date)[4:]
        try:
            two_year_cut = f"{int(y) - 2}{rest}"                    # 两年前同日
        except Exception:
            two_year_cut = None

    ld_map = None
    out = []
    for code in codes:
        # 1) 主板前缀(硬)
        if _bare6(code)[:3] not in _MAIN_BOARD_PREFIX:
            continue
        # 2) 可交易(非停牌/ST/退市/北交所/科创创业、上市满60日)(硬)
        try:
            if date and hasattr(ctx, "is_tradable") and not ctx.is_tradable(code, date):
                continue
        except Exception:
            pass
        # 3) 上市≥2年:优先 list_date,回退历史长度代理
        listed_ok = None
        if two_year_cut is not None:
            if ld_map is None:
                ld_map = _list_date_map(getattr(ctx, "conn", None)) if getattr(ctx, "conn", None) else {}
            ld = ld_map.get(code)
            if ld:
                listed_ok = (str(ld) <= two_year_cut)
        if listed_ok is None:
            try:
                listed_ok = len(ctx.close(code, min_list_days)) >= min_list_days
            except Exception:
                listed_ok = True   # 无法判断则从宽
        if not listed_ok:
            continue
        # 4) 总市值≥80亿(缺数据从宽)
        try:
            f = ctx.fundamental(code) or {}
            mc = f.get("market_cap")
            if mc and mc > 0 and mc < min_market_cap:
                continue
        except Exception:
            pass
        # 5) 日均成交额≥阈值(硬,流动性)
        try:
            if date and ctx.avg_amount(code, 20) < min_avg_amount:
                continue
        except Exception:
            pass
        out.append(code)
    return out


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
