# -*- coding: utf-8 -*-
"""S15 个股行业轮动·核心均衡配置(原核心配置)。

更宽行业覆盖(5个)的均衡轮动: 行业动量抓风口 + 行业内均衡(动量/质量/低波)选股
+ 宽基趋势走弱清仓持现金。作为组合"核心", 行业更分散、波动更稳。展示名"行业轮动·核心配置"。
"""
from models import Order  # noqa: F401
from strategies.base import BaseStrategy
from strategies import sector_stock_core


class S15CoreAllocation(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        params = {
            "rebalance": "monthly",
            "n_sectors": 5, "stocks_per_sector": 2, "hold_n": 10,
            "mom_windows": [60, 120],
            "trend_slow_ma": 60, "trend_fast_ma": 20,
            "use_macro": True, "macro_bad_score": 40, "use_news": True,
            "min_ind_members": 3, "stop_pct": 0.08,
            "sharp_drop_thr": 0.08,
            "tilt": "balanced",
            "weights": {"momentum": 0.30, "low_vol": 0.20, "roe": 0.20,
                        "valuation": 0.15, "dividend": 0.10, "size": 0.05},
        }
        return sector_stock_core.generate_core(self, date, ctx, account, params)
