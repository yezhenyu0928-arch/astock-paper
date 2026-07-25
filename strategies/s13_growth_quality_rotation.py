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
        if not mf_core.should_rebalance(date, self.params):
            return mf_core.risk_orders(date, ctx, account, self.params, self.strategy_id, self.config)

        # 调优参数统一收口到 registry(params 字段, 与 mf_core 对齐); 不再硬编码, 避免与 registry 双源漂移。
        # 历史基线(s13/C, 本地主回测目标 ≥5% / 回撤≤5%)对应 registry 默认 params。
        params = dict(self.params)
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
                                    self.strategy_id, self.config,
                                    stop_pct=params.get("stop_pct", 0.12))
