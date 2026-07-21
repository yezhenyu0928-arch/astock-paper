# -*- coding: utf-8 -*-
"""统一红利质量多因子选股底座(mf = multi-factor)。

s1_dividend@v2(S1DividendQuality) 在 42 只大蓝筹宇宙里跑出 +14.1%/5.2%DD,
证明"高股息 + 连续分红 + ROE质量 + 低波 + 估值 + 新闻"排名法在此数据集唯一有效。
本模块把该逻辑抽象为可参数化底座, 供 s4/s8/s14/s15 各自带风格倾斜复用,
让 6 个策略都能达成"正收益 + 低回撤", 而非因子风格错配导致空仓/跑输。

所有取数经 ctx(DataContext)走 <=信号日 防未来函数。
"""
import logging
from statistics import pstdev
from models import Order
from strategies import common, news_guard
import fundamental as F
import factors as _fac

log = logging.getLogger("mf_core")

POOL_INDEX = "sh000300"  # 沪深300 大盘红利票池(与 s1 一致)


def _roe_quality_ok(code, date, conn, roe_years=3, roe_min=0.08):
    try:
        ok, roe = F.roe_quality(code, date, years=roe_years, min_roe=roe_min, conn=conn)
        return ok, roe
    except Exception:
        return False, 0.0


def _news_score(date, code, conn):
    try:
        import news_engine as ne
        return ne.get_stock_sentiment_score(date, code, conn=conn) or 0.0
    except Exception:
        return 0.0


def _growth_score(date, codes, conn):
    """盈利同比(earnings YoY)排名因子, 单条 SQL 批量取近两年净利润(防未来函数 pub_date<=date)。
    返回 {code: 同比增幅}(None=数据不足, 给中性 0)。仅当 weights 含 'growth'>0 时才调用(不影响其他策略)。"""
    try:
        placeholders = ",".join("?" for _ in codes)
        d = str(date)[:10]
        rows = conn.execute(
            f"SELECT code, net_profit FROM stock_annual "
            f"WHERE code IN ({placeholders}) AND pub_date IS NOT NULL AND pub_date<>'' AND pub_date<=? "
            f"ORDER BY code, stat_year DESC", (*codes, d)).fetchall()
        by_code = {}
        for code, np0 in rows:
            by_code.setdefault(code, []).append(np0)
        out = {}
        for code, lst in by_code.items():
            if len(lst) >= 2 and lst[1] is not None and lst[1] != 0 and lst[0] is not None:
                out[code] = (lst[0] - lst[1]) / abs(lst[1])
            else:
                out[code] = None
        return out
    except Exception:
        return {}


def _industry_leadership(cand, ind_map):
    """个股行业地位因子(可回测的'新闻/行业地位'代理): 同一行业内按 ROE 质量排名,
    业内质量龙头(高 ROE)得高分 —— 市值中性, 不与小盘倾斜(cap_tilt)打架。
    返回 {code: 0..1}。实盘可由 news_engine 主题扫描(行业ETF信号)进一步增强。"""
    from collections import defaultdict
    groups = defaultdict(list)
    for c in cand:
        groups[ind_map.get(c[0]) or "未知"].append(c)
    score = {}
    for ind, members in groups.items():
        if len(members) <= 1:
            for c in members:
                score[c[0]] = 1.0
            continue
        roes = {c[0]: (c[3] or 0.0) for c in members}   # c[3] = roe
        maxro = max(roes.values()) or 1.0
        for c in members:
            score[c[0]] = roes[c[0]] / maxro             # 业内质量龙头(ROE 归一)
    return score


def select(ctx, date, account, params, strategy_id, config):
    """红利质量多因子选股。返回 selection dict, 供 build_orders 使用。

    params 关键项:
      min_dividend_yield, dividend_years, roe_years, roe_min,
      hold_n, max_per_industry, low_vol_pct,
      weights = {dividend, low_vol, roe, valuation, news, cap, value}
      cap_tilt(bool): 偏小市值排名加分
      value_tilt(bool): 偏低 PE/PB 排名加分(深度价值)
      regime_downsize(bool): 宏观 regime 自适应降仓(eff 缩减)
    """
    min_dy = params.get("min_dividend_yield", 0.04)
    years = params.get("dividend_years", 3)
    low_vol_pct = params.get("low_vol_pct", 0.30)
    roe_years = params.get("roe_years", 3)
    roe_min = params.get("roe_min", 0.08)
    hold_n = params.get("hold_n", 10)
    max_per_ind = params.get("max_per_industry", 3)
    cap_tilt = params.get("cap_tilt", False)
    value_tilt = params.get("value_tilt", False)
    regime_downsize = params.get("regime_downsize", False)
    # 动量(12-1月, 跳过最近1月避免短期反转): 趋势择时强收益来源
    mom_win = params.get("momentum_window", 252)
    mom_skip = params.get("momentum_skip", 21)
    mom_min = params.get("momentum_min", None)   # 硬门槛: 仅保留 >= mom_min 的标的(趋势上行); None=不筛
    w = dict(params.get("weights", {"dividend": 0.25, "low_vol": 0.15, "roe": 0.20,
                                    "valuation": 0.10, "news": 0.12, "industry": 0.15,
                                    "momentum": 0.20}))

    eff = common.effective_hold_n(hold_n, account.init_capital, config, strategy_id)
    # regime_downsize 现在缩放【总敞口】(而不仅是持仓数): ratio 直接乘到 weight_per,
    # 使坏行情真正降仓(原实现只减 eff, 而 target_weight 归一化使总仓恒为~98%, 降仓无效)。
    # ratio 在 regime_downsize 关闭时取 1.0(满仓)。
    ratio = 1.0
    if regime_downsize:
        try:
            import macro
            r = macro.compute_market_regime(date, conn=ctx.conn)
            score = r.get("score", 60)
            rgood = params.get("regime_good", 1.0)
            rmid = params.get("regime_mid", 0.75)
            rbad = params.get("regime_bad", 0.5)
            ratio = rgood if score >= 60 else (rmid if score >= 40 else rbad)
        except Exception:
            pass
    weight_per = common.target_weight(eff) * ratio

    pool = ctx.members(POOL_INDEX, date)
    pool = common.main_board_universe(ctx, pool, config, date)

    cand = []  # (code, dy, vol, roe, news, pe, mcap)
    for code in pool:
        if not ctx.is_tradable(code, date):
            continue
        f = ctx.fundamental(code)
        if not f or not f.get("dividend_yield") or f["dividend_yield"] < min_dy:
            continue
        if ctx.dividend_years(code, years) < years:
            continue
        ok, roe = _roe_quality_ok(code, date, ctx.conn, roe_years, roe_min)
        if not ok:
            continue
        c = ctx.close(code, 251)
        if len(c) < 200:
            continue
        rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
        vol = pstdev(rets) if len(rets) > 1 else 9.9
        pe = f.get("pe")
        mcap = f.get("market_cap") or 0.0
        ns = _news_score(date, code, ctx.conn)
        # 动量: 12-1月收益(close[-1]/close[-(win+skip)]-1), 数据不足给 None
        mom = None
        if mom_win:
            mcs = ctx.close(code, mom_win + mom_skip + 1)
            if len(mcs) >= mom_win + mom_skip + 1 and mcs[-(mom_win + mom_skip + 1)]:
                mom = mcs[-1] / mcs[-(mom_win + mom_skip + 1)] - 1
        cand.append((code, f["dividend_yield"], vol, roe, ns, pe, mcap, mom))

    if not cand:
        return {"target": [], "weight_per": 0.0, "meta": {}, "cand_codes": set(),
                "keep_codes": set(), "full_rank": {}, "ind_map": {},
                "eff": eff, "empty_reason": "无满足股息率/分红/ROE门槛标的"}

    # —— 新闻/公告/动态守卫 ——
    _cc = [c[0] for c in cand]
    _ind = {}
    try:
        _ind = _fac.get_industry(ctx.conn, _cc)
    except Exception:
        pass
    # 个股行业地位因子(可回测的'新闻/行业地位'代理): 龙头加分
    ind_lead_score = _industry_leadership(cand, _ind)
    # 成长因子(盈利同比)排名: 仅当 weights 含 growth 时计算(默认权重不含, 不影响 s4/s8/s14/s15)
    grow_score = _growth_score(date, _cc, ctx.conn) if w.get("growth") else {}
    _ban_n, _ = news_guard.guard_candidates(date, _cc, ctx.conn, config)
    _ban_i = news_guard.guard_industry(date, _cc, ctx.conn, config, _ind)
    _ban_s = {c for c in _cc if news_guard.structural_ban(date, c, ctx)[0]}
    _banned = _ban_n | _ban_i | _ban_s
    if _banned:
        cand = [c for c in cand if c[0] not in _banned]
    if not cand:
        return {"target": [], "weight_per": 0.0, "meta": {}, "cand_codes": set(),
                "keep_codes": set(), "full_rank": {}, "ind_map": {},
                "eff": eff, "empty_reason": "新闻/结构守卫清空候选"}

    # 低波后 N% 优选(low_vol_pct 越大, 保留越多候选含较高收益/较高波动标的)
    cand.sort(key=lambda x: x[2])
    keep = cand[:max(eff, int(len(cand) * low_vol_pct))]

    # 动量硬门槛(趋势择时): 仅保留近期上行标的, 剔除深跌趋势; 空仓等待下一月
    if mom_min is not None:
        keep = [c for c in keep if (c[7] or 0) >= mom_min]
        if not keep:
            return {"target": [], "weight_per": 0.0, "meta": {}, "cand_codes": set(),
                    "keep_codes": set(), "full_rank": {}, "ind_map": {},
                    "eff": eff, "empty_reason": "动量门槛剔除全部候选(空仓等待)"}

    by_dy = sorted(keep, key=lambda x: x[1], reverse=True)
    dy_rank = {c[0]: i for i, c in enumerate(by_dy)}
    by_vol = sorted(keep, key=lambda x: x[2])
    vol_rank = {c[0]: i for i, c in enumerate(by_vol)}
    by_roe = sorted(keep, key=lambda x: x[3], reverse=True)
    roe_rank = {c[0]: i for i, c in enumerate(by_roe)}
    by_news = sorted(keep, key=lambda x: x[4], reverse=True)
    news_rank = {c[0]: i for i, c in enumerate(by_news)}
    by_pe = sorted(keep, key=lambda x: (x[5] is None, x[5] if x[5] is not None else 1e9))
    pe_rank = {c[0]: i for i, c in enumerate(by_pe)}
    # 动量排名(收益越高名次越前; 缺失者排末尾)
    by_mom = sorted(keep, key=lambda x: (x[7] is None, x[7] if x[7] is not None else -9e9),
                    reverse=True)
    mom_rank = {c[0]: i for i, c in enumerate(by_mom)}
    # 偏小市值 / 偏低估值 倾斜排名
    cap_rank = {c[0]: i for i, c in enumerate(sorted(keep, key=lambda x: x[6]))} if cap_tilt else {}
    val_rank = {c[0]: i for i, c in enumerate(sorted(keep, key=lambda x: (x[5] is None, x[5] if x[5] is not None else 1e9)))} if value_tilt else {}
    # 个股行业地位排名(龙头优先): 行业内市值/ROE 综合, 越高名次越前
    ind_lead_rank = {code: i for i, code in enumerate(
        sorted(ind_lead_score, key=lambda x: -ind_lead_score.get(x, 0)))}
    # 成长(盈利同比)排名: 越高名次越前; 缺失者排末尾
    grow_rank = {code: i for i, code in enumerate(
        sorted(grow_score, key=lambda x: -(grow_score.get(x) or -9e9)))} if grow_score else {}

    def _score(c):
        """按各因子名次加权打分(名次越小越优); 修复原 _w(code) 闭包误用最后一只候选的 bug。
        新增 industry 项: 个股行业地位(龙头)加分; news 项在实盘取真实舆情分, 回测取 0(由 industry 代理)。"""
        code = c[0]
        return (w.get("dividend", 0.0) * dy_rank.get(code, len(keep))
                + w.get("low_vol", 0.0) * vol_rank.get(code, len(keep))
                + w.get("roe", 0.0) * roe_rank.get(code, len(keep))
                + w.get("valuation", 0.0) * pe_rank.get(code, len(keep))
                + w.get("news", 0.0) * news_rank.get(code, len(keep))
                + w.get("industry", 0.0) * ind_lead_rank.get(code, len(keep))
                + w.get("growth", 0.0) * grow_rank.get(code, len(keep))
                + w.get("cap", 0.0) * cap_rank.get(code, len(keep))
                + w.get("value", 0.0) * val_rank.get(code, len(keep))
                + w.get("momentum", 0.0) * mom_rank.get(code, len(keep)))

    scored = sorted(keep, key=_score)

    ind_map = _ind
    ind_count, target = {}, []
    for c in scored:
        code = c[0]
        ind = ind_map.get(code) or "未知"
        if ind_count.get(ind, 0) >= max_per_ind:
            continue
        target.append(code)
        ind_count[ind] = ind_count.get(ind, 0) + 1
        if len(target) >= eff:
            break

    full_rank = {c[0]: i + 1 for i, c in enumerate(scored)}
    cand_codes = {c[0] for c in cand}
    keep_codes = {c[0] for c in keep}
    meta = {c[0]: {"dy": c[1], "roe": c[3], "pe": c[5]} for c in keep}

    return {"target": target, "weight_per": weight_per, "meta": meta,
            "cand_codes": cand_codes, "keep_codes": keep_codes,
            "full_rank": full_rank, "ind_map": ind_map,
            "eff": eff, "empty_reason": None}


def build_orders(ctx, date, account, sel, params, strategy_id, config, stop_pct=0.10):
    """依据 selection 构建买卖单, 含持有期跟踪止损。"""
    target = sel["target"]
    wgt = sel["weight_per"]
    tset = set(target)
    orders = []
    held = set(account.positions.keys())
    forced = news_guard.guard_holdings(date, list(held), ctx.conn, config)

    for code in held:
        if code in target and code not in forced:
            continue
        nm = ctx.name(code)
        pos = account.positions.get(code)
        peak = getattr(pos, "highest_close", None) or getattr(pos, "avg_cost", None)
        close = ctx.close(code, 1)[-1] if len(ctx.close(code, 1)) else None
        breach = (close is not None and peak is not None and close < peak * (1 - stop_pct))
        if code in forced:
            reason = f"{strategy_id}:{nm}新闻黑天鹅,同步清仓"
        elif breach:
            reason = f"{strategy_id}:{nm}自高点回撤>{stop_pct:.0%},跟踪止损"
        elif code in sel["full_rank"]:
            reason = f"{strategy_id}:{nm}综合排名掉出前{sel['eff']},卖出"
        else:
            reason = f"{strategy_id}:{nm}不再满足选股门槛,卖出"
        orders.append(Order(strategy_id, code, "sell", 0.0, reason, date))

    for code in target:
        if code not in held:
            nm = ctx.name(code)
            m = sel["meta"].get(code, {})
            dy = m.get("dy", 0.0)
            orders.append(Order(strategy_id, code, "buy", wgt,
                               f"{strategy_id}:买入{nm}(股息率{dy:.1%})", date))
    return orders
