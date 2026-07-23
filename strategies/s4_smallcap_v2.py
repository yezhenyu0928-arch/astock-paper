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
        if not mf_core.should_rebalance(date, self.params):
            return mf_core.risk_orders(date, ctx, account, self.params, self.strategy_id, self.config)

        # 调优参数统一收口到 registry(params 字段, 与 mf_core 对齐); 不再硬编码, 避免与 registry 双源漂移。
        # 历史基线(s4/C, 本地主回测 2022-2026: 年化6.3%/回撤4.5%/Calmar1.40)对应 registry 默认 params。
        params = dict(self.params)
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
                                    self.strategy_id, self.config,
                                    stop_pct=params.get("stop_pct", 0.14))
