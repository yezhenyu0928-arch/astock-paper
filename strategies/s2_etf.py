# -*- coding: utf-8 -*-
"""S2 ETF 动量轮动(SPEC 模块3)。每周最后交易日调仓,持有动量最强的1只ETF;
绝对动量为负则切换国债ETF避险。策略不做止损/仓位上限(risk.py 统一管)。"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s2")


class S2EtfMomentum(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_week(date):
            return []                                   # 仅每周最后交易日调仓

        windows = self.params.get("momentum_windows", [20, 60])
        safe = self.params.get("safe_asset", "sh511010")
        extra = (self.config.get("custom") or {}).get("s2_universe_extra", []) or []
        universe = list(dict.fromkeys(list(self.universe) + extra))

        # 计算各标的各窗口收益率
        rets = {}
        for code in universe:
            r = common.returns_over(ctx, code, windows)
            if all(r[w] is not None for w in windows):
                rets[code] = r
        if not rets:
            return []

        # 各窗口降序名次(收益越高名次越小=1),score=均名次,最小者为 best
        ranks = {c: 0.0 for c in rets}
        for w in windows:
            ordered = sorted(rets, key=lambda c: rets[c][w], reverse=True)
            for i, c in enumerate(ordered):
                ranks[c] += (i + 1)
        for c in ranks:
            ranks[c] /= len(windows)
        best = min(ranks, key=lambda c: ranks[c])

        # 绝对动量:best 的最短窗口收益<0 → 切避险(国债ETF)
        w0 = min(windows)
        if rets[best][w0] < 0:
            best = safe

        held = set(account.positions.keys())
        if held == {best}:
            return []                                   # 已只持有 best,无需操作

        orders = []
        for code in held:
            if code != best:
                orders.append(Order(strategy_id=self.strategy_id, code=code, side="sell",
                                    weight=0.0, reason=f"动量轮动:卖出{ctx.name(code)}", signal_date=date))
        if best not in held:
            r_show = rets.get(best, {}).get(w0)
            reason = (f"动量轮动:买入最强 {ctx.name(best)}(r{w0}={r_show:.1%})"
                      if r_show is not None else f"避险:买入{ctx.name(best)}(绝对动量为负)")
            orders.append(Order(strategy_id=self.strategy_id, code=best, side="buy",
                                weight=0.98, reason=reason, signal_date=date))
        return orders
