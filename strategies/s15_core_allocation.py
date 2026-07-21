# -*- coding: utf-8 -*-
"""S15 核心配置(@v2 重建)。

原 v1 逻辑(股息袖+成长袖)依赖 cont_div_years/earnings_yoy 等稀疏数据 -> 候选恒空 -> 0 成交。
根因: 42 只大蓝筹宇宙里股息率>=2%且连续分红达标的极少, 成长袖(盈利同比+趋势)亦难命中。

v2 重建在红利质量多因子底座(mf_core)之上, 构建两路袖:
  红利底仓袖: 高股息 + 低波(防御)
  质量成长袖: ROE质量 + 估值(成长质量, 放宽股息门槛以纳入高质量票)
两袖并集等权持有, 行业中性 + 新闻守卫 + 跟踪止损控回撤。
"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import common, mf_core
from strategies import news_guard

log = logging.getLogger("s15")


class S15CoreAllocation(BaseStrategy):
    """S15 v2 核心配置: 单通道"核心均衡"多因子(原双袖并集实现回归至 3.0%/6.6%,
    故回退为单通道 mf_core 均衡底座 —— 与 s4/s14 同配方, 仅权重更均衡,
    作为组合里的"核心仓": 高股息 + ROE质量 + 估值 + 动量 + 行业地位 并重, 不极端倾斜。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []

        # 调优锁定(s15/C, 单通道核心均衡): 锚定 s4/s14 已验证配方(regime_bad 0.75 / 动量0.35 /
        # 止损0.12 / low_vol_pct 0.55), 权重均衡化(股息0.18+质量0.20+动量0.30+估值0.10+news0.10+
        # industry0.08), 不极端倾斜。hold_n 10 比 s4/s14 的 8 更分散, 压回撤。
        params = {
            "min_dividend_yield": 0.03,
            "dividend_years": 3, "roe_years": 3, "roe_min": 0.08,
            "hold_n": 8, "max_per_industry": 3, "low_vol_pct": 0.55,
            "value_tilt": True,             # round-5 加价值倾斜(借 s14 已验证 4.9% 低回撤配方)
            "momentum_window": 252, "momentum_skip": 21, "momentum_min": 0.0,
            "regime_downsize": True,
            "regime_good": 1.0, "regime_mid": 0.88, "regime_bad": 0.68,
            # round-5 防御化: 仿 s14(价值倾斜+动量0.28/低波0.16/估值0.14), 压回撤至≤5%
            "weights": {"dividend": 0.18, "low_vol": 0.16, "roe": 0.20,
                        "valuation": 0.14, "news": 0.10, "industry": 0.08, "momentum": 0.28},
        }
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            forced = news_guard.guard_holdings(date, list(account.positions.keys()), ctx.conn, self.config)
            return [Order(self.strategy_id, code, "sell", 0.0,
                          f"S15核心:{ctx.name(code)}新闻黑天鹅,清仓", date)
                    for code in account.positions.keys() if code in forced] + \
                   [Order(self.strategy_id, code, "sell", 0.0,
                          f"S15核心:{ctx.name(code)}无候选,清仓", date)
                    for code in account.positions.keys() if code not in forced]
        return mf_core.build_orders(ctx, date, account, sel, params,
                                    self.strategy_id, self.config, stop_pct=0.10)
