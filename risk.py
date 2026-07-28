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

# 策略级回撤熔断线(手册总回撤红线)。用户硬指标:全部策略回撤≤5%(低回撤不可破,
# 比高收益更不可妥协),故 s1/s4/s8/s13/s14/s15 熔断线全部锁 0.05。s1 原先豁免 0.10,
# 但用户最新指令"所有策略回撤≤5%"已覆盖,统一收紧到 0.05。
_STRATEGY_MAX_DD = {
    "s1": 0.20,
    "s4": 0.20, "s8": 0.20, "s13": 0.20, "s14": 0.20, "s15": 0.20,   # 2026-07-24 用户要求最大回撤≤20%,熔断线设20%
}

# ---------------------------------------------------------------------------
# 激进阵容风控放宽层(2026-07-27 用户明确选择"放宽风控冲收益")。
# 按 sid 前缀配置,只对白名单策略生效;保守阵容 s20-s23 仍守全局 0.10 熔断+大盘冻结。
# 诚实声明: 放宽后回撤可能到 25-40%,这是冲高收益的代价,已获用户确认。
# ---------------------------------------------------------------------------
_RISK_RELAX = {
    "s25": {
        "max_dd": 0.30,                # 熔断线 10% → 30%
        "market_freeze_exempt": True,  # 豁免大盘冻结(弱市仍可开仓,s24 回测证明冻结是收益≈0 的主因)
        "exposure_exempt": True,       # 豁免 宏观敞口×回撤分层降险(不降仓,让仓位打满)
        "trailing_tp_exempt": True,    # 豁免移动止盈 6%(让盈利奔跑)
        "time_stop_exempt": True,      # 豁免 30 日时间止损(小盘动量需要时间)
        "stop_override": 0.20,         # 个股硬止损 8% → 20%(小盘波动大,8% 必然频繁洗出)
    },
    # s26 微盘规模因子(2026-07-27 冲"年化≥10%且夏普>1"目标): 冻结豁免+熔断20%
    "s26": {
        "max_dd": 0.20,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.20,
    },
    # s29 小盘精选(复现国信金工,2026-07-27): 同 s26 档,熔断20%,止损20%
    "s29": {
        "max_dd": 0.20,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.20,
    },
    # s28 微盘涡轮版(2026-07-27 用户"少数放宽回撤40%"组): 熔断40%,止损35%
    "s28": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.35,
    },
    # s27 红利低波(防守进攻线,靠低波动顶夏普): 冻结豁免,熔断15%,止损15%
    "s27": {
        "max_dd": 0.15,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,    # 红利股拿住吃息,不被6%移动止盈洗出
        "time_stop_exempt": True,
        "stop_override": 0.15,
    },
    # s32 ROE质量选股(2026-07-27 穿透国信《基于ROE高质量选股》新建): 列入"少数放宽回撤40%" aggressive 组
    "s32": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.30,
    },
    # s35 长端动量(2026-07-27 穿透开源金工《长端动量2.0》新建): 少数放宽回撤40% aggressive 组
    "s35": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.35,
    },
    # s36 成长股图谱(2026-07-27 穿透国信《成长股投资》新建): 少数放宽回撤40% aggressive 组
    "s36": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.30,
    },
    # s37 超预期近似(2026-07-27 穿透国信《超预期投资》新建): 少数放宽回撤40% aggressive 组
    "s37": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.30,
    },
    # s38/s39 事件驱动择时(复现国信超预期44.9%/成长44.3% 近似): 放宽组回撤≤40%
    "s38": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.30,
    },
    "s39": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.30,
    },
    # s40/s41 冲刺20%+ 的 SUE/52周高 复合策略(放宽组, 回撤≤40%)
    "s40": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.30,
    },
    "s41": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.30,
    },
    "s42": {
        "max_dd": 0.40,
        "market_freeze_exempt": True,
        "exposure_exempt": True,
        "trailing_tp_exempt": True,
        "time_stop_exempt": True,
        "stop_override": 0.30,
    },
}


def _relax(sid):
    """返回该策略的风控放宽配置(无则空 dict = 走正常纪律)。"""
    return _RISK_RELAX.get(sid.split("_")[0], {})


def _stop_type(sid):
    return _STOP_TYPE.get(sid.split("_")[0], "rotation")


def _clearance_orders(sid, account, date, reason):
    """对某账户全部持仓生成清仓 sell(weight=0)。"""
    return [Order(strategy_id=sid, code=code, side="sell", weight=0.0,
                  reason=reason, signal_date=date)
            for code, pos in account.positions.items() if pos.shares > 0]


def pre_check(date, ctx, states, cfg):
    date = util.to_date_str(date)
    # 策略级熔断线(优先用 _STRATEGY_MAX_DD,缺省回退全局配置)
    _global_mdd = cfg["risk"].get("strategy_max_drawdown", 0.10)
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
        # 按策略前缀取熔断线;激进阵容(_RISK_RELAX)优先,其次 _STRATEGY_MAX_DD,最后全局
        rlx = _relax(sid)
        max_dd = rlx.get("max_dd") or _STRATEGY_MAX_DD.get(sid.split("_")[0], _global_mdd)
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
    """综合敞口系数 = macro 宏观择时仓位(仅用于回测).
    新闻敞口在回测期间跳过(避免 news_signal 历史数据误触发 0 仓位)."""
    # 消息面: 跳过
    news_mult = 1.0
    # 宏观 7 指标: 只用 baseline 0.85, 不打折
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
        rlx = _relax(sid)
        if rlx.get("stop_override"):
            thr = rlx["stop_override"]          # 激进阵容: 个股硬止损放宽
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
            # 5b) 移动止盈:盈利状态下,自持有期最高收盘回撤超阈值即锁定(激进阵容豁免,让盈利奔跑)
            hc = getattr(pos, "highest_close", 0) or 0
            if trail_tp and hc > 0 and cur > pos.avg_cost and (cur / hc - 1) < -trail_tp \
                    and not rlx.get("trailing_tp_exempt"):
                stop_orders.append(Order(strategy_id=sid, code=code, side="sell", weight=0.0,
                                         reason=f"移动止盈(自峰值回撤{1-cur/hc:.1%}>{trail_tp:.0%},锁定{pnl:+.1%})",
                                         signal_date=date))
                continue
            # 5c) 时间止损:持仓≥time_stop_days 且 收益<time_stop_min_return → 退出(激进阵容豁免)
            if ts_days and ts_days > 0 and pos.buy_date and not rlx.get("time_stop_exempt"):
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
        o_rlx = _relax(o.strategy_id)
        if o.side == "buy":
            # 规则1:大盘冻结删所有"新开仓"buy;但保留同策略的"轮动置换"(已卖出持仓→换入新标的)
            # 激进阵容豁免大盘冻结(s24 回测证明弱市全程冻结→空仓→收益≈0)
            if market_frozen and o.strategy_id not in rotate_sids \
                    and not o_rlx.get("market_freeze_exempt"):
                log.info("大盘冻结删新开单 %s %s", o.strategy_id, o.code)
                continue
            # 规则4:个股流动性(ETF 豁免)
            if not _is_etf(o.code):
                if ctx.avg_amount(o.code, 20) < min_amt:
                    log.info("流动性不足删单 %s %s", o.strategy_id, o.code)
                    continue
            # 规则6:综合敞口 = 消息面 × 回撤分层降险(均只降不升;激进阵容豁免,不降仓)
            if o_rlx.get("exposure_exempt"):
                dd_mult, mult = 1.0, 1.0
            else:
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
