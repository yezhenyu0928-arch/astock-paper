# -*- coding: utf-8 -*-
"""S7 赛道旗舰(2026-07 新增)。选中 1-2 个有政策/景气主线的行业赛道,集中持有、
耐心持仓,市场转弱/风险时果断避险。综合"政策+行业动量+市场regime+绝对动量风险"判断要不要操作。

与 S6 的区别:
- S6:momentum×0.8 + 低波×0.2,持 1 只,产业信号仅 0.2 权重(动量为主)。
- S7:momentum×0.5 + 产业信号(GLM)×0.5,持 1-2 只,大幅提高政策/行业信号权重(信息驱动);
      用移植的 compute_market_regime 综合牛熊判断仓位与避险,更贴近"看大势择赛道"的主动风格。

诚实约束:GLM 产业信号(sector_boost)只在实盘前瞻可得,回测无历史政策打分序列 → 回测退化为
"行业动量 + regime"骨架(可验证),GLM 政策倾斜是实盘 overlay(与现有 news 层哲学一致)。
风控(止损/仓位上限/熔断)仍由 risk.py 统一管,本策略只表达"选哪条赛道、多大仓位"的观点。"""
import logging

from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s7")


class S7TrackFlagship(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        rebalance = self.params.get("rebalance", "monthly")
        due = (ctx.is_last_trade_day_of_month(date) if rebalance == "monthly"
               else ctx.is_last_trade_day_of_week(date))
        if not due:
            return []                                   # 仅调仓日动作(耐心持仓,低换手)

        windows = self.params.get("momentum_windows", [60, 120])
        weights = self.params.get("weights", {"momentum": 0.5, "industry": 0.5})
        hold_n = int(self.params.get("hold_n", 2))
        safe = self.params.get("safe_asset", "sh511010")
        universe = [c for c in dict.fromkeys(self.universe) if c != safe]

        # ── 1. 市场 regime(移植自 K线机 marketRegime:多基准+MA+广度+风险) ──
        try:
            import macro
            reg = macro.compute_market_regime(date, conn=ctx.conn)
        except Exception:
            reg = {"regime": "震荡", "score": 50}
        regime = reg.get("regime", "震荡")
        rscore = reg.get("score", 50)
        # regime → 目标总仓位 + 是否强制避险(风险市果断转国债)
        if regime == "风险":
            target_weight, force_defensive = 0.0, True
        elif regime == "转弱":
            target_weight, force_defensive = 0.50, False
        elif regime == "强势":
            target_weight, force_defensive = 0.98, False
        else:                                           # 震荡
            target_weight, force_defensive = 0.80, False

        # ── 2. GLM 产业/政策信号(实盘前瞻;回测为空 → 退化为动量骨架) ──
        sector_boosts = {}
        try:
            import news_engine as ne
            sector_boosts = ne.get_all_sector_boosts(date, conn=ctx.conn)
        except Exception as e:
            log.debug("产业信号获取失败: %s", e)

        # ── 3. 行业动量(数据不足的新上市ETF自然被跳过) ──
        rets = {}
        for code in universe:
            r = common.returns_over(ctx, code, windows)
            if all(r[w] is not None for w in windows):
                rets[code] = r
        if not rets:
            return []
        n = len(rets)

        # 动量:各窗口降序名次均值 → 归一 0..1(越小越强)
        mom_rank = {c: 0.0 for c in rets}
        for w in windows:
            ordered = sorted(rets, key=lambda c: rets[c][w], reverse=True)
            for i, c in enumerate(ordered):
                mom_rank[c] += (i + 1)
        mom_norm = {c: (mom_rank[c] / len(windows)) / n for c in rets}
        # 产业信号 → 归一 0..1(越小越强):boost +2→0(最利好), 0→0.5, -2→1(最利空)
        ind_norm = {c: max(0.0, min(1.0, 1 - (sector_boosts.get(c, 0) + 2) / 4.0)) for c in rets}

        wm = weights.get("momentum", 0.5)
        wi = weights.get("industry", 0.5)
        comp = {c: wm * mom_norm[c] + wi * ind_norm[c] for c in rets}   # 越小越优
        ranked = sorted(rets, key=lambda c: comp[c])

        # ── 4. 绝对动量过滤:不追跌,短窗收益<0 的赛道剔除(顺势不逆势) ──
        w0 = min(windows)
        picks = [c for c in ranked if rets[c][w0] > 0][:hold_n]

        # ── 5. 目标持仓 ──
        if force_defensive or not picks:
            # 风险市 → 全仓国债避险;非风险但无正动量赛道 → 按目标仓位配国债
            targets = {safe: (0.98 if regime == "风险" else max(target_weight, 0.5))}
            reason_defensive = True
        else:
            per_w = round(target_weight / len(picks), 6)
            targets = {c: per_w for c in picks}
            reason_defensive = False

        held = set(account.positions.keys())
        if set(targets.keys()) == held and not reason_defensive:
            return []                                   # 已持有目标赛道,不折腾

        orders = []
        # 卖出:持仓中不在目标里的
        for code in held:
            if code not in targets:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                     f"赛道轮动:换出{ctx.name(code)}(市场{regime}/分{rscore})", date))
        # 买入:目标里尚未持有的
        for code, w in targets.items():
            if code in held:
                continue
            if reason_defensive:
                reason = (f"避险:市场 regime={regime}(分{rscore}),全仓{ctx.name(code)}(国债)等待,不逆势")
            else:
                b = sector_boosts.get(code, 0)
                binfo = f"·政策/行业信号{b:+.1f}" if b else ""
                rtxt = " ".join(f"r{wn}={rets[code][wn]:+.1%}" for wn in windows)
                reason = (f"赛道旗舰:买入{ctx.name(code)}({rtxt}{binfo},{n}选综合前{hold_n},"
                          f"市场{regime}/分{rscore},目标仓位{w*100:.0f}%)")
            orders.append(Order(self.strategy_id, code, "buy", w, reason, date))
        return orders
