# -*- coding: utf-8 -*-
"""S4 小市值多因子(SPEC 模块3+产业逻辑增强)。池:沪深300(演示档)。
过滤:可交易+流动性+上市满1年 → 按市值升序取前400;
打分=市值升序%×0.4 + PB升序%×0.25 + 20日收益降序%×0.15 + 基本面×0.1 + 新闻面×0.1,
取综合分最小(最优)前N等权。月末调仓。

产业逻辑增强: 新闻面利好的个股获得额外加分。"""
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
        weights = self.params.get("weights", {"size": 0.4, "pb": 0.25, "momentum_20d": 0.15,
                                               "fundamental": 0.1, "news": 0.1})
        hold_n = self.params.get("hold_n", 20)
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        cand = []   # (code, mcap, pb, ret20, fund_score, news_score)
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
            fund_score = common.get_fundamental_score(ctx, code, date)
            news_score = 0.0
            try:
                import news_engine as ne
                news_score = ne.get_stock_sentiment_score(date, code, conn=ctx.conn)
            except Exception:
                pass
            cand.append((code, f["market_cap"], f["pb"], ret20, fund_score, news_score))
        if len(cand) < eff:
            return []

        cand.sort(key=lambda x: x[1])            # 市值升序
        cand = cand[:pool_size]                   # 最小 400
        n = len(cand)
        size_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[1]))}
        pb_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[2]))}
        mom_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[3], reverse=True))}
        fund_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[4], reverse=True))}
        news_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[5], reverse=True))}
        scored = sorted(cand, key=lambda x: (weights["size"] * size_rank[x[0]]
                                             + weights["pb"] * pb_rank[x[0]]
                                             + weights["momentum_20d"] * mom_rank[x[0]]
                                             + weights.get("fundamental", 0.1) * fund_rank[x[0]]
                                             + weights.get("news", 0.1) * news_rank[x[0]]))
        target = [c[0] for c in scored[:eff]]

        # —— 仅供理由展示(卡H):只读数值/排名,不参与选股 ——
        meta = {c[0]: (c[1], c[2], c[3], c[4]) for c in cand}   # code -> (市值, PB, 20日动量, 基本面)
        size_pos = {c[0]: i + 1 for i, c in enumerate(sorted(cand, key=lambda x: x[1]))}  # 1=最小市值
        full_rank = {c[0]: i + 1 for i, c in enumerate(scored)}  # 综合分名次(1=最优)

        held = set(account.positions.keys())
        orders = []
        for code in held:
            if code not in target:
                nm = ctx.name(code)
                if code in full_rank:
                    reason = f"小市值调仓:{nm}综合排名第{full_rank[code]}/{n}掉出前{eff},卖出"
                else:
                    reason = f"小市值:{nm}掉出候选池(市值/流动性/上市满1年过滤),卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                mcap, pb, ret20, fund = meta[code]
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"小市值价值:买入{ctx.name(code)}(市值{mcap/1e8:.0f}亿第{size_pos[code]}小"
                                    f"·PB{pb:.2f}·20日{ret20:+.1%}·基本面{fund:.2f}·综合分第{full_rank[code]}/{n})", date))
        return orders
