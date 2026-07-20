# -*- coding: utf-8 -*-
"""S4 个股行业轮动·小盘倾斜。

原红利质量多因子在 42 只大蓝筹里因子互斥 -> 选股失效。现改为: 申万一级行业动量选
最强行业(抓风口) -> 行业内偏小市值倾斜选股 -> 宽基趋势走弱清仓持现金。展示名"行业轮动·小盘倾斜"。
"""
from models import Order  # noqa: F401
from strategies.base import BaseStrategy
from strategies import sector_stock_core


class S4SmallcapV2(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        params = {
            "rebalance": "monthly",
            "n_sectors": 4, "stocks_per_sector": 2, "hold_n": 8,
            "mom_windows": [60, 120],
            "trend_slow_ma": 60, "trend_fast_ma": 20,
            "use_macro": True, "macro_bad_score": 40, "use_news": True,
            "min_ind_members": 3, "stop_pct": 0.09,
            "sharp_drop_thr": 0.08,
            "tilt": "smallcap",
            "weights": {"momentum": 0.30, "low_vol": 0.10, "roe": 0.10,
                        "valuation": 0.10, "dividend": 0.10, "size": 0.30},
        }
        return sector_stock_core.generate_core(self, date, ctx, account, params)
