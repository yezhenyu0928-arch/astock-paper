# -*- coding: utf-8 -*-
"""S1 红利低波(SPEC 模块3)。池:中证800(近似全A,避幸存者偏差以membership快照,报告注明)。
过滤:股息率≥4% + 连续3年现金分红 + 250日波动率处于剩余池后30%(低波);
打分=股息率排名50%+低波排名50%,取前N等权。月末调仓。"""
import logging
from statistics import mean, pstdev
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s1")
POOL_INDEX = "sh000300"   # 沪深300(大盘红利票;可改中证800需相应扩充回填)


class S1DividendLowVol(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        min_dy = self.params.get("min_dividend_yield", 0.04)
        years = self.params.get("dividend_years", 3)
        low_vol_pct = self.params.get("low_vol_pct", 0.30)
        hold_n = self.params.get("hold_n", 10)
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        pool = ctx.members(POOL_INDEX, date)
        cand = []   # (code, div_yield, vol)
        for code in pool:
            if not ctx.is_tradable(code, date):
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("dividend_yield") or f["dividend_yield"] < min_dy:
                continue
            if ctx.dividend_years(code, years) < years:
                continue
            c = ctx.close(code, 251)
            if len(c) < 200:
                continue
            rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
            vol = pstdev(rets) if len(rets) > 1 else 9.9
            cand.append((code, f["dividend_yield"], vol))

        if not cand:
            return []
        # 低波后30%:按 vol 升序保留前 (1-0.3)? SPEC"位于剩余池后30%"=波动率最低的30%
        cand.sort(key=lambda x: x[2])
        keep = cand[:max(eff, int(len(cand) * low_vol_pct))]
        # 打分:股息率降序名次 + 低波(vol升序)名次,各50%
        by_dy = sorted(keep, key=lambda x: x[1], reverse=True)
        dy_rank = {c[0]: i for i, c in enumerate(by_dy)}
        by_vol = sorted(keep, key=lambda x: x[2])
        vol_rank = {c[0]: i for i, c in enumerate(by_vol)}
        scored = sorted(keep, key=lambda x: 0.5 * dy_rank[x[0]] + 0.5 * vol_rank[x[0]])
        target = [c[0] for c in scored[:eff]]

        held = set(account.positions.keys())
        orders = []
        for code in held:
            if code not in target:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                    f"掉出红利低波前{eff},卖出{ctx.name(code)}", date))
        for code in target:
            if code not in held:
                dy = dict((c[0], c[1]) for c in keep)[code]
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"红利低波:买入{ctx.name(code)}(股息率{dy:.1%})", date))
        return orders
