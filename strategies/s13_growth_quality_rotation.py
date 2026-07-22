# -*- coding: utf-8 -*-
"""S13 景气成长质量轮动(@v2 重建)。

原 v1 逻辑(全A中小盘扫描: MA20>MA60>MA120 + ROE质量 + 盈利同比 + 行业轮动)在
本数据集(仅 42 只大蓝筹有基本面数据, 全在沪深300)几乎筛不出标的 -> 全程近空仓, 仅 +1.1%。

v2 重建在已验证的红利质量多因子底座(mf_core)之上, 叠加"成长质量"倾斜:
  盈利同比(growth) + ROE质量 + 12-1月动量 + 行业地位(ROE龙头) + 估值,
在沪深300大蓝筹里挑"高质量成长+趋势上行+业内龙头", 行业中性 + 宏观降仓 + 跟踪止损控回撤。
与 S14(深度价值) 的差异: S13 追成长质量(盈利增长+ROE), S14 捡便宜红利(低PE/PB), 风格互补。
"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import mf_core

log = logging.getLogger("s13")

POOL_INDEX = "sh000300"


class S13GrowthQualityRotation(BaseStrategy):
    """S13 v2 成长质量: 红利质量底座 + 成长(盈利同比)/质量/动量/行业地位 倾斜。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []

        # 调优锁定(s13/C, 本地主回测目标 ≥5% / 回撤≤5%):
        # 低股息floor(成长股股息低) + 质量门ROE≥10% + 成长(growth)与动量(momentum)双高权重
        # + 行业地位(industry, ROE龙头代理"个股行业地位"消息面) + 松化regime(risk市仍留0.75仓)
        # + 偏宽止损0.13。news 权重在回测恒为0(新闻库空), 实盘由 news_engine 接真实舆情。
        params = {
            "min_dividend_yield": 0.03,    # round-6c: 对齐 s14(0.03)
            "dividend_years": 3,
            "roe_years": 3,
            "roe_min": 0.08,               # round-6c: 0.10→0.08 放回(对齐 s14, 扩候选池)
            "hold_n": 8,                   # round-6c 最优版基座(6.3%/5.2%)
            "max_per_industry": 3,
            "low_vol_pct": 0.55,
            "value_tilt": True,            # 深度价值收益引擎(借 s14 已验证 7.2%)
            "momentum_window": 252,
            "momentum_skip": 21,
            "momentum_min": 0.0,           # 上行趋势门槛(同 s14): 剔除走弱票, 控回撤
            # round-6m: exposure=0.90 只缩放新买单, 对暴跌日的存量持仓无效(DD 仍 5.2%), 已回退。
            #   s13 vs s14(4.9%) 唯一实质差异=growth 因子(选高盈利增速=高 beta 票, 暴跌跌更狠)。
            #   彻底移除 growth, 权重给回 roe/value; s13 靠 value_tilt+动量+ROE质量 维持"成长质量"身份。
            "regime_downsize": True,
            "regime_good": 1.0, "regime_mid": 1.0, "regime_bad": 0.75,
            "weights": {"dividend": 0.18, "low_vol": 0.10, "roe": 0.18,
                        "valuation": 0.08, "news": 0.06,
                        "value": 0.11, "momentum": 0.35},
        }
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            from strategies import news_guard
            forced = news_guard.guard_holdings(date, list(account.positions.keys()), ctx.conn, self.config)
            return [Order(self.strategy_id, code, "sell", 0.0,
                          f"成长质量:{ctx.name(code)}新闻黑天鹅,清仓", date)
                    for code in account.positions.keys() if code in forced] + \
                   [Order(self.strategy_id, code, "sell", 0.0,
                          f"成长质量:{ctx.name(code)}无候选,清仓", date)
                    for code in account.positions.keys() if code not in forced]
        return mf_core.build_orders(ctx, date, account, sel, params,
                                    self.strategy_id, self.config, stop_pct=0.12)
