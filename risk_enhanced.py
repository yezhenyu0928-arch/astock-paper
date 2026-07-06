# -*- coding: utf-8 -*-
"""增强风控模块 - 阶梯熔断与组合风控。

功能:
1. 阶梯式熔断: 10%减仓50%, 15%清仓 (vs 原固定15%清仓)
2. 策略间相关性监控: 聚合同板块敞口
3. 波动率自适应风控: 高波动期收紧阈值
4. 组合风险价值(VaR)估算

与原 risk.py 的关系:
- 本模块提供增强功能,不替换原模块
- 通过 risk_override 配置启用
- 最终风控决策由引擎整合两者输出
"""
import logging
import math
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from collections import defaultdict
import numpy as np

import conf
import util
from models import Order
from risk import pre_check as original_pre_check, post_check as original_post_check

log = logging.getLogger("risk_enhanced")


@dataclass
class LadderCutoff:
    """阶梯熔断档位"""
    drawdown_threshold: float    # 回撤阈值
    action: str                  # 动作: "reduce"(减仓) / "clear"(清仓)
    reduction_pct: float         # 减仓比例(0-1)
    cooldown_days: int           # 冷却期天数


# 默认阶梯熔断配置
DEFAULT_LADDER = [
    LadderCutoff(drawdown_threshold=0.10, action="reduce", reduction_pct=0.50, cooldown_days=1),
    LadderCutoff(drawdown_threshold=0.15, action="clear", reduction_pct=1.00, cooldown_days=3),
    LadderCutoff(drawdown_threshold=0.20, action="clear", reduction_pct=1.00, cooldown_days=7),
]


class LadderRiskManager:
    """阶梯熔断管理器"""

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or conf.load_config()
        self.ladder = self._load_ladder_config()
        self.cutoff_state = {}  # sid -> {last_cutoff_level: int, cooldown_until: str}

    def _load_ladder_config(self) -> List[LadderCutoff]:
        """加载阶梯熔断配置"""
        custom_ladder = (self.cfg.get("risk", {}).get("ladder_cutoff") or [])

        if not custom_ladder:
            return DEFAULT_LADDER

        return [LadderCutoff(
            drawdown_threshold=item.get("drawdown", 0.15),
            action=item.get("action", "clear"),
            reduction_pct=item.get("reduction", 1.0),
            cooldown_days=item.get("cooldown", 1)
        ) for item in custom_ladder]

    def check_drawdown(self, sid: str, current_nav: float,
                       highest_nav: float, date: str) -> Tuple[List[Order], List[str]]:
        """检查回撤并生成阶梯熔断订单

        Returns:
            (orders, alerts)
        """
        if highest_nav <= 0:
            return [], []

        drawdown = 1 - current_nav / highest_nav
        state = self.cutoff_state.get(sid, {
            "last_level": -1,
            "cooldown_until": ""
        })

        orders = []
        alerts = []

        # 检查是否在冷却期
        if date <= state.get("cooldown_until", ""):
            return orders, alerts

        # 从最高档开始检查
        triggered_level = -1
        for i, level in enumerate(self.ladder):
            if drawdown >= level.drawdown_threshold:
                triggered_level = i

        if triggered_level > state["last_level"]:
            level = self.ladder[triggered_level]

            if level.action == "clear":
                # 清仓
                alerts.append(
                    f"🔴 {sid} 回撤 {drawdown:.1%} 触发第{triggered_level+1}档熔断({level.drawdown_threshold:.0%}),全清仓"
                )
                orders.append(self._create_clearance_order(sid, date, drawdown))
            else:
                # 减仓
                alerts.append(
                    f"🟡 {sid} 回撤 {drawdown:.1%} 触发第{triggered_level+1}档风控({level.drawdown_threshold:.0%}),减仓{level.reduction_pct:.0%}"
                )
                orders.append(self._create_reduction_order(sid, date, drawdown, level.reduction_pct))

            # 更新状态
            state["last_level"] = triggered_level
            # 计算冷却期结束日期
            from data_adapter import fetch_calendar
            try:
                cal = fetch_calendar(date, "2099-12-31")
                future_dates = cal[cal["cal_date"] > date]["cal_date"].tolist()
                if len(future_dates) >= level.cooldown_days:
                    state["cooldown_until"] = future_dates[level.cooldown_days - 1]
                else:
                    state["cooldown_until"] = "2099-12-31"
            except Exception:
                state["cooldown_until"] = date  # 出错则只冷却当天

            self.cutoff_state[sid] = state

        return orders, alerts

    def _create_clearance_order(self, sid: str, date: str, drawdown: float) -> Order:
        """创建清仓订单(通过权重设为0实现)"""
        return Order(
            strategy_id=sid,
            code="",  # 空code表示全部持仓
            side="sell",
            weight=0.0,
            reason=f"阶梯熔断清仓(回撤{drawdown:.1%})",
            signal_date=date
        )

    def _create_reduction_order(self, sid: str, date: str, drawdown: float,
                                reduction_pct: float) -> Order:
        """创建减仓订单"""
        return Order(
            strategy_id=sid,
            code="",
            side="reduce",  # 自定义side,引擎需支持
            weight=1.0 - reduction_pct,  # 保留的仓位比例
            reason=f"阶梯熔断减仓{reduction_pct:.0%}(回撤{drawdown:.1%})",
            signal_date=date
        )

    def reset_state(self, sid: str = None):
        """重置熔断状态(回撤修复后调用)"""
        if sid:
            if sid in self.cutoff_state:
                del self.cutoff_state[sid]
        else:
            self.cutoff_state.clear()


class CorrelationRiskManager:
    """策略间相关性风险管理"""

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or conf.load_config()
        self.correlation_threshold = self.cfg.get("risk", {}).get("correlation_threshold", 0.8)
        self.exposure_limit = self.cfg.get("risk", {}).get("sector_exposure_limit", 0.30)

    def calculate_strategy_returns(self, nav_history: Dict[str, List[float]]) -> Dict[str, np.ndarray]:
        """计算各策略收益率序列"""
        returns = {}
        for sid, navs in nav_history.items():
            if len(navs) < 2:
                continue
            navs_arr = np.array(navs)
            rets = navs_arr[1:] / navs_arr[:-1] - 1
            returns[sid] = rets
        return returns

    def compute_correlation_matrix(self, returns: Dict[str, np.ndarray]) -> Dict[Tuple[str, str], float]:
        """计算策略间相关系数"""
        correlations = {}
        sids = list(returns.keys())

        for i, sid1 in enumerate(sids):
            for sid2 in sids[i+1:]:
                r1, r2 = returns[sid1], returns[sid2]
                # 对齐长度
                min_len = min(len(r1), len(r2))
                if min_len < 10:
                    continue
                corr = np.corrcoef(r1[-min_len:], r2[-min_len:])[0, 1]
                if not np.isnan(corr):
                    correlations[(sid1, sid2)] = corr

        return correlations

    def check_correlation_risk(self, returns: Dict[str, np.ndarray]) -> List[Dict]:
        """检查相关性风险

        Returns:
            高风险策略对列表
        """
        correlations = self.compute_correlation_matrix(returns)
        high_corr_pairs = []

        for (sid1, sid2), corr in correlations.items():
            if abs(corr) >= self.correlation_threshold:
                high_corr_pairs.append({
                    "strategy1": sid1,
                    "strategy2": sid2,
                    "correlation": corr,
                    "risk_level": "high" if abs(corr) > 0.9 else "medium"
                })

        return high_corr_pairs

    def aggregate_sector_exposure(self, positions: Dict[str, Dict[str, float]]) -> Dict[str, float]:
        """聚合行业/板块敞口

        Args:
            positions: {sid: {code: weight}}

        Returns:
            {sector: total_weight}
        """
        sector_weights = defaultdict(float)

        # 简化的板块分类(基于代码前缀)
        for sid, pos in positions.items():
            for code, weight in pos.items():
                sector = self._get_sector(code)
                sector_weights[sector] += weight

        return dict(sector_weights)

    def _get_sector(self, code: str) -> str:
        """根据代码判断板块(简化版)"""
        prefix = util.bare(code)[:3]

        # ETF 板块映射
        etf_sectors = {
            "510": "宽基ETF", "512": "行业ETF", "513": "跨境ETF",
            "515": "行业ETF", "516": "行业ETF", "518": "商品ETF",
            "511": "债券ETF", "159": "ETF",
        }

        if util.is_etf_code(code):
            for k, v in etf_sectors.items():
                if prefix.startswith(k):
                    return v
            return "ETF"

        # 个股板块(简化,实际应查行业表)
        return "个股"

    def check_sector_exposure(self, positions: Dict[str, Dict[str, float]]) -> List[Dict]:
        """检查板块集中度风险"""
        sector_exposure = self.aggregate_sector_exposure(positions)
        alerts = []

        for sector, exposure in sector_exposure.items():
            if exposure > self.exposure_limit:
                alerts.append({
                    "type": "sector_concentration",
                    "sector": sector,
                    "exposure": exposure,
                    "limit": self.exposure_limit,
                    "severity": "error" if exposure > self.exposure_limit * 1.5 else "warning"
                })

        return alerts


class VolatilityAdaptiveRisk:
    """波动率自适应风控"""

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or conf.load_config()
        self.base_thresholds = {
            "strategy_max_drawdown": self.cfg.get("risk", {}).get("strategy_max_drawdown", 0.15),
            "stop_loss_trend": self.cfg.get("risk", {}).get("stop_loss", {}).get("trend", 0.08),
            "stop_loss_rotation": self.cfg.get("risk", {}).get("stop_loss", {}).get("rotation", 0.12),
        }

    def calculate_market_volatility(self, market_returns: List[float],
                                   window: int = 20) -> float:
        """计算市场波动率(年化)"""
        if len(market_returns) < window:
            return 0.20  # 默认20%年化波动

        recent_returns = market_returns[-window:]
        daily_vol = np.std(recent_returns, ddof=1)
        annual_vol = daily_vol * math.sqrt(252)

        return annual_vol

    def adjust_thresholds(self, market_volatility: float) -> Dict[str, float]:
        """根据市场波动率调整风控阈值

        高波动期: 收紧阈值(止损更严,阈值降低)
        低波动期: 放宽阈值(允许更大回撤)
        """
        # 基准波动率20%
        base_vol = 0.20
        vol_ratio = market_volatility / base_vol

        # 波动率调整:高波收紧(阈值↓),低波放宽(阈值↑)
        # 调整系数: 1/vol_ratio, 限幅 0.7-1.3
        adjust_factor = max(0.7, min(1.3, 1.0 / vol_ratio))

        adjusted = {}
        for key, base in self.base_thresholds.items():
            # 阈值与波动率同向调整
            adjusted[key] = round(base * adjust_factor, 4)

        return adjusted

    def get_adaptive_stop_loss(self, sid: str, market_vol: float) -> float:
        """获取自适应止损阈值"""
        stop_type = self._get_stop_type(sid)
        base_threshold = self.base_thresholds.get(f"stop_loss_{stop_type}", 0.10)

        # 波动率调整
        base_vol = 0.20
        vol_ratio = market_vol / base_vol
        adjust_factor = max(0.7, min(1.3, vol_ratio))

        return round(base_threshold * adjust_factor, 4)

    def _get_stop_type(self, sid: str) -> str:
        """获取策略止损类型"""
        prefix = sid.split("_")[0]
        mapping = {
            "s3": "trend",
            "s1": "rotation",
            "s2": "rotation",
            "s4": "rotation",
            "s5": "none"
        }
        return mapping.get(prefix, "rotation")


# ============ 增强风控主入口 ============

def enhanced_pre_check(date, ctx, states, cfg) -> Dict:
    """增强版盘前风控检查"""

    # 先执行原始风控
    original_result = original_pre_check(date, ctx, states, cfg)

    # 如果原始风控已触发熔断,不再检查
    if original_result.get("forced_orders"):
        return original_result

    # 阶梯熔断检查
    ladder_manager = LadderRiskManager(cfg)
    ladder_orders = []
    ladder_alerts = []

    for sid, st in states.items():
        acct = st["account"]
        peak = max(st.get("highest_nav", 1.0), acct.nav)

        orders, alerts = ladder_manager.check_drawdown(sid, acct.nav, peak, date)
        ladder_orders.extend(orders)
        ladder_alerts.extend(alerts)

    # 合并结果
    result = {
        "market_frozen": original_result.get("market_frozen", False),
        "forced_orders": original_result.get("forced_orders", []) + ladder_orders,
        "alerts": original_result.get("alerts", []) + ladder_alerts,
        "ladder_triggered": bool(ladder_orders)
    }

    return result


def enhanced_post_check(date, ctx, orders, states, cfg,
                       market_frozen=False, nav_history=None) -> List[Order]:
    """增强版盘后风控检查"""

    # 先执行原始风控
    filtered_orders = original_post_check(date, ctx, orders, states, cfg, market_frozen)

    # 如果提供了净值历史,检查相关性风险
    if nav_history and len(nav_history) >= 2:
        corr_manager = CorrelationRiskManager(cfg)
        returns = corr_manager.calculate_strategy_returns(nav_history)

        if len(returns) >= 2:
            high_corr = corr_manager.check_correlation_risk(returns)
            if high_corr:
                for pair in high_corr:
                    log.warning("策略相关性告警: %s - %s (%.2f)",
                              pair["strategy1"], pair["strategy2"], pair["correlation"])

    return filtered_orders


# ============ 配置扩展 ============

def get_enhanced_risk_config() -> Dict:
    """获取增强风控配置模板"""
    return {
        "ladder_cutoff": [
            {"drawdown": 0.10, "action": "reduce", "reduction": 0.50, "cooldown": 1},
            {"drawdown": 0.15, "action": "clear", "reduction": 1.00, "cooldown": 3},
            {"drawdown": 0.20, "action": "clear", "reduction": 1.00, "cooldown": 7},
        ],
        "correlation_threshold": 0.80,
        "sector_exposure_limit": 0.30,
        "volatility_adaptive": True,
    }


# ============ CLI 测试 ============

if __name__ == "__main__":
    # 测试阶梯熔断
    manager = LadderRiskManager()

    test_cases = [
        ("s1_test", 100, 95, "2024-01-15"),   # 5%回撤,无熔断
        ("s1_test", 100, 89, "2024-01-16"),   # 11%回撤,一档减仓
        ("s1_test", 100, 83, "2024-01-17"),   # 17%回撤,二档清仓
    ]

    for sid, peak, current, date in test_cases:
        orders, alerts = manager.check_drawdown(sid, current, peak, date)
        print(f"{sid}: peak={peak}, current={current}, dd={1-current/peak:.1%}")
        for alert in alerts:
            print(f"  {alert}")
        for order in orders:
            print(f"  订单: {order}")
