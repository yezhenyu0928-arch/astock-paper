# -*- coding: utf-8 -*-
"""参考文章因子重建的 4 个个股策略(诚实冲、报实数)。

均复用已验证的红利质量多因子底座 mf_core.select(), 仅在 weights/pool/集中度上
体现 4 类参考因子(国信/开源金工/中信建投), 不再堆 @v1~v5 互相雷同的版本。

参考来源 → 本文件策略:
  · 国信《稳健精选》: 低估值/低波动/低换手/高股息 → 稳健复合因子 + 动量/成长/资金结构增强
        → RefSteadyQuality (s20)  hold_n=8 集中
  · 国信《小盘精选》: 总市值最小1/3 为池 + 复合选股因子(质量+动量+低波)前N等权, 月末
        → RefSmallcapCompound (s21)  cap=small, hold_n=10
  · 国信《超预期精选》: 研报标题超预期 + 分析师上调净利润, 季频, 基本面+技术面共振
        → RefEarningsSurprise (s22)  季频; 实盘接 news_engine 超预期信号, 回测降级为成长+动量代理
  · 开源金工《行业动量》/中信建投《联合动量》: 个股+行业联合动量
        → RefIndustryMomentum (s23)  momentum+industry 加权, hold_n=8

注意: 集中度(hold_n 小)是提高年化的最直接杠杆, 但也会放大回撤;
本文件统一用 8~10 只(比旧 20~25 更集中), 年化会推高、回撤也会更大。
所有取数经 ctx 走 <=信号日 防未来函数(与 mf_core 一致)。
"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import mf_core

log = logging.getLogger("ref_strategies")

POOL_INDEX = "mainboard"   # 主板流动性池(扩池后约 1300~1500 只)


# ---------------------------------------------------------------------------
# s20 稳健质量精选(国信 稳健精选)
# ---------------------------------------------------------------------------
class RefSteadyQuality(BaseStrategy):
    """低估值/低波动/高股息/ROE质量 + 动量增强; 大盘段, 周频, 集中 8 只。"""

    def generate_orders(self, date, ctx, account):
        if not mf_core.should_rebalance(date, self.params):
            return mf_core.risk_orders(date, ctx, account, self.params,
                                      self.strategy_id, self.config)
        params = dict(self.params)
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            from strategies import news_guard
            forced = news_guard.guard_holdings(date, list(account.positions.keys()),
                                                ctx.conn, self.config)
            return [Order(self.strategy_id, c, "sell", 0.0,
                          f"稳健质量:{ctx.name(c)}无候选/黑天鹅,清仓", date)
                    for c in account.positions.keys() if c not in forced]
        return mf_core.build_orders(ctx, date, account, sel, params,
                                       self.strategy_id, self.config,
                                       stop_pct=params.get("stop_pct", 0.18))


# ---------------------------------------------------------------------------
# s21 小盘复合精选(国信 小盘精选)
# ---------------------------------------------------------------------------
class RefSmallcapCompound(BaseStrategy):
    """市值最小段 + 复合选股因子(质量+动量+低波+成长)前N等权; 月频, 集中 10 只。"""

    def generate_orders(self, date, ctx, account):
        if not mf_core.should_rebalance(date, self.params):
            return mf_core.risk_orders(date, ctx, account, self.params,
                                      self.strategy_id, self.config)
        params = dict(self.params)
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            from strategies import news_guard
            forced = news_guard.guard_holdings(date, list(account.positions.keys()),
                                                ctx.conn, self.config)
            return [Order(self.strategy_id, c, "sell", 0.0,
                          f"小盘复合:{ctx.name(c)}无候选/黑天鹅,清仓", date)
                    for c in account.positions.keys() if c not in forced]
        return mf_core.build_orders(ctx, date, account, sel, params,
                                       self.strategy_id, self.config,
                                       stop_pct=params.get("stop_pct", 0.25))


# ---------------------------------------------------------------------------
# s22 超预期精选(国信 超预期精选)
#   研报标题超预期 + 分析师上调净利润, 季频, 基本面+技术面共振。
#   实盘: news_engine 提供超预期/上调信号 → 强筛; 回测库无新闻 → 降级为 成长+动量+质量 代理。
# ---------------------------------------------------------------------------
class RefEarningsSurprise(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not mf_core.should_rebalance(date, self.params):
            return mf_core.risk_orders(date, ctx, account, self.params,
                                      self.strategy_id, self.config)
        params = dict(self.params)
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        # —— 超预期实盘叠加层(news_engine 有数据时强筛; 回测恒空 → 跳过, 降级) ——
        if sel["target"]:
            try:
                import news_engine as ne
                surp = ne.get_earnings_surprise_codes(date, ctx.conn)
                if surp:   # 仅当当日真有超预期信号时才收窄到信号池
                    tgt = [c for c in sel["target"] if c in surp]
                    if tgt:
                        sel = dict(sel)
                        sel["target"] = tgt
            except Exception:
                pass
        if not sel["target"]:
            from strategies import news_guard
            forced = news_guard.guard_holdings(date, list(account.positions.keys()),
                                                ctx.conn, self.config)
            return [Order(self.strategy_id, c, "sell", 0.0,
                          f"超预期:{ctx.name(c)}无候选/黑天鹅,清仓", date)
                    for c in account.positions.keys() if c not in forced]
        return mf_core.build_orders(ctx, date, account, sel, params,
                                       self.strategy_id, self.config,
                                       stop_pct=params.get("stop_pct", 0.20))


# ---------------------------------------------------------------------------
# s23 行业动量个股(开源金工 行业动量 / 中信建投 联合动量)
#   个股动量 + 行业地位(龙头)联合; 中盘段, 周频, 集中 8 只。
# ---------------------------------------------------------------------------
class RefIndustryMomentum(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not mf_core.should_rebalance(date, self.params):
            return mf_core.risk_orders(date, ctx, account, self.params,
                                      self.strategy_id, self.config)
        params = dict(self.params)
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            from strategies import news_guard
            forced = news_guard.guard_holdings(date, list(account.positions.keys()),
                                                ctx.conn, self.config)
            return [Order(self.strategy_id, c, "sell", 0.0,
                          f"行业动量:{ctx.name(c)}无候选/黑天鹅,清仓", date)
                    for c in account.positions.keys() if c not in forced]
        return mf_core.build_orders(ctx, date, account, sel, params,
                                       self.strategy_id, self.config,
                                       stop_pct=params.get("stop_pct", 0.22))


# ---------------------------------------------------------------------------
# s24 激进小盘集中(冲年化: 高集中度+动量主导+小盘最小段+放宽止损)
#   诚实说明: 这是"把收益杠杆拉满"的版本——持仓压到 5 只、动量权重 0.48、
#   取消分红/ROE 过滤(池更大更纯小盘)、regime 不降仓、个股止损放宽到 0.30 让盈利奔跑。
#   但全局 strategy_max_drawdown=0.10 熔断仍会在账户回撤≥10% 时清仓"冬眠",
#   与"冲 50% 年化"在数学上冲突(50% 年化≈夏普 5, 不可持续)。
#   本策略只在"放宽回撤纪律"的前提下才有机会接近高收益; 否则仍受 10% 熔断压制。
# ---------------------------------------------------------------------------
class RefAggressiveSmallcap(BaseStrategy):
    """冲收益版: 市值最小段 + 高集中度(5只) + 动量主导(0.48) + 偏小市值倾斜;
       取消分红/ROE 门槛、regime 不降仓、个股止损 0.30。"""

    def generate_orders(self, date, ctx, account):
        if not mf_core.should_rebalance(date, self.params):
            return mf_core.risk_orders(date, ctx, account, self.params,
                                      self.strategy_id, self.config)
        params = dict(self.params)
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            from strategies import news_guard
            forced = news_guard.guard_holdings(date, list(account.positions.keys()),
                                                ctx.conn, self.config)
            return [Order(self.strategy_id, c, "sell", 0.0,
                          f"激进小盘:{ctx.name(c)}无候选/黑天鹅,清仓", date)
                    for c in account.positions.keys() if c not in forced]
        return mf_core.build_orders(ctx, date, account, sel, params,
                                       self.strategy_id, self.config,
                                       stop_pct=params.get("stop_pct", 0.30))
