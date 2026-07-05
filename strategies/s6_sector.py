# -*- coding: utf-8 -*-
"""S6 行业ETF动量轮动(卡C+P2升级)。每月最后交易日持有综合评分最强的 1 只行业ETF;
评分 = 动量排名×0.8 + 60日低波排名×0.2(低波加分,名次越小越优);
最强者 20 日收益<0 → 切国债ETF避险。风控(止损/仓位)由 risk.py 统一管。

P2升级: 用 macro_score() 调节避险阈值 + 仓位大小。
- macro_score < -0.5（紧缩）→ 降仓位到50%，更快切国债
- macro_score > 0.5（扩张）→ 维持满仓，放宽避险阈值

设计意图——"政策/行业面"的可验证代理:产业政策利好(设备更新、化债、AI扶持、新能源补贴等)
最终都会体现为对应行业指数的涨幅动量。用行业ETF动量轮动自动跟随政策与景气主线,无需爬政策文本。"""
import logging
from statistics import pstdev

from models import Order
from strategies.base import BaseStrategy
from strategies import common
import macro

log = logging.getLogger("s6")


class S6SectorMomentum(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        rebalance = self.params.get("rebalance", "weekly")
        due = (ctx.is_last_trade_day_of_month(date) if rebalance == "monthly"
               else ctx.is_last_trade_day_of_week(date))
        if not due:
            return []                                   # 仅调仓日动作

        # ── 宏观评分 ──
        try:
            ms = macro.macro_score(date, conn=ctx.conn)
            mf = macro.macro_factor(date, conn=ctx.conn)
        except Exception:
            ms = 0.0
            mf = {}
        # 紧缩期降仓位, 扩张期满仓位
        if ms < -0.5:
            target_weight = 0.50  # 半仓防御
        elif ms > 0.5:
            target_weight = 0.98  # 满仓积极
        else:
            target_weight = 0.98

        windows = self.params.get("momentum_windows", [20, 60])
        vol_window = self.params.get("vol_window", 60)
        weights = self.params.get("weights", {"momentum": 0.8, "low_vol": 0.2})
        safe = self.params.get("safe_asset", "sh511010")
        universe = list(dict.fromkeys(self.universe))

        # 计算各标的动量(各窗口收益)与 60 日波动率;数据不足者跳过(新上市ETF自然被排除)
        rets, vols = {}, {}
        for code in universe:
            r = common.returns_over(ctx, code, windows)
            if not all(r[w] is not None for w in windows):
                continue
            c = ctx.close(code, vol_window + 1)
            if len(c) < vol_window:
                continue
            drs = [c[i] / c[i - 1] - 1 for i in range(1, len(c)) if c[i - 1]]
            if len(drs) < 2:
                continue
            rets[code] = r
            vols[code] = pstdev(drs)
        if not rets:
            return []

        # 动量:各窗口降序名次均值(名次越小=越强)
        mom_rank = {c: 0.0 for c in rets}
        for w in windows:
            ordered = sorted(rets, key=lambda c: rets[c][w], reverse=True)
            for i, c in enumerate(ordered):
                mom_rank[c] += (i + 1)
        for c in mom_rank:
            mom_rank[c] /= len(windows)
        # 低波:波动率升序名次(越低=越优)
        vol_ordered = sorted(vols, key=lambda c: vols[c])
        vol_rank = {c: i + 1 for i, c in enumerate(vol_ordered)}
        # 综合分(越小越优)= 动量×0.8 + 低波×0.2
        wl, wv = weights.get("momentum", 0.8), weights.get("low_vol", 0.2)
        score = {c: wl * mom_rank[c] + wv * vol_rank[c] for c in rets}
        best = min(score, key=lambda c: score[c])

        # 绝对动量:best 的最短窗口收益<0 → 切避险(国债ETF)
        # 紧缩期更敏感: 最短窗口收益 < 0 就切; 扩张期宽松: 收益 < -3% 才切
        w0 = min(windows)
        if ms < -0.5:
            safe_threshold = 0.0    # 紧缩: 零收益就切
        elif ms > 0.3:
            safe_threshold = -0.03  # 扩张: 跌3%才切
        else:
            safe_threshold = 0.0

        orig_best, switched = best, False
        if rets[best][w0] < safe_threshold:
            best, switched = safe, True

        held = set(account.positions.keys())
        if held == {best}:
            return []

        best_name = ctx.name(best)
        m2_info = f" M2{mf.get('m2_yoy',0) or 0:.0f}%" if mf.get("m2_yoy") else ""
        orders = []
        for code in held:
            if code != best:
                if switched:
                    reason = f"行业轮动:换出{ctx.name(code)}(最强{ctx.name(orig_best)}绝对动量转负,macro{ms:+.1f}{m2_info},避险)"
                else:
                    reason = f"行业轮动:换出{ctx.name(code)},轮入更强的{best_name}"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        if best not in held:
            if switched:
                r0 = rets[orig_best][w0]
                reason = (f"避险:买入{best_name}(最强{ctx.name(orig_best)} r{w0}={r0:+.1%}<{safe_threshold:+.0%},"
                          f"绝对动量为负,macro{ms:+.1f}{m2_info},全仓避险)")
            else:
                rtxt = " ".join(f"r{wn}={rets[best][wn]:+.1%}" for wn in windows)
                reason = (f"行业轮动:买入最强 {best_name}({rtxt}·60日波动池内第{vol_rank[best]}低,"
                          f"{len(rets)}只中综合第1,macro{ms:+.1f}{m2_info})")
            orders.append(Order(self.strategy_id, best, "buy", target_weight, reason, date))
        return orders
