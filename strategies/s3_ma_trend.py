# -*- coding: utf-8 -*-
"""S3 双均线趋势(SPEC 模块3+P2升级)。沪深300成分,MA20 上穿 MA60 且放量→买,跌破 MA20→卖。
每日调仓。策略不做止损/仓位上限(risk.py 统一管)。

P2升级: macro_score() 调节放量倍数阈值——
紧缩期提高门槛(vol_mult*1.5)防假突破, 扩张期降低门槛(vol_mult*0.7)积极入场。"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import common
import macro

log = logging.getLogger("s3")


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


class S3MaTrend(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        fast = self.params.get("fast", 20)
        slow = self.params.get("slow", 60)
        vol_mult_base = self.params.get("vol_mult", 1.5)
        max_hold = self.params.get("max_holdings", 10)

        # ── 宏观调节放量倍数 ──
        try:
            ms = macro.macro_score(date, conn=ctx.conn)
            mf = macro.macro_factor(date, conn=ctx.conn)
        except Exception:
            ms = 0.0
            mf = {}
        # 紧缩: 提高门槛防假突破; 扩张: 降低门槛积极入场
        if ms < -0.3:
            vol_mult = vol_mult_base * 1.5    # 更严
        elif ms > 0.3:
            vol_mult = vol_mult_base * 0.7    # 更松
        else:
            vol_mult = vol_mult_base

        eff = common.effective_hold_n(max_hold, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        pool = ctx.members("sh000300", date)
        held = set(account.positions.keys())
        orders, buy_cands = [], []

        for code in pool:
            c = ctx.close(code, slow + 2)
            if len(c) < slow + 1:
                continue
            ma_f_now, ma_f_prev = mean(c[-fast:]), mean(c[-fast - 1:-1])
            ma_s_now, ma_s_prev = mean(c[-slow:]), mean(c[-slow - 1:-1])
            close_now = c[-1]
            # 卖:跌破 MA20 → 清仓该票
            if code in held:
                if close_now < ma_f_now:
                    orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                        f"收盘{close_now:.2f}跌破MA{fast}({ma_f_now:.2f}),清仓{ctx.name(code)}", date))
                continue
            # 买:金叉 + 放量 + 可交易
            if not ctx.is_tradable(code, date):
                continue
            golden = (ma_f_prev <= ma_s_prev) and (ma_f_now > ma_s_now)
            if not golden:
                continue
            bar = ctx.bar(code, date)
            avgvol = ctx.avg_volume(code, fast)
            if not bar or avgvol <= 0 or bar["volume"] <= vol_mult * avgvol:
                continue
            strength = close_now / ma_s_now - 1
            volr = bar["volume"] / avgvol if avgvol else 0.0
            buy_cands.append((strength, code, volr))

        m2_info = f" M2{mf.get('m2_yoy',0) or 0:.0f}%" if mf.get("m2_yoy") else ""

        # 空位 = eff - 现有持仓(扣除本轮将卖出的)
        selling = {o.code for o in orders}
        slots = max(0, eff - (len(held) - len(selling)))
        buy_cands.sort(reverse=True)
        for strength, code, volr in buy_cands[:slots]:
            orders.append(Order(self.strategy_id, code, "buy", w,
                                f"MA{fast}上穿MA{slow},量比{volr:.1f}倍,买入{ctx.name(code)}"
                                f"(站上MA{slow}幅度{strength:+.1%},macro{ms:+.1f}{m2_info})", date))
        return orders
