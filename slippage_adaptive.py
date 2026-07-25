# -*- coding: utf-8 -*-
"""自适应滑点模型 - 基于市场微观结构的动态滑点估算。

功能:
1. 基于成交量/流动性的自适应滑点
2. 基于波动率的滑点调整
3. 大单拆分执行建模
4. 时间加权成交概率(尾盘信号成交概率降低)

与原滑点模型的区别:
- 原模型:固定滑点(ETF 0.05%, 个股 0.15%)
- 新模型:动态滑点,基于实时市场条件

滑点计算公式:
slippage = base_slippage + volume_adjustment + volatility_adjustment + time_adjustment

其中:
- base_slippage: 基础滑点(与原模型一致)
- volume_adjustment: 成交量不足时的额外滑点
- volatility_adjustment: 高波动时的额外滑点
- time_adjustment: 非理想交易时段的调整
"""
import math
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import numpy as np

import conf
import util
from data_adapter import fetch_daily, is_etf_code

log = logging.getLogger("adaptive_slippage")


@dataclass
class MarketCondition:
    """市场条件"""
    avg_daily_volume: float      # 日均成交量(股)
    avg_daily_amount: float      # 日均成交额(元)
    volatility_20d: float        # 20日年化波动率
    avg_spread_pct: float        # 平均买卖价差(%)
    current_price: float         # 当前价格


class AdaptiveSlippageModel:
    """自适应滑点模型"""

    # 基础滑点配置(与原模型一致)
    BASE_SLIPPAGE = {
        "etf": 0.0005,      # 0.05%
        "stock": 0.0015,    # 0.15%
    }

    # 成交量影响系数
    VOLUME_IMPACT_K = {
        "etf": 0.0003,
        "stock": 0.0010,
    }

    # 波动率影响系数
    VOLATILITY_IMPACT_K = {
        "etf": 0.0002,
        "stock": 0.0005,
    }

    # 时间影响系数(交易时段)
    TIME_IMPACT = {
        "opening": 0.0003,      # 开盘 9:30-10:00
        "normal": 0.0,          # 正常 10:00-14:30
        "closing": 0.0005,      # 收盘 14:30-15:00
    }

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or conf.load_config()
        self.impact_k = self.cfg.get("custom", {}).get("impact_k", {
            "stock": 0.0020, "etf": 0.0008
        })

    def estimate_slippage(self, code: str, order_amount: float,
                         market_condition: MarketCondition,
                         time_slot: str = "normal") -> float:
        """估算滑点

        Args:
            code: 股票代码
            order_amount: 订单金额(元)
            market_condition: 市场条件
            time_slot: 交易时段(opening/normal/closing)

        Returns:
            滑点比例(如 0.002 表示 0.2%)
        """
        asset_type = "etf" if is_etf_code(code) else "stock"

        # 1. 基础滑点
        base = self.BASE_SLIPPAGE[asset_type]

        # 2. 成交量调整(订单占日均成交额的百分比)
        volume_ratio = order_amount / max(market_condition.avg_daily_amount, 1)
        volume_adj = self._volume_adjustment(volume_ratio, asset_type)

        # 3. 波动率调整
        vol_adj = self._volatility_adjustment(market_condition.volatility_20d, asset_type)

        # 4. 时间调整
        time_adj = self.TIME_IMPACT.get(time_slot, 0.0)

        # 5. 买卖价差调整
        spread_adj = market_condition.avg_spread_pct * 0.5  # 假设吃一半价差

        # 总滑点
        total_slippage = base + volume_adj + vol_adj + time_adj + spread_adj

        # 滑点上限(防止极端情况)
        max_slippage = 0.02 if asset_type == "stock" else 0.01  # 个股2%,ETF1%
        total_slippage = min(total_slippage, max_slippage)

        log.debug("滑点估算 %s: base=%.4f, vol_adj=%.4f, vola_adj=%.4f, time_adj=%.4f, spread_adj=%.4f, total=%.4f",
                 code, base, volume_adj, vol_adj, time_adj, spread_adj, total_slippage)

        return round(total_slippage, 6)

    def _volume_adjustment(self, volume_ratio: float, asset_type: str) -> float:
        """成交量调整项

        订单占日均成交额比例越大,滑点越大
        """
        k = self.VOLUME_IMPACT_K[asset_type]

        if volume_ratio <= 0.001:  # 小于0.1%
            return 0.0
        elif volume_ratio <= 0.01:  # 0.1%-1%
            return k * volume_ratio * 100  # 线性增长
        elif volume_ratio <= 0.05:  # 1%-5%
            return k * (1 + (volume_ratio - 0.01) * 50)  # 加速增长
        else:  # >5%
            return k * 3  # 封顶

    def _volatility_adjustment(self, volatility: float, asset_type: str) -> float:
        """波动率调整项

        波动率越高,滑点越大
        """
        base_vol = 0.20  # 基准波动率20%
        k = self.VOLATILITY_IMPACT_K[asset_type]

        if volatility <= base_vol * 0.5:  # 低波动
            return -k * 0.5  # 滑点减小
        elif volatility <= base_vol:  # 正常波动
            return 0.0
        elif volatility <= base_vol * 2:  # 高波动
            return k * (volatility / base_vol - 1)
        else:  # 极高波动
            return k * 2  # 封顶

    def get_market_condition(self, code: str, ctx=None, date: str = None) -> MarketCondition:
        """获取市场条件"""
        try:
            # 从数据库获取近期数据
            if ctx:
                # 使用 DataContext
                closes = ctx.close(code, 21)
                volumes = []  # DataContext 不直接提供volume,需要其他方式
            else:
                # 直接查询
                from db import get_conn
                conn = get_conn()
                rows = conn.execute(
                    "SELECT close, volume, amount FROM daily_bar WHERE code=? ORDER BY trade_date DESC LIMIT 20",
                    (code,)
                ).fetchall()
                conn.close()

                if not rows:
                    return self._default_market_condition()

                closes = [r[0] for r in reversed(rows)]
                volumes = [r[1] for r in rows]
                amounts = [r[2] for r in rows]

            # 计算波动率
            if len(closes) >= 2:
                returns = [closes[i]/closes[i-1] - 1 for i in range(1, len(closes))]
                daily_vol = np.std(returns, ddof=1) if len(returns) > 1 else 0.02
                annual_vol = daily_vol * math.sqrt(252)
            else:
                annual_vol = 0.20

            # 计算日均成交量和成交额
            avg_volume = np.mean(volumes) if volumes else 0
            avg_amount = np.mean([r[2] for r in rows]) if rows else 0

            # 估算买卖价差(简化模型:价格越低,价差百分比越大)
            current_price = closes[-1] if closes else 10.0
            spread_pct = self._estimate_spread(code, current_price)

            return MarketCondition(
                avg_daily_volume=avg_volume,
                avg_daily_amount=avg_amount,
                volatility_20d=annual_vol,
                avg_spread_pct=spread_pct,
                current_price=current_price
            )

        except Exception as e:
            log.warning("获取市场条件失败 %s: %s", code, e)
            return self._default_market_condition()

    def _default_market_condition(self) -> MarketCondition:
        """默认市场条件"""
        return MarketCondition(
            avg_daily_volume=1000000,
            avg_daily_amount=10000000,
            volatility_20d=0.20,
            avg_spread_pct=0.001,
            current_price=10.0
        )

    def _estimate_spread(self, code: str, price: float) -> float:
        """估算买卖价差百分比

        简化模型:
        - ETF: 价差较小(0.02%-0.05%)
        - 个股: 价差较大,与价格相关
          - 高价股(>100): 0.05%
          - 中价股(10-100): 0.1%
          - 低价股(<10): 0.2%
        """
        if is_etf_code(code):
            return 0.0003  # 0.03%

        if price >= 100:
            return 0.0005
        elif price >= 10:
            return 0.0010
        else:
            return 0.0020


class OrderSlicer:
    """大单拆分执行建模"""

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or conf.load_config()
        self.max_participation = self.cfg.get("custom", {}).get("max_participation", 0.02)  # 最大参与率2%

    def should_slice(self, order_amount: float, avg_daily_amount: float) -> bool:
        """判断是否需要拆分订单"""
        if avg_daily_amount <= 0:
            return False
        participation = order_amount / avg_daily_amount
        return participation > self.max_participation

    def slice_order(self, total_shares: int, code: str,
                   avg_daily_volume: float, avg_daily_amount: float,
                   days: int = 3) -> List[Dict]:
        """拆分订单为多日执行

        Args:
            total_shares: 总股数
            code: 股票代码
            avg_daily_volume: 日均成交量
            avg_daily_amount: 日均成交额
            days: 拆分天数

        Returns:
            拆分后的订单列表 [{shares, day_offset, fill_probability}]
        """
        if avg_daily_volume <= 0:
            return [{"shares": total_shares, "day_offset": 0, "fill_probability": 1.0}]

        # 每日可成交股数(按参与率限制)
        daily_limit = int(avg_daily_volume * self.max_participation)
        daily_limit = max(daily_limit, 100)  # 至少100股

        slices = []
        remaining = total_shares

        for day in range(days):
            if remaining <= 0:
                break

            # 当日股数
            day_shares = min(remaining, daily_limit)

            # 成交概率(随天数递减)
            fill_prob = max(0.3, 1.0 - day * 0.2)

            slices.append({
                "shares": day_shares,
                "day_offset": day,
                "fill_probability": fill_prob
            })

            remaining -= day_shares

        # 如果还有剩余,合并到最后一天
        if remaining > 0 and slices:
            slices[-1]["shares"] += remaining
            slices[-1]["fill_probability"] *= 0.8  # 降低成交概率

        return slices


class TimeWeightedFillProbability:
    """时间加权成交概率"""

    # 交易时段成交概率权重
    TIME_WEIGHTS = {
        "09:30": 0.80,  # 开盘
        "10:00": 1.00,  # 早盘活跃期
        "11:00": 0.95,
        "13:00": 0.85,  # 午后开盘
        "14:00": 0.90,
        "14:30": 0.85,
        "14:50": 0.70,  # 收盘前(信号生成后成交概率降低)
        "15:00": 0.60,  # 收盘
    }

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or conf.load_config()

    def get_fill_probability(self, signal_time: str, is_etf: bool = False) -> float:
        """获取成交概率

        Args:
            signal_time: 信号生成时间(HH:MM)
            is_etf: 是否为ETF

        Returns:
            成交概率(0-1)
        """
        # ETF成交概率更高
        base_prob = 1.0 if is_etf else 0.95

        # 根据时间调整
        hour_min = signal_time[:5] if len(signal_time) >= 5 else signal_time

        # 找到最接近的时间点
        weights = self.TIME_WEIGHTS
        closest_time = min(weights.keys(),
                          key=lambda t: abs(self._time_to_minutes(t) - self._time_to_minutes(hour_min)))

        time_weight = weights.get(closest_time, 0.9)

        return base_prob * time_weight

    def _time_to_minutes(self, time_str: str) -> int:
        """时间字符串转分钟数"""
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    def adjust_slippage_by_time(self, base_slippage: float,
                                signal_time: str) -> float:
        """根据信号时间调整滑点"""
        fill_prob = self.get_fill_probability(signal_time)

        # 成交概率越低,滑点越大(追价成本)
        if fill_prob >= 0.9:
            return base_slippage
        elif fill_prob >= 0.7:
            return base_slippage * 1.2
        else:
            return base_slippage * 1.5


# ============ 与原引擎的集成接口 ============

def get_adaptive_slippage(code: str, order_value: float,
                         ctx=None, date: str = None,
                         time_slot: str = "normal") -> float:
    """获取自适应滑点(供引擎调用)

    Args:
        code: 股票代码
        order_value: 订单金额
        ctx: DataContext(可选)
        date: 日期(可选)
        time_slot: 交易时段

    Returns:
        滑点比例
    """
    model = AdaptiveSlippageModel()
    market_condition = model.get_market_condition(code, ctx, date)

    return model.estimate_slippage(code, order_value, market_condition, time_slot)


def calculate_filled_shares(total_shares: int, code: str,
                           avg_daily_volume: float,
                           days: int = 1) -> int:
    """计算实际可成交股数(考虑流动性截断)

    与原引擎的流动性截断逻辑一致,但更精细
    """
    if avg_daily_volume <= 0:
        return total_shares

    # 每日可成交上限(参与率2%)
    daily_limit = int(avg_daily_volume * 0.02)
    daily_limit = max(daily_limit, 100)

    # 多天累计
    total_limit = daily_limit * days

    return min(total_shares, total_limit)


# ============ 配置扩展 ============

def get_adaptive_slippage_config() -> Dict:
    """获取自适应滑点配置模板"""
    return {
        "slippage_model": "adaptive",  # adaptive 或 fixed
        "base_slippage": {
            "etf": 0.0005,
            "stock": 0.0015,
        },
        "volume_impact_k": {
            "etf": 0.0003,
            "stock": 0.0010,
        },
        "volatility_impact_k": {
            "etf": 0.0002,
            "stock": 0.0005,
        },
        "max_participation": 0.02,  # 最大日参与率
        "order_slice_days": 3,      # 大单拆分天数
    }


# ============ CLI 测试 ============

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="自适应滑点模型测试")
    parser.add_argument("code", help="股票代码")
    parser.add_argument("--amount", type=float, default=100000, help="订单金额")
    parser.add_argument("--volume", type=float, default=1000000, help="日均成交量")
    parser.add_argument("--volatility", type=float, default=0.20, help="年化波动率")
    parser.add_argument("--time", default="normal", choices=["opening", "normal", "closing"],
                       help="交易时段")
    args = parser.parse_args()

    model = AdaptiveSlippageModel()

    market_condition = MarketCondition(
        avg_daily_volume=args.volume,
        avg_daily_amount=args.volume * 10,  # 假设均价10元
        volatility_20d=args.volatility,
        avg_spread_pct=0.001,
        current_price=10.0
    )

    slippage = model.estimate_slippage(args.code, args.amount, market_condition, args.time)

    print(f"代码: {args.code}")
    print(f"订单金额: {args.amount:,.0f} 元")
    print(f"日均成交量: {args.volume:,.0f} 股")
    print(f"波动率: {args.volatility:.1%}")
    print(f"时段: {args.time}")
    print(f"估算滑点: {slippage:.4f} ({slippage:.2%})")
    print(f"滑点成本: {args.amount * slippage:,.2f} 元")
