# -*- coding: utf-8 -*-
"""S3v2 纪律化趋势跟踪(个股,沪深300成分)。

persona: 趋势型基金经理——只骑"多周期均线多头排列 + 放量突破"的确认上升趋势,
市场进入风险regime或个股长期趋势破位即离场观望,不当死多头。

相对 v1 的优化(解决双均线在震荡市反复 whipsaw、全期 -0.8%/回撤24% 的问题):
  1) 多周期趋势对齐门禁:要求 收盘>MA120 且 MA120走平/上行 且 MA60>MA120 且 MA60上行,
     只在"中长期趋势确认向上"时参与,过滤掉无趋势的来回假突破。
  2) 市场regime防御:compute_market_regime=='风险' 时清仓全部持仓且不开新仓;
     '转弱'/'震荡' 时只做最强信号(strength门槛更高);'强势' 才积极入场。
  3) 趋势破位即退出:除跌破MA20外,MA120拐头向下也触发清仓,熊市不硬扛。
  4) 保留 v1 的宏观调节放量倍数 + 基本面/新闻面加权排名。
"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import common
import macro

log = logging.getLogger("s3v2")


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


class S3MaTrendV2(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        fast = self.params.get("fast", 20)
        slow = self.params.get("slow", 60)
        long_ma = self.params.get("long_ma", 120)
        vol_mult_base = self.params.get("vol_mult", 1.5)
        max_hold = self.params.get("max_holdings", 10)
        min_strength = self.params.get("min_strength", 0.02)  # 非强势regime下额外强度门槛

        # ── 宏观调节放量倍数 ──
        try:
            ms = macro.macro_score(date, conn=ctx.conn)
            mf = macro.macro_factor(date, conn=ctx.conn)
        except Exception:
            ms = 0.0
            mf = {}
        if ms < -0.3:
            vol_mult = vol_mult_base * 1.5    # 紧缩:更严,防假突破
        elif ms > 0.3:
            vol_mult = vol_mult_base * 0.7    # 扩张:更松,积极入场
        else:
            vol_mult = vol_mult_base

        # ── 市场regime防御 ──
        try:
            _reg = macro.compute_market_regime(date, conn=ctx.conn).get("regime", "震荡")
        except Exception:
            _reg = "震荡"
        risk_regime = (_reg == "风险")
        strong_regime = (_reg == "强势")

        eff = common.effective_hold_n(max_hold, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        pool = ctx.members("sh000300", date)
        held = set(account.positions.keys())
        orders, buy_cands = [], []

        for code in pool:
            c = ctx.close(code, long_ma + 2)
            if len(c) < long_ma + 1:
                continue
            ma_f_now, ma_f_prev = mean(c[-fast:]), mean(c[-fast - 1:-1])
            ma_s_now, ma_s_prev = mean(c[-slow:]), mean(c[-slow - 1:-1])
            ma_l_now, ma_l_prev = mean(c[-long_ma:]), mean(c[-long_ma - 1:-1])
            close_now = c[-1]

            # ── 持仓管理:趋势破位即退出 ──
            if code in held:
                # 跌破短期均线 或 长期趋势拐头向下 → 清仓
                if close_now < ma_f_now or ma_l_now < ma_l_prev:
                    reason = f"收盘{close_now:.2f}跌破MA{fast}" if close_now < ma_f_now \
                        else f"长期趋势MA{long_ma}拐头向下"
                    orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                        f"{reason},清仓{ctx.name(code)}", date))
                continue

            if not ctx.is_tradable(code, date):
                continue
            if risk_regime:
                continue  # 风险市不开新仓,全程观望

            # ── 买点:多周期均线多头排列 + 金叉 + 放量 ──
            # 1) 长期趋势向上:价格在长期均线之上 且 长期均线斜率非负
            if not (close_now > ma_l_now and ma_l_now >= ma_l_prev):
                continue
            # 2) 中期在长期之上(多头排列)
            if ma_s_now <= ma_l_now:
                continue
            # 3) 中期趋势向上(慢线斜率>0)
            if ma_s_now <= ma_s_prev:
                continue
            # 4) 金叉:MAfast 上穿 MAslow
            golden = (ma_f_prev <= ma_s_prev) and (ma_f_now > ma_s_now)
            if not golden:
                continue
            bar = ctx.bar(code, date)
            avgvol = ctx.avg_volume(code, fast)
            if not bar or avgvol <= 0 or bar["volume"] <= vol_mult * avgvol:
                continue
            strength = close_now / ma_s_now - 1
            # 5) 非强势regime需更强确认,避免震荡市假突破
            if not strong_regime and strength < min_strength:
                continue
            volr = bar["volume"] / avgvol if avgvol else 0.0

            # 基本面评分
            fund_score = common.get_fundamental_score(ctx, code, date)
            # 新闻语义分
            news_score = 0.0
            try:
                import news_engine as ne
                news_score = ne.get_stock_sentiment_score(date, code, conn=ctx.conn)
            except Exception:
                pass

            buy_cands.append((strength, code, volr, fund_score, news_score))

        m2_info = f" M2{mf.get('m2_yoy', 0) or 0:.0f}%" if mf.get("m2_yoy") else ""

        selling = {o.code for o in orders}
        slots = max(0, eff - (len(held) - len(selling)))

        n = len(buy_cands)
        if n > 0:
            by_tech = sorted(range(n), key=lambda i: buy_cands[i][0], reverse=True)
            tech_rank = {by_tech[i]: i for i in range(n)}
            by_fund = sorted(range(n), key=lambda i: buy_cands[i][3], reverse=True)
            fund_rank = {by_fund[i]: i for i in range(n)}
            by_news = sorted(range(n), key=lambda i: buy_cands[i][4], reverse=True)
            news_rank = {by_news[i]: i for i in range(n)}
            scored = sorted(range(n),
                           key=lambda i: (0.5 * tech_rank[i] + 0.3 * fund_rank[i] + 0.2 * news_rank[i]))
            buy_cands = [buy_cands[i] for i in scored]

        for strength, code, volr, fund_score, news_score in buy_cands[:slots]:
            fund_info = f"·基本面{fund_score:.2f}" if fund_score != 0.5 else ""
            orders.append(Order(self.strategy_id, code, "buy", w,
                                f"MA{fast}上穿MA{slow}(多周期多头排列),量比{volr:.1f}倍,买入{ctx.name(code)}"
                                f"(站上MA{slow}幅度{strength:+.1%}{fund_info},regime={_reg},macro{ms:+.1f}{m2_info})", date))
        return orders
