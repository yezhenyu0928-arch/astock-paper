# -*- coding: utf-8 -*-
"""S4 小市值多因子(SPEC 模块3)。池:中证1000(近似小市值全A,membership快照,报告注明幸存者偏差)。
过滤:可交易+流动性+上市满1年 → 按市值升序取前400;
打分=市值升序%×0.5 + PB升序%×0.3 + 20日收益降序%×0.2,取综合分最小(最优)前N等权。月末调仓。"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s4")
# 本 build 用沪深300(数据可行性:东财历史被代理挡,baostock回填1000只需160min)。
# 沪深300内取"相对最小市值",是小市值因子的演示档。要升级真·小盘:改回 "sh000852"(中证1000)并重跑 backfill。
POOL_INDEX = "sh000300"   # 沪深300(演示档;真小盘改 sh000852)


class S4SmallCapFactor(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        pool_size = self.params.get("pool_size", 400)
        weights = self.params.get("weights", {"size": 0.5, "pb": 0.3, "momentum_20d": 0.2})
        hold_n = self.params.get("hold_n", 20)
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        cand = []   # (code, mcap, pb, ret20)
        for code in ctx.members(POOL_INDEX, date):
            if not ctx.is_tradable(code, date):
                continue
            c = ctx.close(code, 260)
            if len(c) < 250:                     # 上市满1年近似
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("market_cap") or not f.get("pb") or f["pb"] <= 0:
                continue
            ret20 = c[-1] / c[-21] - 1 if len(c) >= 21 else 0
            cand.append((code, f["market_cap"], f["pb"], ret20))
        if len(cand) < eff:
            return []

        cand.sort(key=lambda x: x[1])            # 市值升序
        cand = cand[:pool_size]                   # 最小 400
        n = len(cand)
        size_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[1]))}
        pb_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[2]))}
        mom_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[3], reverse=True))}
        scored = sorted(cand, key=lambda x: (weights["size"] * size_rank[x[0]]
                                             + weights["pb"] * pb_rank[x[0]]
                                             + weights["momentum_20d"] * mom_rank[x[0]]))
        target = [c[0] for c in scored[:eff]]

        held = set(account.positions.keys())
        orders = []
        for code in held:
            if code not in target:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                    f"掉出小市值前{eff},卖出{ctx.name(code)}", date))
        for code in target:
            if code not in held:
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"小市值多因子:买入{ctx.name(code)}", date))
        return orders
