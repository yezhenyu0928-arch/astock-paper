# -*- coding: utf-8 -*-
"""S4 红利中小盘倾斜(@v2 重建)。

原 v2 的 Barra 7因子(小市值/动量/价值/流动性/BETA/盈利yield/质量)在 42 只大蓝筹宇宙里
因子方向相互打架 -> 选股无效, 主回测 -6.3%、胜率仅 1.6%。

v2 重建在已验证的红利质量多因子底座(mf_core)之上, 叠加 cap_tilt(偏小市值排名加分),
在本数据集的有限大蓝筹里挑"相对偏小盘的高股息质量票", 行业中性 + 宏观降仓 + 跟踪止损。
展示名: "红利中小盘倾斜(沪深300)"。
"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import mf_core
from strategies import news_guard

log = logging.getLogger("s4")
POOL_INDEX = "sh000300"


class S4SmallcapV2(BaseStrategy):
    """S4 v2 红利中小盘倾斜: 红利质量 + 偏小市值排名。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []

        params = {
            "min_dividend_yield": 0.035,
            "dividend_years": 3,
            "roe_years": 3,
            "roe_min": 0.08,
            "hold_n": 10,
            "max_per_industry": 3,
            "low_vol_pct": 0.35,
            "cap_tilt": True,             # 偏小市值排名加分
            "regime_downsize": True,
            "weights": {"dividend": 0.30, "low_vol": 0.15, "roe": 0.20,
                        "valuation": 0.15, "news": 0.10, "cap": 0.10},
        }
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            forced = news_guard.guard_holdings(date, list(account.positions.keys()), ctx.conn, self.config)
            return [Order(self.strategy_id, code, "sell", 0.0,
                          f"S4中小盘:{ctx.name(code)}新闻黑天鹅,清仓", date)
                    for code in account.positions.keys() if code in forced] + \
                   [Order(self.strategy_id, code, "sell", 0.0,
                          f"S4中小盘:{ctx.name(code)}无候选,清仓", date)
                    for code in account.positions.keys() if code not in forced]
        return mf_core.build_orders(ctx, date, account, sel, params,
                                    self.strategy_id, self.config, stop_pct=0.11)
