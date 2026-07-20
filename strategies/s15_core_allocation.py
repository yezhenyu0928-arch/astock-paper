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
    """S15 v2 核心配置: 红利底仓 + 质量成长, 双袖并集等权。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []

        # 红利底仓袖: 高股息 + 动量(防御+趋势)
        div_params = {
            "min_dividend_yield": 0.04, "dividend_years": 3, "roe_years": 3, "roe_min": 0.08,
            "hold_n": 5, "max_per_industry": 2, "low_vol_pct": 0.60,
            "momentum_window": 252, "momentum_skip": 21, "momentum_min": 0.0,
            "regime_downsize": True, "regime_good": 1.0, "regime_mid": 0.92, "regime_bad": 0.72,
            "weights": {"dividend": 0.32, "low_vol": 0.08, "roe": 0.13, "valuation": 0.10, "news": 0.05, "momentum": 0.32},
        }
        # 质量成长袖: 放宽股息门槛, 重 ROE质量 + 估值 + 动量(纳入高质量票)
        grw_params = {
            "min_dividend_yield": 0.015, "dividend_years": 2, "roe_years": 3, "roe_min": 0.10,
            "hold_n": 5, "max_per_industry": 2, "low_vol_pct": 0.60,
            "momentum_window": 252, "momentum_skip": 21, "momentum_min": 0.0,
            "regime_downsize": True, "regime_good": 1.0, "regime_mid": 0.92, "regime_bad": 0.72,
            "weights": {"dividend": 0.07, "low_vol": 0.06, "roe": 0.29, "valuation": 0.20, "news": 0.10, "momentum": 0.28},
        }
        sel_d = mf_core.select(ctx, date, account, div_params, self.strategy_id, self.config)
        sel_g = mf_core.select(ctx, date, account, grw_params, self.strategy_id, self.config)

        target = list(dict.fromkeys(sel_d["target"] + sel_g["target"]))[:10]
        if not target:
            forced = news_guard.guard_holdings(date, list(account.positions.keys()), ctx.conn, self.config)
            return [Order(self.strategy_id, code, "sell", 0.0,
                          f"S15核心:{ctx.name(code)}新闻黑天鹅,清仓", date)
                    for code in account.positions.keys() if code in forced] + \
                   [Order(self.strategy_id, code, "sell", 0.0,
                          f"S15核心:{ctx.name(code)}无候选,清仓", date)
                    for code in account.positions.keys() if code not in forced]

        meta = {**sel_d["meta"], **sel_g["meta"]}
        full_rank = {**sel_d["full_rank"], **sel_g["full_rank"]}
        ind_map = {**sel_d["ind_map"], **sel_g["ind_map"]}
        merged = {
            "target": target,
            "weight_per": common.target_weight(len(target)),
            "meta": meta, "cand_codes": set(), "keep_codes": set(),
            "full_rank": full_rank, "ind_map": ind_map, "eff": len(target),
        }
        return mf_core.build_orders(ctx, date, account, merged, div_params,
                                    self.strategy_id, self.config, stop_pct=0.10)
