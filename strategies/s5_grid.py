# -*- coding: utf-8 -*-
"""S5 大盘择时网格(SPEC 模块3+产业逻辑增强)。只操作 sh510300,分5档建/减仓。
PE分位(沪深300近10年):<30% 每跌grid_step加1档;>70% 每涨grid_step减1档;中间纯网格±grid_step。
每日。
P2升级: macro_score() 调节网格密度——收缩期放宽步长防接飞刀，扩张期收窄步长防踏空。
产业逻辑增强: 市场面利好时略微放宽步长(更积极建仓),利空时收紧步长(更谨慎)。
⚠ 参考价用持仓均价近似"最近操作价";档数 k 由持仓市值反推;PE数据不足期间仅网格不择时。"""
import logging
from models import Order
from strategies.base import BaseStrategy
import macro

log = logging.getLogger("s5")
ETF = "sh510300"


class S5IndexGrid(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        code = self.universe[0] if self.universe else ETF
        pe_low = self.params.get("pe_low", 0.30)
        pe_high = self.params.get("pe_high", 0.70)
        step_base = self.params.get("grid_step", 0.02)
        tranches = self.params.get("tranches", 5)

        # ── 宏观调整网格步长 ──
        try:
            ms = macro.macro_score(date, conn=ctx.conn)
            mf = macro.macro_factor(date, conn=ctx.conn)
        except Exception:
            ms = 0.0
            mf = {}
        # 收缩期放宽步长(不易频繁触发加仓), 扩张期收窄(积极加仓)
        if ms < -0.3:
            step = step_base * 1.5        # 收缩: 放宽到3%
        elif ms > 0.3:
            step = step_base * 0.7        # 扩张: 收窄到1.4%
        else:
            step = step_base

        # ── 产业/市场面信号调整 ──
        news_adjust = 0.0
        try:
            import news_engine as ne
            market_score = 0.0
            r = ctx.conn.execute("SELECT score FROM news_signal WHERE signal_date=? AND scope='market'",
                                 (date,)).fetchone()
            if r:
                market_score = float(r[0])
            # 利好时略微放宽步长(更积极建仓), 利空时收紧(更谨慎)
            if market_score > 0.5:
                news_adjust = -0.003   # 放宽步长(更易触发加仓)
            elif market_score < -0.5:
                news_adjust = 0.005    # 收紧步长(更难触发加仓)
        except Exception:
            pass
        step = max(0.01, step + news_adjust)

        # 宏观分越高, 总档数可略多(更积极建仓)
        extra_tranches = max(0, int(ms * 2))  # ms=-1→-2(不减), ms=+1→+2
        eff_tranches = max(3, min(8, tranches + extra_tranches))
        tw = round(0.95 / eff_tranches, 4)

        bar = ctx.bar(code, date)
        if not bar:
            return []
        close = bar["close"]
        pos = account.positions.get(code)
        price_of = lambda c: (ctx.raw_close(c) or 0)
        total = account.total(price_of)
        held_val = (pos.shares * close) if pos else 0
        k = round(held_val / total / tw) if total > 0 else 0
        k = max(0, min(eff_tranches, k))
        ref = pos.avg_cost if pos and pos.avg_cost else close

        import fundamental as F
        pe_pct = F.index_pe_percentile("sh000300", date, conn=ctx.conn)

        # PE 择时区间决定加/减方向偏好
        if pe_pct is not None and pe_pct < pe_low:
            zone = "low"
        elif pe_pct is not None and pe_pct > pe_high:
            zone = "high"
        else:
            zone = "mid"

        drop = close <= ref * (1 - step)
        rise = close >= ref * (1 + step)

        m2_info = f" M2{mf.get('m2_yoy',0) or 0:.0f}%" if mf.get("m2_yoy") else ""

        # 加1档:低估区跌破网格,或中间区无仓起建/跌破网格
        if k < eff_tranches and (drop or (zone != "high" and k == 0)):
            if zone == "high" and k > 0:
                pass
            elif zone == "low" or zone == "mid" or k == 0:
                pename = f"PE分位{pe_pct:.0%}" if pe_pct is not None else "PE数据不足,纯网格"
                return [Order(self.strategy_id, code, "buy", tw,
                              f"网格加1档(第{k+1}/{eff_tranches}档,{zone}区,{pename},macro{ms:+.1f}{m2_info})", date)]
        # 减1档:高估区涨破网格,或中间区涨破网格
        if k > 0 and rise and (zone == "high" or zone == "mid"):
            pename = f"PE分位{pe_pct:.0%}" if pe_pct is not None else "纯网格"
            return [Order(self.strategy_id, code, "sell", tw,
                          f"网格减1档(第{k}/{eff_tranches}档,{zone}区,{pename},macro{ms:+.1f}{m2_info})", date)]
        return []
