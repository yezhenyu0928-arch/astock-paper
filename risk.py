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
    """消息面敞口系数(SPEC_NEWS N3)。P12 由 news_engine 提供市场分→系数;
    未启用/无信号时为 1.0。只降不升(利好不追)。"""
    if not (cfg.get("news_layer") or {}).get("enabled"):
        return 1.0
    try:
        import news_engine
        return news_engine.market_exposure_mult(date, ctx, cfg)
    except Exception:
        return 1.0


def post_check(date, ctx, orders, states, cfg, market_frozen=False):
    date = util.to_date_str(date)
    accounts = {sid: st["account"] for sid, st in states.items()}
    max_pos = cfg["risk"]["max_position_pct"]
    min_amt = cfg["risk"]["min_avg_amount"]
    stop = cfg["risk"]["stop_loss"]

    # 规则5:止损(遍历持仓生成强制 sell)
    stop_orders = []
    for sid, acct in accounts.items():
        stype = _stop_type(sid)
        if stype == "none":
            continue
        thr = stop.get(stype, 0.12)
        for code, pos in acct.positions.items():
            cur = ctx.raw_close(code)
            if cur and pos.avg_cost and (cur / pos.avg_cost - 1) < -thr:
                stop_orders.append(Order(strategy_id=sid, code=code, side="sell", weight=0.0,
                                         reason=f"止损(浮亏{cur/pos.avg_cost-1:.1%}>{thr:.0%})",
                                         signal_date=date))
    orders = list(orders) + stop_orders

    # 检测"轮动置换":同策略存在对当前持仓的卖出 → 视为换仓而非新开,大盘冻结时予以保留
    rotate_sids = set()
    for o in orders:
        if o.side == "sell":
            acct = accounts.get(o.strategy_id)
            if acct and o.code in acct.positions:
                rotate_sids.add(o.strategy_id)

    mult = _exposure_mult(date, ctx, cfg)
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
            # 规则6:消息面敞口(只降险,且记录被削/删明细,使"为何无交易"可追溯)
            if mult < 1.0:
                new_w = round(o.weight * mult, 6)
                log.info("消息面降敞口 %s %s: 权重 %s ×%s → %s", o.strategy_id, o.code, o.weight, mult, new_w)
                o.weight = new_w
                o.reason = (o.reason or "") + f"[消息面降敞口×{mult}]"
                if o.weight <= 0:
                    log.warning("消息面敞口×%s 抹平买单(已删) %s %s", mult, o.strategy_id, o.code)
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

    # 去重:同 sid+code+side 只留一单(止损/清仓/策略信号可能重叠),清仓优先
    dedup = {}
    for o in kept:
        k = (o.strategy_id, o.code, o.side)
        if k not in dedup or (o.side == "sell" and o.weight == 0):
            dedup[k] = o
    return list(dedup.values())


def _price_of(ctx):
    def f(code):
        return ctx.raw_close(code) or 0.0
    return f


def _is_etf(code):
    six = util.bare(code)
    return six[0] == "5" or six[:2] in ("15", "16", "18")
