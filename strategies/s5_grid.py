# -*- coding: utf-8 -*-
"""S5 大盘择时网格(SPEC 模块3)。只操作 sh510300,分5档建/减仓。
PE分位(沪深300近10年):<30% 每跌grid_step加1档;>70% 每涨grid_step减1档;中间纯网格±grid_step。
每日。⚠ 参考价用持仓均价近似"最近操作价";档数 k 由持仓市值反推;PE数据不足期间仅网格不择时。"""
import logging
from models import Order
from strategies.base import BaseStrategy

log = logging.getLogger("s5")
ETF = "sh510300"


class S5IndexGrid(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        code = self.universe[0] if self.universe else ETF
        pe_low = self.params.get("pe_low", 0.30)
        pe_high = self.params.get("pe_high", 0.70)
        step = self.params.get("grid_step", 0.02)
        tranches = self.params.get("tranches", 5)
        tw = round(0.95 / tranches, 4)             # 每档权重(留缓冲)

        bar = ctx.bar(code, date)
        if not bar:
            return []
        close = bar["close"]
        pos = account.positions.get(code)
        price_of = lambda c: (ctx.raw_close(c) or 0)
        total = account.total(price_of)
        held_val = (pos.shares * close) if pos else 0
        k = round(held_val / total / tw) if total > 0 else 0
        k = max(0, min(tranches, k))
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

        # 加1档:低估区跌破网格,或中间区无仓起建/跌破网格
        if k < tranches and (drop or (zone != "high" and k == 0)):
            if zone == "high" and k > 0:
                pass
            elif zone == "low" or zone == "mid" or k == 0:
                pename = f"PE分位{pe_pct:.0%}" if pe_pct is not None else "PE数据不足,纯网格"
                return [Order(self.strategy_id, code, "buy", tw,
                              f"网格加1档(第{k+1}/{tranches}档,{zone}区,{pename})", date)]
        # 减1档:高估区涨破网格,或中间区涨破网格
        if k > 0 and rise and (zone == "high" or zone == "mid"):
            pename = f"PE分位{pe_pct:.0%}" if pe_pct is not None else "纯网格"
            return [Order(self.strategy_id, code, "sell", tw,
                          f"网格减1档(第{k}/{tranches}档,{zone}区,{pename})", date)]
        return []
