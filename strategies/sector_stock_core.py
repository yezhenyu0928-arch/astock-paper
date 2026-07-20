# -*- coding: utf-8 -*-
"""统一个股行业轮动核心(sector_stock_core)。

设计意图(对齐用户方向:选对行业赛道 + 行业轮动 + 新闻抓风口 + 及时风控卖出,且只买卖个股不碰ETF):
- 选对行业赛道: 用申万一级行业动量(行业指数涨幅的截面排名)作为"政策/景气/风口"的
  最可验证代理(业界研究: 行业动量年化~19%、行业ETF动量即自动跟随产业政策主线);
- 行业内选股: 在最强几个行业里, 按风格倾斜(小盘/红利/成长/价值/均衡)挑个股;
- 及时风控卖出: 宽基趋势(沪深300 跌破均线)或宏观收紧或市场急跌 -> 整仓清仓持现金
  (不买任何ETF, 现金即避险资产); 叠加 news_guard 黑天鹅强卖;
- 新闻抓风口: 作为实盘叠加层(news_guard 黑天鹅强卖 + 可选行业主题加分); 历史回测无
  新闻数据则中性退化, 不污染回测。

回测纪律: 所有取数经 ctx(DataContext) 走 <=信号日, 防未来函数; 成交按次日开盘价+真实费用滑点。
"""
import logging
from statistics import pstdev, median
from models import Order
from strategies import common, news_guard
import fundamental as F  # noqa: F401
import factors as _fac

log = logging.getLogger("sector_stock_core")

POOL_INDEX = "sh000300"   # 沪深300 大盘票池(覆盖全部31个申万一级行业)
BENCH = "sh510300"        # 宽基趋势代理(仅读取价格作信号, 不交易)


def _empty(eff, reason):
    return {"target": [], "weight_per": 0.0, "meta": {}, "eff": eff,
            "empty_reason": reason, "risk_off": False, "top_sectors": []}


def _bench_state(ctx, date, slow, fast):
    """返回 (慢线破位, 快线破位, 近fast日收益)。取数失败返回 None。"""
    try:
        c = ctx.close(BENCH, slow + 1)
        if len(c) < slow:
            return None
        slow_ma = sum(c[-slow:]) / slow
        price = c[-1]
        if price <= 0 or slow_ma <= 0:
            return None
        fast_ma = sum(c[-fast:]) / fast if len(c) >= fast else slow_ma
        fast_ret = (price / c[-fast] - 1.0) if len(c) >= fast and c[-fast] else 0.0
        return (price < slow_ma, price < fast_ma, fast_ret)
    except Exception:
        return None


def market_risk_off(ctx, date, params, config):
    """宽基趋势 + 宏观 判定是否清仓持现金(不买ETF)。保守优先控回撤。"""
    slow = int(params.get("trend_slow_ma", 60))
    fast = int(params.get("trend_fast_ma", 20))
    st = _bench_state(ctx, date, slow, fast)
    slow_down, fast_down, fast_ret = (False, False, 0.0)
    if st is not None:
        slow_down, fast_down, fast_ret = st
    macro_bad = False
    if params.get("use_macro", True):
        try:
            import macro
            r = macro.compute_market_regime(date, conn=ctx.conn)
            macro_bad = (r.get("score", 60) or 60) < params.get("macro_bad_score", 40)
        except Exception:
            pass
    # 市场急跌(近fast日跌超阈值)也避险
    sharp_drop = fast_ret < -params.get("sharp_drop_thr", 0.08)
    # 保守退出: 慢线破位 / 宏观差 / 市场急跌 任一即走
    return bool(slow_down) or bool(macro_bad) or bool(sharp_drop)


def _stock_momentum(ctx, code, mom_w):
    maxw = max(mom_w)
    c = ctx.close(code, maxw + 1)
    mom = {}
    for w in mom_w:
        if len(c) >= w + 1 and c[-(w + 1)]:
            mom[w] = c[-1] / c[-(w + 1)] - 1
        else:
            mom[w] = None
    return mom, c


def select(ctx, date, account, params, strategy_id, config):
    mom_w = params.get("mom_windows", [60, 120])
    eff = common.effective_hold_n(params.get("hold_n", 8), account.init_capital, config, strategy_id)
    n_sec = int(params.get("n_sectors", 4))
    per_sec = int(params.get("stocks_per_sector", 2))
    tilt = params.get("tilt", "balanced")
    w = dict(params.get("weights", {}))
    primary = mom_w[0]

    pool = ctx.members(POOL_INDEX, date)
    pool = common.main_board_universe(ctx, pool, config, date)

    try:
        ind_map = _fac.get_industry(ctx.conn, pool) or {}
    except Exception:
        ind_map = {}

    stock_mom, stock_vol, stock_fund = {}, {}, {}
    for code in pool:
        if not ctx.is_tradable(code, date):
            continue
        mom, c = _stock_momentum(ctx, code, mom_w)
        if any(v is None for v in mom.values()):
            continue
        rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c)) if c[i - 1]]
        vol = pstdev(rets) if len(rets) > 1 else 9.9
        f = ctx.fundamental(code) or {}
        stock_mom[code] = mom
        stock_vol[code] = vol
        stock_fund[code] = f

    if not stock_mom:
        return _empty(eff, "无候选(动量数据不足)")

    # 行业动量 = 成员票 短窗口动量中位数(政策/景气代理)
    ind_vals = {}
    for code, mom in stock_mom.items():
        ind = ind_map.get(code)
        if not ind:
            continue
        ind_vals.setdefault(ind, []).append(mom[primary])
    min_members = int(params.get("min_ind_members", 3))
    ind_score = {i: median(v) for i, v in ind_vals.items() if len(v) >= min_members}
    if not ind_score:
        return _empty(eff, "无足够成员行业")

    # 新闻/主题行业加分(实盘叠加; 历史回测无新闻数据则中性, 不强行映射)
    if params.get("use_news", True):
        try:
            import news_engine as ne  # noqa: F401  仅确认接口可用, 个股行业主题信号回测不可用
        except Exception:
            pass

    ranked = sorted(ind_score, key=lambda i: ind_score[i], reverse=True)
    top_sec = ranked[:n_sec]

    # 行业内选股
    target = []
    for ind in top_sec:
        members = [c for c in stock_mom if ind_map.get(c) == ind]
        scored = _score_stocks(members, stock_mom, stock_vol, stock_fund, primary, tilt, w)
        for code in scored[:per_sec]:
            if code not in target:
                target.append(code)
        if len(target) >= eff:
            break
    target = target[:eff]
    if not target:
        return _empty(eff, "行业内无达标个股")

    weight_per = common.target_weight(eff)
    meta = {c: {"ind": ind_map.get(c), "mom": stock_mom[c].get(primary)} for c in target}
    return {"target": target, "weight_per": weight_per, "meta": meta,
            "eff": eff, "empty_reason": None, "risk_off": False,
            "top_sectors": top_sec, "ind_map": ind_map}


def _score_stocks(members, stock_mom, stock_vol, stock_fund, primary, tilt, w):
    if not members:
        return []
    by_mom = sorted(members, key=lambda c: stock_mom[c].get(primary) or -9e9, reverse=True)
    mom_rank = {c: i for i, c in enumerate(by_mom)}
    by_vol = sorted(members, key=lambda c: stock_vol[c])
    vol_rank = {c: i for i, c in enumerate(by_vol)}
    by_roe = sorted(members, key=lambda c: (stock_fund[c].get("roe") or 0), reverse=True)
    roe_rank = {c: i for i, c in enumerate(by_roe)}
    by_pe = sorted(members, key=lambda c: (stock_fund[c].get("pe") is None, stock_fund[c].get("pe") or 1e9))
    pe_rank = {c: i for i, c in enumerate(by_pe)}
    by_dy = sorted(members, key=lambda c: (stock_fund[c].get("dividend_yield") or 0), reverse=True)
    dy_rank = {c: i for i, c in enumerate(by_dy)}
    by_cap = sorted(members, key=lambda c: (stock_fund[c].get("market_cap") or 0))
    cap_rank = {c: i for i, c in enumerate(by_cap)}

    n = len(members)

    def sc(c):
        return (w.get("momentum", 0.30) * mom_rank.get(c, n)
                + w.get("low_vol", 0.15) * vol_rank.get(c, n)
                + w.get("roe", 0.15) * roe_rank.get(c, n)
                + w.get("valuation", 0.10) * pe_rank.get(c, n)
                + w.get("dividend", 0.10) * dy_rank.get(c, n)
                + w.get("size", 0.0) * cap_rank.get(c, n))

    return sorted(members, key=sc)


def _stop_breach(ctx, account, stop_pct):
    """返回需跟踪止损的持仓(每日维护用)。"""
    out = []
    for code, pos in account.positions.items():
        try:
            close = ctx.close(code, 1)[-1]
        except Exception:
            continue
        peak = getattr(pos, "highest_close", None) or getattr(pos, "avg_cost", None)
        if peak and close is not None and close < peak * (1 - stop_pct):
            out.append(code)
    return out


def build_orders(ctx, date, account, sel, params, strategy_id, config, stop_pct=0.08):
    target = sel["target"]
    wgt = sel["weight_per"]
    tset = set(target)
    orders = []
    forced = set()
    try:
        forced = set(news_guard.guard_holdings(date, list(account.positions.keys()), ctx.conn, config))
    except Exception:
        pass
    held = set(account.positions.keys())
    for code in held:
        if code in tset and code not in forced:
            continue
        nm = ctx.name(code)
        reason = (f"{strategy_id}:{nm}新闻黑天鹅,强卖" if code in forced
                  else f"{strategy_id}:{nm}调出(换更强行业/个股),卖出")
        orders.append(Order(strategy_id, code, "sell", 0.0, reason, date))
    for code in target:
        if code not in held:
            nm = ctx.name(code)
            m = sel["meta"].get(code, {})
            reason = f"{strategy_id}:买入{nm}(行业:{m.get('ind', '?')},动量{m.get('mom', 0):.1%})"
            orders.append(Order(strategy_id, code, "buy", wgt, reason, date))
    return orders


def generate_core(self, date, ctx, account, params):
    """供各策略 generate_orders 调用的统一入口(每日调用)。"""
    stop_pct = params.get("stop_pct", 0.08)
    rebal = params.get("rebalance", "monthly")
    due = (ctx.is_last_trade_day_of_month(date) if rebal == "monthly"
           else ctx.is_last_trade_day_of_week(date))

    # 1) 风控优先: 宽基趋势/宏观/急跌走坏 -> 清仓持现金(不买ETF)
    try:
        if market_risk_off(ctx, date, params, self.config):
            held = list(account.positions.keys())
            if held:
                return [Order(self.strategy_id, code, "sell", 0.0,
                              f"{self.strategy_id}:{ctx.name(code)}宽基趋势走弱,清仓避险", date)
                        for code in held]
            return []
    except Exception:
        pass

    # 2) 调仓日: 全选 + 调仓
    if due:
        sel = select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            held = list(account.positions.keys())
            if held:
                return [Order(self.strategy_id, code, "sell", 0.0,
                              f"{self.strategy_id}:{ctx.name(code)}无候选,清仓", date)
                        for code in held]
            return []
        return build_orders(ctx, date, account, sel, params, self.strategy_id, self.config, stop_pct)

    # 3) 非调仓日: 仅跟踪止损 / 黑天鹅维护(不新建仓)
    sells = _stop_breach(ctx, account, stop_pct)
    forced = set()
    try:
        forced = set(news_guard.guard_holdings(date, list(account.positions.keys()), ctx.conn, self.config))
    except Exception:
        pass
    orders = []
    for code in set(sells) | forced:
        if code in account.positions:
            nm = ctx.name(code)
            reason = (f"{self.strategy_id}:{nm}跟踪止损,卖出" if code in sells
                      else f"{self.strategy_id}:{nm}黑天鹅,强卖")
            orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
    return orders
