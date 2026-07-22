# -*- coding: utf-8 -*-
"""S14 红利价值轮动(@v2 重建)。

原 v1 逻辑(超跌+企稳+放量反转)在 42 只大蓝筹宇宙里几乎筛不出标的 -> 全程空仓。
根因: 该数据集仅 42 只有基本面数据且全为大蓝筹, 反转因子风格错配。

v2 重建在已验证的红利质量多因子底座(mf_core)之上, 叠加"深度价值"倾斜
(偏低 PE/PB + 股息率优先), 行业中性 + 新闻守卫 + 跟踪止损控回撤。
与 S13(成长质量) 的差异: S13 追成长质量, S14 捡便宜红利价值, 风格互补。
"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import mf_core

log = logging.getLogger("s14")


class S14ValueReversalRotation(BaseStrategy):
    """S14 v2 红利价值: 红利质量底座 + 深度价值(低PE/PB)倾斜。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []

        params = {
            "min_dividend_yield": 0.03,    # 略低于 s1(4%), 扩大价值票来源
            "dividend_years": 3,
            "roe_years": 3,
            "roe_min": 0.08,
            "hold_n": 8,
            "max_per_industry": 3,
            "low_vol_pct": 0.55,
            "value_tilt": True,            # 偏低 PE/PB 排名加分(深度价值)
            "momentum_window": 252,
            "momentum_skip": 21,
            "momentum_min": 0.0,           # 要求上行趋势(控回撤, 拉回≤5%)
            "regime_downsize": True,       # 宏观 risk-off 降仓(松化: weak市仍留0.75仓冲收益)
            "regime_good": 1.0, "regime_mid": 1.0, "regime_bad": 0.75,
            "weights": {"dividend": 0.18, "low_vol": 0.10, "roe": 0.16,
                        "valuation": 0.10, "news": 0.10, "industry": 0.08,
                        "value": 0.08, "momentum": 0.35},
        }
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            # 无候选: 清仓现有持仓(避免裸多)
            orders = []
            forced = __import__("strategies.news_guard", fromlist=["guard_holdings"]).guard_holdings(
                date, list(account.positions.keys()), ctx.conn, self.config)
            for code in account.positions.keys():
                if code in forced:
                    orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                        f"S14价值:{ctx.name(code)}新闻黑天鹅,清仓", date))
            return orders
        return mf_core.build_orders(ctx, date, account, sel, params,
                                    self.strategy_id, self.config, stop_pct=0.12)
