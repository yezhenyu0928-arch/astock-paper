# -*- coding: utf-8 -*-
"""S13 个股行业轮动·成长质量倾斜(原景气成长质量轮动)。

行业动量抓风口 + 行业内高ROE质量/成长倾斜选股 + 宽基趋势走弱清仓持现金。展示名"行业轮动·成长质量"。
"""
from models import Order  # noqa: F401
from strategies.base import BaseStrategy
from strategies import sector_stock_core


class S13GrowthQualityRotation(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        params = {
            "rebalance": "monthly",
            "n_sectors": 4, "stocks_per_sector": 2, "hold_n": 8,
            "mom_windows": [60, 120],
            "trend_slow_ma": 60, "trend_fast_ma": 20,
            "use_macro": True, "macro_bad_score": 40, "use_news": True,
            "min_ind_members": 3, "stop_pct": 0.10,
            "sharp_drop_thr": 0.08,
            "tilt": "growth",
            "weights": {"momentum": 0.30, "low_vol": 0.10, "roe": 0.28,
                        "valuation": 0.10, "dividend": 0.05, "size": 0.12},
        }
        return sector_stock_core.generate_core(self, date, ctx, account, params)
