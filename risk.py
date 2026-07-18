# -*- coding: utf-8 -*-
"""风控层(SPEC 模块4 完整版 + SPEC_NEWS N3 敞口接入)。
策略只表达观点,止损/仓位上限/熔断/流动性/大盘冻结全部由本层统一处理。
接口:
  pre_check(date, ctx, states, cfg) -> {'market_frozen':bool,'forced_orders':[Order],'alerts':[str]}
  post_check(date, ctx, orders, states, cfg, market_frozen=False) -> list[Order]
其中 states = {sid: {'account':Account,'highest_nav':float,...}}(engine.state)。
"""
import logging
import util
from models import Order

log = logging.getLogger("risk")

MARKET_PROXY = "sh510300"   # 大盘代理(沪深300ETF)

# 策略止损类型:trend(8%) / rotation(12%) / none
_STOP_TYPE = {"s3": "trend", "s1": "rotation", "s2": "rotation", "s4": "rotation", "s5": "none"}


def _stop_type(sid):
    return _STOP_TYPE.get(sid.split("_")[0], "rotation")


def _clearance_orders(sid, account, date, reason):
    """对某账户全部持仓生成清仓 sell(weight=0)。"""
    return [Order(strategy_id=sid, code=code, side="sell", weight=0.0,
                  reason=reason, signal_date=date)
            for code, pos in account.positions.items() if pos.shares > 0]


def pre_check(date, ctx, states, cfg):
    date = util.to_date_str(date)
    max_dd = cfg["risk"]["strategy_max_drawdown"]
    forced, alerts = [], []

    # 各账户回撤熔断(可重置):首次触发→清仓+告警+冻结;已冻结(上轮已清仓)→重置峰值+解冻,继续参赛。
    # 每次触发=一次全清仓+一次告警(README:出局与否由用户看告警后决定,系统只自动降险)。
    for sid, st in states.items():
        acct = st["account"]
        if acct.frozen:
            # 上一轮已触发并挂出清仓单,本轮已在 settle 中清空 → 重置基准、解冻
            st["highest_nav"] = acct.nav
            acct.frozen = False
            continue
        peak = max(st.get("highest_nav", 1.0), acct.nav)
        dd = 1 - acct.nav / peak if peak > 0 else 0
        if dd > max_dd:
            alerts.append(f"🔴 策略 {sid} 回撤 {dd:.1%} 触发熔断线 {max_dd:.0%},清仓降险并告警(次日重置参赛)")
            log.warning(alerts[-1])
            acct.frozen = True
            forced.extend(_clearance_orders(sid, acct, date, f"熔断清仓(回撤{dd:.1%})"))

    # 大盘冻结:单日跌>day_drop 或 20日跌>m20_drop
    market_frozen = False
    closes = ctx.close(MARKET_PROXY, 21)
    if len(closes) >= 2:
        day_ret = closes[-1] / closes[-2] - 1
        m20_ret = closes[-1] / closes[0] - 1 if len(closes) >= 21 else 0
        if day_ret < -cfg["risk"]["market_freeze"]["day_drop"] or \
           m20_ret < -cfg["risk"]["market_freeze"]["m20_drop"]:
            market_frozen = True
            alerts.append(f"🔴 大盘冻结:今日{day_ret:.1%} / 20日{m20_ret:.1%},今日禁止开仓")
            log.warning(alerts[-1])

    return {"market_frozen": market_frozen, "forced_orders": forced, "alerts": alerts}


def _exposure_mult(date, ctx, cfg):
    """综合敞口系数(只降不升) = min(消息面, 宏观7指标)。
    消息面: news_engine 市场分→系数(手册消息面层);
    宏观:   macro.macro_exposure_mult(score_7→总仓位0-90%,手册宏观择时)。
    两者均只降不升,取最严一档。未启用/无数据/异常 → 1.0(不干预)。"""
    # 消息面
    news_mult = 1.0
    if (cfg.get("news_layer") or {}).get("enabled"):
        try:
            import news_engine
            news_mult = news_engine.market_exposure_mult(date, ctx, cfg)
        except Exception:
            news_mult = 1.0
    # 宏观 7 指标
    try:
        import macro
        macro_mult = macro.macro_exposure_mult(date, ctx, cfg)
    except Exception:
        macro_mult = 1.0
    return min(news_mult, macro_mult)


def _held_days(ctx, buy_date, date):
    """持仓自然交易日数(基于 trade_calendar.is_open)。失败返回 None。"""
    try:
        conn = getattr(ctx, "conn", None)
        if conn is None or not buy_date:
            return None
        r = conn.execute(
            "SELECT COUNT(*) FROM trade_calendar WHERE cal_date>? AND cal_date<=? AND is_open=1",
            (str(buy_date), str(date))).fetchone()
        return int(r[0]) if r else None
    except Exception:
        return None


def _drawdown_mult(acct, st, tiers):
    """回撤分层递进降险(手册风控体系)。按账户当前回撤匹配最深档位的敞口系数。
    tiers = [{'dd':0.04,'mult':0.8}, ...](按 dd 升序);回撤<最小档→1.0。"""
    if not tiers:
        return 1.0
    peak = max(st.get("highest_nav", 1.0), acct.nav)
    dd = 1 - acct.nav / peak if peak > 0 else 0
    mult = 1.0
    for t in tiers:
        try:
            if dd >= float(t["dd"]):
                mult = float(t["mult"])
        except Exception:
            continue
    return mult


def post_check(date, ctx, orders, states, cfg, market_frozen=False):
    date = util.to_date_str(date)
    accounts = {sid: st["account"] for sid, st in states.items()}
    max_pos = cfg["risk"]["max_position_pct"]
    min_amt = cfg["risk"]["min_avg_amount"]
    stop = cfg["risk"]["stop_loss"]

    # 规则5:止损 + 移动止盈 + 时间止损(遍历持仓生成强制 sell)。手册:硬止损8% + 移动止盈6% + 时间止损。
    trail_tp = cfg["risk"].get("trailing_take_profit", 0) or 0
    ts_days = cfg["risk"].get("time_stop_days") or 0
    ts_min = cfg["risk"].get("time_stop_min_return", 0.0) or 0.0
    stop_orders = []
    for sid, acct in accounts.items():
        stype = _stop_type(sid)
        thr = None if stype == "none" else stop.get(stype, 0.12)
        for code, pos in acct.positions.items():
            cur = ctx.raw_close(code)
            if not cur or not pos.avg_cost:
                continue
            pnl = cur / pos.avg_cost - 1
            # 5a) 硬止损:自成本浮亏超阈值
            if thr is not None and pnl < -thr:
                stop_orders.append(Order(strategy_id=sid, code=code, side="sell", weight=0.0,
                                         reason=f"止损(浮亏{pnl:.1%}>{thr:.0%})",
                                         signal_date=date))
                continue
            # 5b) 移动止盈:盈利状态下,自持有期最高收盘回撤超阈值即锁定
            hc = getattr(pos, "highest_close", 0) or 0
            if trail_tp and hc > 0 and cur > pos.avg_cost and (cur / hc - 1) < -trail_tp:
                stop_orders.append(Order(strategy_id=sid, code=code, side="sell", weight=0.0,
                                         reason=f"移动止盈(自峰值回撤{1-cur/hc:.1%}>{trail_tp:.0%},锁定{pnl:+.1%})",
                                         signal_date=date))
                continue
            # 5c) 时间止损:持仓≥time_stop_days 且 收益<time_stop_min_return → 退出(手册:不达预期时间的仓位清理)
            if ts_days and ts_days > 0 and pos.buy_date:
                hd = _held_days(ctx, pos.buy_date, date)
                if hd is not None and hd >= ts_days and pnl < ts_min:
                    stop_orders.append(Order(strategy_id=sid, code=code, side="sell", weight=0.0,
                                             reason=f"时间止损(持有{hd}日收益{pnl:.1%}<{ts_min:.0%})",
                                             signal_date=date))
    orders = list(orders) + stop_orders

    # 检测"轮动置换":同策略存在对当前持仓的卖出 → 视为换仓而非新开,大盘冻结时予以保留
    rotate_sids = set()
    for o in orders:
        if o.side == "sell":
            acct = accounts.get(o.strategy_id)
            if acct and o.code in acct.positions:
                rotate_sids.add(o.strategy_id)

    news_mult = _exposure_mult(date, ctx, cfg)
    tiers = cfg["risk"].get("drawdown_tiers") or []
    kept = []
    for o in orders:
        acct = accounts.get(o.strategy_id)
        if acct is None:
            continue
        # 规则2:冻结策略只保留清仓 sell
        if acct.frozen and not (o.side == "sell" and o.weight == 0):
            continue
        if o.side == "buy":
            # 规则1:大盘冻结删所有"新开仓"buy;但保留同策略的"轮动置换"(已卖出持仓→换入新标的)
            if market_frozen and o.strategy_id not in rotate_sids:
                log.info("大盘冻结删新开单 %s %s", o.strategy_id, o.code)
                continue
            # 规则4:个股流动性(ETF 豁免)
            if not _is_etf(o.code):
                if ctx.avg_amount(o.code, 20) < min_amt:
                    log.info("流动性不足删单 %s %s", o.strategy_id, o.code)
                    continue
            # 规则6:综合敞口 = 消息面 × 回撤分层降险(均只降不升,手册风控体系)
            dd_mult = _drawdown_mult(acct, states.get(o.strategy_id, {}), tiers)
            mult = round(news_mult * dd_mult, 6)
            if mult < 1.0:
                new_w = round(o.weight * mult, 6)
                tag = f"[敞口×{mult}(消息{news_mult}/回撤{dd_mult})]"
                log.info("降敞口 %s %s: 权重 %s → %s %s", o.strategy_id, o.code, o.weight, new_w, tag)
                o.weight = new_w
                o.reason = (o.reason or "") + tag
                if o.weight <= 0:
                    log.warning("敞口×%s 抹平买单(已删) %s %s", mult, o.strategy_id, o.code)
                    continue
            # 规则3:单票上限(成交后占比预估>max_pos → 削)。仅个股;ETF 是分散工具,豁免
            if not _is_etf(o.code):
                total = acct.total(_price_of(ctx))
                pos = acct.positions.get(o.code)
                held_val = (pos.shares * (ctx.raw_close(o.code) or 0)) if pos else 0
                target_val = total * o.weight
                if total > 0 and (held_val + target_val) / total > max_pos:
                    new_w = max(0.0, max_pos - held_val / total)
                    if new_w <= 0:
                        continue
                    o.weight = round(new_w, 6)
                    o.reason = (o.reason or "") + f"[单票上限{max_pos:.0%}削仓]"
        kept.append(o)

    # 组合级上限:总仓位≤90%(现金≥10%) + 单行业≤25%(手册风控体系)
    kept = _apply_portfolio_caps(date, ctx, kept, accounts, cfg)

    # 去重:同 sid+code+side 只留一单(止损/清仓/策略信号可能重叠),清仓优先
    dedup = {}
    for o in kept:
        k = (o.strategy_id, o.code, o.side)
        if k not in dedup or (o.side == "sell" and o.weight == 0):
            dedup[k] = o
    return list(dedup.values())


def _apply_portfolio_caps(date, ctx, kept, accounts, cfg):
    """组合级上限(手册风控):总仓位≤total_position_max(现金≥cash_floor) + 单行业≤industry_max_pct。
    仅按比例缩放个股 buy 单;sell/清仓不动。缺行业数据的个股归'未知',不参与行业封顶(避免误杀)。"""
    tot_max = cfg["risk"].get("total_position_max")
    ind_max = cfg["risk"].get("industry_max_pct")
    if not tot_max and not ind_max:
        return kept
    price_of = _price_of(ctx)
    by_sid = {}
    for o in kept:
        by_sid.setdefault(o.strategy_id, []).append(o)
    for sid, olist in by_sid.items():
        acct = accounts.get(sid)
        if acct is None:
            continue
        buys = [o for o in olist if o.side == "buy" and not _is_etf(o.code) and o.weight > 0]
        if not buys:
            continue
        # 1) 总仓位上限:sum(buy weight) ≤ tot_max(等价现金≥1-tot_max)
        if tot_max:
            s = sum(o.weight for o in buys)
            if s > tot_max:
                k = tot_max / s
                for o in buys:
                    o.weight = round(o.weight * k, 6)
                    o.reason = (o.reason or "") + f"[总仓≤{tot_max:.0%}缩放]"
        # 2) 单行业上限:含现存持仓占比,超限按比例缩放该行业新买单
        if ind_max and getattr(ctx, "conn", None):
            try:
                import factors
                total = acct.total(price_of) or 0
                ind_map = factors.get_industry(ctx.conn, [o.code for o in buys])
                held_codes = list(acct.positions.keys())
                held_ind = factors.get_industry(ctx.conn, held_codes) if held_codes else {}
                held_frac = {}
                if total > 0:
                    for c, pos in acct.positions.items():
                        ind = held_ind.get(c) or "未知"
                        held_frac[ind] = held_frac.get(ind, 0) + (pos.shares * (price_of(c) or 0)) / total
                grp = {}
                for o in buys:
                    grp.setdefault(ind_map.get(o.code) or "未知", []).append(o)
                for ind, os in grp.items():
                    if ind == "未知":
                        continue
                    buy_sum = sum(o.weight for o in os)
                    base = held_frac.get(ind, 0)
                    if base + buy_sum > ind_max:
                        room = max(0.0, ind_max - base)
                        k = (room / buy_sum) if buy_sum > 0 else 0
                        for o in os:
                            o.weight = round(o.weight * k, 6)
                            o.reason = (o.reason or "") + f"[行业{ind}≤{ind_max:.0%}缩放]"
            except Exception as e:
                log.debug("行业上限计算失败: %s", e)
    return [o for o in kept if not (o.side == "buy" and o.weight <= 0)]


def _price_of(ctx):
    def f(code):
        return ctx.raw_close(code) or 0.0
    return f


def _is_etf(code):
    six = util.bare(code)
    return six[0] == "5" or six[:2] in ("15", "16", "18")
