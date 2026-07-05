# -*- coding: utf-8 -*-
"""S2 ETF 动量轮动(SPEC 模块3+P2升级)。每周最后交易日调仓,持有动量最强的1只ETF;
绝对动量为负则切换国债ETF避险。
P2升级: macro_score() 调节仓位大小——紧缩期降仓到60%, 扩张期满仓。
策略不做止损/仓位上限(risk.py 统一管)。"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import common
import macro

log = logging.getLogger("s2")


class S2EtfMomentum(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_week(date):
            return []                                   # 仅每周最后交易日调仓

        # ── 宏观评分 ──
        try:
            ms = macro.macro_score(date, conn=ctx.conn)
            mf = macro.macro_factor(date, conn=ctx.conn)
        except Exception:
            ms = 0.0
            mf = {}
        # 紧缩降仓, 扩张满仓
        if ms < -0.5:
            target_w = 0.60    # 紧缩半仓
        elif ms > 0.5:
            target_w = 0.98    # 扩张满仓
        else:
            target_w = 0.98

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
        orig_best = best                                # 仅供理由展示(卡H)
        switched_safe = False
        if rets[best][w0] < 0:
            best = safe
            switched_safe = True

        held = set(account.positions.keys())
        if held == {best}:
            return []                                   # 已只持有 best,无需操作

        best_name = ctx.name(best)
        m2_info = f" M2{mf.get('m2_yoy',0) or 0:.0f}%" if mf.get("m2_yoy") else ""
        orders = []
        for code in held:
            if code != best:
                if switched_safe:
                    reason = f"动量轮动:换出{ctx.name(code)}(最强{ctx.name(orig_best)}绝对动量转负,避险)"
                else:
                    reason = f"动量轮动:换出{ctx.name(code)},轮入更强的{best_name}"
                orders.append(Order(strategy_id=self.strategy_id, code=code, side="sell",
                                    weight=0.0, reason=reason, signal_date=date))
        if best not in held:
            if switched_safe:
                r0 = rets[orig_best][w0]
                reason = (f"避险:买入{best_name}(最强{ctx.name(orig_best)} r{w0}={r0:+.1%}<0,"
                          f"绝对动量为负,macro{ms:+.1f}{m2_info},全仓避险)")
            else:
                rtxt = " ".join(f"r{wn}={rets[best][wn]:+.1%}" for wn in windows)
                reason = f"动量轮动:买入最强 {best_name}({rtxt},{len(rets)}只中第1,macro{ms:+.1f}{m2_info})"
            orders.append(Order(strategy_id=self.strategy_id, code=best, side="buy",
                                weight=target_w, reason=reason, signal_date=date))
        return orders
