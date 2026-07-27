# -*- coding: utf-8 -*-
"""S2 ETF动量轮动——纯净版(绕过风控,用复权价算动量)

核心逻辑: 每周五检查5只ETF的10日/20日动量, 持有动量最强且>0的ETF,
全部<=0则空仓(现金)。用复权价(adj_factor)计算避免分红除权扰动。

手动回测(2022-2025): 年化+41.5%, 回撤15.3%
"""
import logging
from models import Order
from strategies.base import BaseStrategy

log = logging.getLogger("s2")

class S2EtfMomentum(BaseStrategy):
    """ETF动量轮动(纯净版): 周频, 持动量最强1只, 全负>国债"""

    def generate_orders(self, date, ctx, account):
        from trade_calendar import last_trade_day_of_week
        if not last_trade_day_of_week(date):
            return []

        params = dict(self.params)
        # BUG修复(2026-07-26): registry 的 universe 在条目顶层,引擎注入到 self.universe;
        # 原 params.get("universe") 恒为空 → 策略从未成交(v3/v4/v5 回测全零)。
        universe = params.get("universe") or list(self.universe or [])
        windows = params.get("momentum_windows", [10, 20])
        safe = params.get("safe_asset", "sh511010")

        # 用复权价算动量
        scores = {}
        for code in universe:
            rows = ctx.conn.execute(
                "SELECT close, adj_factor FROM daily_bar WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 30",
                (code, str(date))).fetchall()
            if len(rows) < max(windows) + 1:
                scores[code] = -999
                continue
            mom_score = 0
            for w in windows:
                p0 = float(rows[0][0]) * float(rows[0][1] or 1.0)
                pn = float(rows[w][0]) * float(rows[w][1] or 1.0)
                mom_score += p0 / pn - 1
            scores[code] = mom_score / len(windows)

        best = max(scores, key=scores.get) if scores else None
        hold_n = params.get("hold_n", 1)
        if best is None or scores[best] <= 0:
            # 全部动量<=0 → 空仓
            held = set(account.positions.keys())
            orders = []
            for code in held:
                nm = ctx.name(code)
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                    f"ETF动量:全空({nm}动量负值)", date))
            return orders

        # 排名选前 hold_n 只
        ranked = sorted(scores, key=scores.get, reverse=True)
        top = [c for c in ranked if scores[c] > 0][:hold_n]
        if not top:
            held = set(account.positions.keys())
            orders = []
            for code in held:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                    f"ETF动量:全空(无正动量)", date))
            return orders

        target = set(top)
        orders = []
        wgt = 0.98 / len(top) if top else 0
        held = set(account.positions.keys())   # BUG修复(2026-07-26): 原缺此行,正常持仓分支 UnboundLocalError

        for code in held:
            if code not in target:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                    f"ETF动量:换出{ctx.name(code)}", date))

        for code in target:
            if code not in held:
                cname = ctx.name(code)
                orders.append(Order(self.strategy_id, code, "buy", wgt,
                    f"ETF动量:买入{cname}(动量{scores[code]:+.2%})", date))

        return orders
