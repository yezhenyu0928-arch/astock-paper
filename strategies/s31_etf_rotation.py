# -*- coding: utf-8 -*-
"""S31 跨资产ETF双动量轮动(升级版)

相对 s2_etf 的三个升级:
1. 全负动量时不空仓,切换到避险资产(国债ETF 511010)吃票息 —— 修复 s2 空仓期零收益
2. 持 hold_n 只(默认2)分散,降低单标的回撤
3. 跨资产池: A股宽基/纳指/黄金/红利低波/行业,资产相关性低,轮动空间大

规则: 每周(或月)最后交易日,计算各ETF复权价动量(多窗口均值),
持有动量最强且>0 的前 hold_n 只等权;不足则剩余仓位买避险资产。
"""
import logging
from models import Order
from strategies.base import BaseStrategy

log = logging.getLogger("s31")


class S31EtfRotation(BaseStrategy):
    """跨资产ETF双动量轮动: 动量Top-N + 避险资产兜底"""

    def _rebalance_today(self, date):
        from trade_calendar import last_trade_day_of_week, last_trade_day_of_month
        freq = self.params.get("rebalance", "weekly")
        if freq == "monthly":
            return last_trade_day_of_month(date)
        return last_trade_day_of_week(date)

    def _adj_close_series(self, ctx, code, date, n):
        rows = ctx.conn.execute(
            "SELECT close, adj_factor FROM daily_bar WHERE code=? AND trade_date<=? "
            "ORDER BY trade_date DESC LIMIT ?", (code, str(date), n)).fetchall()
        return [float(r[0]) * float(r[1] or 1.0) for r in rows]

    def generate_orders(self, date, ctx, account):
        if not self._rebalance_today(date):
            return []

        params = dict(self.params)
        universe = params.get("universe") or list(self.universe or [])
        windows = params.get("momentum_windows", [20, 60])
        safe = params.get("safe_asset", "sh511010")
        hold_n = int(params.get("hold_n", 2))
        need = max(windows) + 1

        # 风险资产池 = universe 去掉避险资产
        risk_pool = [c for c in universe if c != safe]

        scores = {}
        for code in risk_pool:
            px = self._adj_close_series(ctx, code, date, need + 5)
            if len(px) < need:
                continue
            s = 0.0
            for w in windows:
                s += px[0] / px[w] - 1.0
            scores[code] = s / len(windows)

        ranked = sorted(scores, key=scores.get, reverse=True)
        top = [c for c in ranked if scores[c] > 0][:hold_n]

        # 目标组合: 正动量的风险资产等权,空缺仓位给避险资产
        target = {}
        slot_w = 0.98 / hold_n
        for c in top:
            target[c] = slot_w
        free_slots = hold_n - len(top)
        if free_slots > 0 and safe:
            target[safe] = target.get(safe, 0.0) + slot_w * free_slots

        held = set(account.positions.keys())
        orders = []
        for code in held:
            if code not in target:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                    f"ETF轮动:换出{ctx.name(code)}", date))
        for code, w in target.items():
            if code not in held:
                nm = ctx.name(code)
                why = "避险兜底" if code == safe and code not in top else f"动量{scores.get(code, 0):+.1%}"
                orders.append(Order(self.strategy_id, code, "buy", w,
                    f"ETF轮动:买入{nm}({why})", date))
        return orders
