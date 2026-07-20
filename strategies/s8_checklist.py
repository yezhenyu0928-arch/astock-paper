# -*- coding: utf-8 -*-
"""S8 个股行业轮动·红利质量倾斜(原价值质量清单)。

行业轮动抓风口 + 行业内高股息/质量倾斜选股 + 宽基趋势走弱清仓持现金。展示名"行业轮动·红利质量"。
"""
from models import Order  # noqa: F401
from strategies.base import BaseStrategy
from strategies import sector_stock_core


class S8LowDrawdown(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        params = {
            "rebalance": "monthly",
            "n_sectors": 4, "stocks_per_sector": 2, "hold_n": 8,
            "mom_windows": [60, 120],
            "trend_slow_ma": 60, "trend_fast_ma": 20,
            "use_macro": True, "macro_bad_score": 40, "use_news": True,
            "min_ind_members": 3, "stop_pct": 0.08,
            "sharp_drop_thr": 0.08,
            "tilt": "dividend",
            "weights": {"momentum": 0.22, "low_vol": 0.18, "roe": 0.22,
                        "valuation": 0.10, "dividend": 0.28, "size": 0.0},
        }
        return sector_stock_core.generate_core(self, date, ctx, account, params)
