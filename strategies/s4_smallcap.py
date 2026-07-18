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
from strategies import news_guard

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
        w = round(w * news_guard.market_exposure(date, ctx, self.config), 6)  # 市场分调仓(跟踪大盘动态)

        # 真小盘宇宙:全A股按市值升序取最小一批(不再局限沪深300)。
        # 用 daily_bar 全量代码 + fundamental 市值截面,经可交易/上市满1年/流动性过滤。
        min_avg = self.params.get("min_avg_amount", 30_000_000)
        pool_size = self.params.get("pool_size", 800)
        try:
            all_codes = [r[0] for r in ctx.conn.execute(
                "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]
        except Exception:
            all_codes = ctx.members(POOL_INDEX, date)
        # 主板宇宙硬约束(手册):主板前缀/非ST/上市≥2年/总市值≥80亿/日均成交≥8000万/可交易
        all_codes = common.main_board_universe(ctx, all_codes, self.config, date)
        univ = []
        for code in all_codes:
            f = ctx.fundamental(code)
            if not f or not f.get("market_cap") or f["market_cap"] <= 0:
                continue
            univ.append((code, f["market_cap"]))     # 硬过滤已在 main_board_universe 完成,此处仅取市值排序
        univ.sort(key=lambda x: x[1])            # 市值升序
        small = [u[0] for u in univ[:pool_size]] # 取最小 pool_size 只 = 主板真小盘(已排除微盘)

        cand = []   # (code, mcap, pb, ret20, fund_score, news_score, turnover)
        for code in small:
            c = ctx.close(code, 260)
            f = ctx.fundamental(code)
            if not f or not f.get("pb") or f["pb"] <= 0:
                continue
            ret20 = c[-1] / c[-21] - 1 if len(c) >= 21 else 0
            fund_score = common.get_fundamental_score(ctx, code, date)
            news_score = 0.0
            try:
                import news_engine as ne
                news_score = ne.get_stock_sentiment_score(date, code, conn=ctx.conn)
            except Exception:
                pass
            # 资金面代理:20日成交额 / 总市值 = 换手率口径,越高代表资金关注度越强
            turn = (ctx.avg_amount(code, 20) / f["market_cap"]) if f["market_cap"] else 0.0
            cand.append((code, f["market_cap"], f["pb"], ret20, fund_score, news_score, turn))
        if len(cand) < eff:
            return []

        cand.sort(key=lambda x: x[1])            # 市值升序
        cand = cand[:pool_size]                   # 最小 pool_size
        n = len(cand)
        # —— 新闻/公告/动态守卫(全量接入) ——
        _cc = [c[0] for c in cand]
        try:
            import factors as _fac
            _ind = _fac.get_industry(ctx.conn, _cc)
        except Exception:
            _ind = {}
        _ban_n, _ = news_guard.guard_candidates(date, _cc, ctx.conn, self.config)
        _ban_i = news_guard.guard_industry(date, _cc, ctx.conn, self.config, _ind)
        _ban_s = {c for c in _cc if news_guard.structural_ban(date, c, ctx)[0]}
        _banned = _ban_n | _ban_i | _ban_s
        if _banned:
            cand = [c for c in cand if c[0] not in _banned]
        if len(cand) < eff:
            return []
        size_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[1]))}
        pb_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[2]))}
        mom_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[3], reverse=True))}
        fund_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[4], reverse=True))}
        news_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[5], reverse=True))}
        # 资金面:换手率(成交额/市值)降序排名,越高=资金越活跃→名次越小越优
        turn_rank = {c[0]: i / n for i, c in enumerate(sorted(cand, key=lambda x: x[6], reverse=True))}
        w_turn = weights.get("turnover", 0.10)
        scored = sorted(cand, key=lambda x: (weights["size"] * size_rank[x[0]]
                                             + weights["pb"] * pb_rank[x[0]]
                                             + weights["momentum_20d"] * mom_rank[x[0]]
                                             + weights.get("fundamental", 0.1) * fund_rank[x[0]]
                                             + weights.get("news", 0.1) * news_rank[x[0]]
                                             + w_turn * turn_rank[x[0]]))
        target = [c[0] for c in scored[:eff]]

        # —— 仅供理由展示(卡H):只读数值/排名,不参与选股 ——
        meta = {c[0]: (c[1], c[2], c[3], c[4]) for c in cand}   # code -> (市值, PB, 20日动量, 基本面)
        size_pos = {c[0]: i + 1 for i, c in enumerate(sorted(cand, key=lambda x: x[1]))}  # 1=最小市值
        full_rank = {c[0]: i + 1 for i, c in enumerate(scored)}  # 综合分名次(1=最优)

        held = set(account.positions.keys())
        orders = []
        forced = news_guard.guard_holdings(date, held, ctx.conn, self.config)
        for code in held:
            if code in target and code not in forced:
                continue
            nm = ctx.name(code)
            if code in forced:
                reason = f"小市值:{nm}新闻黑天鹅,同步清仓"
            elif code in full_rank:
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
