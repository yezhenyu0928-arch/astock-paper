# -*- coding: utf-8 -*-
"""S2 ETF 动量轮动(SPEC 模块3+P2升级+产业逻辑增强)。每周最后交易日调仓,持有动量最强的1只ETF;
绝对动量为负则切换国债ETF避险。
P2升级: macro_score() 调节仓位大小——紧缩期降仓到60%, 扩张期满仓。
产业逻辑增强: 叠加产业信号,政策利好的ETF获得额外加分。
策略不做止损/仓位上限(risk.py 统一管)。"""
import logging
from statistics import pstdev
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

        # ── 获取产业信号 ──
        sector_boosts = {}
        try:
            import news_engine as ne
            sector_boosts = ne.get_all_sector_boosts(date, conn=ctx.conn)
        except Exception:
            pass

        # 计算各标的各窗口收益率
        rets = {}
        for code in universe:
            r = common.returns_over(ctx, code, windows)
            if all(r[w] is not None for w in windows):
                rets[code] = r
        if not rets:
            return []

        # 各窗口降序名次(收益越高名次越小=1),score=均名次,最小者为 best
        # 叠加产业信号:利好ETF排名提前
        ranks = {c: 0.0 for c in rets}
        for w in windows:
            ordered = sorted(rets, key=lambda c: rets[c][w], reverse=True)
            for i, c in enumerate(ordered):
                ranks[c] += (i + 1)
        for c in ranks:
            ranks[c] /= len(windows)
            # 产业加分:利好boost>0时排名提前(减小),利空时排名推后(增大)
            boost = sector_boosts.get(c, 0)
            if boost != 0:
                ranks[c] -= boost * 0.3  # 每+1分提前0.3名

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

        # 构建产业信号描述
        boost_info = ""
        if orig_best in sector_boosts and sector_boosts[orig_best] != 0:
            boost_info = f"·产业信号{sector_boosts[orig_best]:+.1f}"

        orders = []
        for code in held:
            if code != best:
                if switched_safe:
                    reason = f"动量轮动:换出{ctx.name(code)}(最强{ctx.name(orig_best)}绝对动量转负,避险)"
                else:
                    reason = f"动量轮动:换出{ctx.name(code)},轮入更强的{best_name}{boost_info}"
                orders.append(Order(strategy_id=self.strategy_id, code=code, side="sell",
                                    weight=0.0, reason=reason, signal_date=date))
        if best not in held:
            if switched_safe:
                r0 = rets[orig_best][w0]
                reason = (f"避险:买入{best_name}(最强{ctx.name(orig_best)} r{w0}={r0:+.1%}<0,"
                          f"绝对动量为负,macro{ms:+.1f}{m2_info},全仓避险)")
            else:
                rtxt = " ".join(f"r{wn}={rets[best][wn]:+.1%}" for wn in windows)
                reason = f"动量轮动:买入最强 {best_name}({rtxt},{len(rets)}只中第1,macro{ms:+.1f}{m2_info}{boost_info})"
            orders.append(Order(strategy_id=self.strategy_id, code=best, side="buy",
                                weight=target_w, reason=reason, signal_date=date))
        return orders


class S2EtfMomentumV2(BaseStrategy):
    """S2 v2(卡N/OPTIMIZE_V4.md,动量崩溃防护)。在 S2EtfMomentum(v1,以上,一字不改)逻辑上
    加两道闸,v1/v2 并行赛马、曲线互不干扰:
    - 趋势过滤:候选第一名须 close>MA200(200日均线,ETF后复权价);数据不足200日的标的视同
      不适用该过滤(即不通过),按 v1 既有的避险逻辑切国债 sh511010。
    - 波动目标:未触发避险时,所选风险ETF近20日年化波动>vol_cap 则目标仓位×vol_scale,
      与现有 macro 仓位调节相乘,最终仓位下限0.30。
    本类不注册 registry.yaml/config.yaml(留给验证批,按"验证完成才冻结"纪律)。"""

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
        ma_filter = self.params.get("ma_filter", 200)       # 卡N:趋势过滤MA窗口
        vol_cap = self.params.get("vol_cap", 0.25)           # 卡N:波动目标触发阈值(年化)
        vol_scale = self.params.get("vol_scale", 0.6)        # 卡N:超阈值后的仓位缩放系数
        extra = (self.config.get("custom") or {}).get("s2_universe_extra", []) or []
        universe = list(dict.fromkeys(list(self.universe) + extra))

        # ── 获取产业信号 ──
        sector_boosts = {}
        try:
            import news_engine as ne
            sector_boosts = ne.get_all_sector_boosts(date, conn=ctx.conn)
        except Exception:
            pass

        # 计算各标的各窗口收益率
        rets = {}
        for code in universe:
            r = common.returns_over(ctx, code, windows)
            if all(r[w] is not None for w in windows):
                rets[code] = r
        if not rets:
            return []

        # 各窗口降序名次(收益越高名次越小=1),score=均名次,最小者为 best
        # 叠加产业信号:利好ETF排名提前
        ranks = {c: 0.0 for c in rets}
        for w in windows:
            ordered = sorted(rets, key=lambda c: rets[c][w], reverse=True)
            for i, c in enumerate(ordered):
                ranks[c] += (i + 1)
        for c in ranks:
            ranks[c] /= len(windows)
            # 产业加分:利好boost>0时排名提前(减小),利空时排名推后(增大)
            boost = sector_boosts.get(c, 0)
            if boost != 0:
                ranks[c] -= boost * 0.3  # 每+1分提前0.3名

        best = min(ranks, key=lambda c: ranks[c])

        # 绝对动量(v1既有):best 的最短窗口收益<0 → 切避险(国债ETF)
        w0 = min(windows)
        orig_best = best                                # 仅供理由展示(卡H)
        switched_safe = False
        fail_tag = ""
        if rets[best][w0] < 0:
            switched_safe = True
            fail_tag = "绝对动量为负"
        else:
            # 趋势过滤(卡N新增):候选须 close>MA{ma_filter};数据不足则该标的不适用过滤,
            # 视同不通过 → 切避险。
            ma_closes = ctx.close(best, ma_filter)
            if len(ma_closes) < ma_filter:
                switched_safe = True
                fail_tag = f"MA{ma_filter}数据不足({len(ma_closes)}<{ma_filter})"
            else:
                ma_val = sum(ma_closes) / len(ma_closes)
                if not (ma_closes[-1] > ma_val):
                    switched_safe = True
                    fail_tag = f"跌破MA{ma_filter}({ma_closes[-1]:.3f}≤{ma_val:.3f})"

        if switched_safe:
            best = safe
        else:
            # 波动目标(卡N新增):所选风险ETF近20日年化波动>vol_cap → 目标仓位×vol_scale,
            # 与 macro 仓位调节相乘,最终仓位下限0.30。数据不足20日不缩放(无法判定,保守不误伤)。
            vol_closes = ctx.close(best, 21)
            drs = [vol_closes[i] / vol_closes[i - 1] - 1
                   for i in range(1, len(vol_closes)) if vol_closes[i - 1]]
            if len(drs) >= 2:
                ann_vol = pstdev(drs) * (252 ** 0.5)
                if ann_vol > vol_cap:
                    target_w = max(0.30, target_w * vol_scale)

        held = set(account.positions.keys())
        if held == {best}:
            return []                                   # 已只持有 best,无需操作

        best_name = ctx.name(best)
        m2_info = f" M2{mf.get('m2_yoy',0) or 0:.0f}%" if mf.get("m2_yoy") else ""

        # 构建产业信号描述
        boost_info = ""
        if orig_best in sector_boosts and sector_boosts[orig_best] != 0:
            boost_info = f"·产业信号{sector_boosts[orig_best]:+.1f}"

        orders = []
        for code in held:
            if code != best:
                if switched_safe:
                    reason = f"动量轮动V2:换出{ctx.name(code)}(最强{ctx.name(orig_best)}{fail_tag},避险)"
                else:
                    reason = f"动量轮动V2:换出{ctx.name(code)},轮入更强的{best_name}{boost_info}"
                orders.append(Order(strategy_id=self.strategy_id, code=code, side="sell",
                                    weight=0.0, reason=reason, signal_date=date))
        if best not in held:
            if switched_safe:
                r0 = rets[orig_best][w0]
                reason = (f"避险:买入{best_name}(最强{ctx.name(orig_best)} r{w0}={r0:+.1%},"
                          f"{fail_tag},macro{ms:+.1f}{m2_info},全仓避险)")
            else:
                rtxt = " ".join(f"r{wn}={rets[best][wn]:+.1%}" for wn in windows)
                vol_info = f"·波动目标降仓至{target_w:.0%}" if target_w < 0.9 else ""
                reason = (f"动量轮动V2:买入最强 {best_name}({rtxt},{len(rets)}只中第1,"
                          f"macro{ms:+.1f}{m2_info}{boost_info}{vol_info})")
            orders.append(Order(strategy_id=self.strategy_id, code=best, side="buy",
                                weight=target_w, reason=reason, signal_date=date))
        return orders
