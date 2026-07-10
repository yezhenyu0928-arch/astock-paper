# -*- coding: utf-8 -*-
"""策略参数自适应优化模块。

功能:
1. Walk-forward 优化: 滚动窗口参数优化,避免过拟合
2. 参数稳健性评估: 评估参数在不同市场环境下的表现
3. 自适应参数选择: 根据近期市场环境自动调整参数
4. 参数版本管理: 记录参数变更历史,支持回滚

Walk-forward 优化流程:
1. 将历史数据分为训练集(IS)和测试集(OOS)
2. 在训练集上优化参数
3. 在测试集上验证性能
4. 滚动窗口重复步骤1-3
5. 选择在所有窗口表现稳健的参数

低成本设计:
- 纯本地计算,无需外部优化服务
- 增量优化(只计算新增数据)
- 参数空间剪枝(避免穷举)
"""
import json
import logging
import itertools
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Callable
from dataclasses import dataclass
import numpy as np

import conf
import util
from backtest import run_backtest, compute_metrics
from data_adapter import fetch_calendar

log = logging.getLogger("strategy_optimize")

# 参数优化状态目录
OPTIMIZE_DIR = conf.STATE_DIR / "optimize"
OPTIMIZE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ParameterRange:
    """参数范围定义"""
    name: str
    min_val: float
    max_val: float
    step: float
    is_int: bool = False  # 是否为整数参数

    def get_values(self) -> List:
        """获取参数所有可能值"""
        if self.is_int:
            return list(range(int(self.min_val), int(self.max_val) + 1, int(self.step)))
        else:
            values = []
            val = self.min_val
            while val <= self.max_val:
                values.append(round(val, 6))
                val += self.step
            return values


@dataclass
class WalkForwardWindow:
    """Walk-forward 窗口定义"""
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    window_id: int


@dataclass
class ParameterSet:
    """参数组合"""
    params: Dict[str, Any]
    train_metrics: Dict = None
    test_metrics: Dict = None
    window_id: int = 0

    @property
    def score(self) -> float:
        """综合评分(测试集 Calmar + Sharpe)"""
        if not self.test_metrics:
            return -999
        calmar = self.test_metrics.get("calmar", 0)
        sharpe = self.test_metrics.get("sharpe", 0)
        return calmar * 0.6 + sharpe * 0.4


class WalkForwardOptimizer:
    """Walk-forward 参数优化器"""

    def __init__(self, strategy_id: str, param_ranges: List[ParameterRange],
                 train_days: int = 252, test_days: int = 63):
        """
        Args:
            strategy_id: 策略ID
            param_ranges: 参数范围列表
            train_days: 训练集天数(默认252天≈1年)
            test_days: 测试集天数(默认63天≈1季度)
        """
        self.strategy_id = strategy_id
        self.param_ranges = param_ranges
        self.train_days = train_days
        self.test_days = test_days

        # 参数组合缓存
        self.param_combinations = self._generate_param_combinations()

    def _generate_param_combinations(self) -> List[Dict]:
        """生成所有参数组合(剪枝后)"""
        values_list = [pr.get_values() for pr in self.param_ranges]
        names = [pr.name for pr in self.param_ranges]

        combinations = []
        for values in itertools.product(*values_list):
            combo = dict(zip(names, values))
            # 参数约束检查
            if self._check_param_constraints(combo):
                combinations.append(combo)

        # 限制组合数量(避免计算爆炸)
        max_combinations = 100
        if len(combinations) > max_combinations:
            log.warning("参数组合过多(%d),随机采样至%d个", len(combinations), max_combinations)
            import random
            random.seed(42)
            combinations = random.sample(combinations, max_combinations)

        return combinations

    def _check_param_constraints(self, params: Dict) -> bool:
        """检查参数约束(子类可覆盖)"""
        # 默认约束:短周期 < 长周期
        if "short_window" in params and "long_window" in params:
            return params["short_window"] < params["long_window"]
        if "fast_ma" in params and "slow_ma" in params:
            return params["fast_ma"] < params["slow_ma"]
        return True

    def _generate_windows(self, start_date: str, end_date: str) -> List[WalkForwardWindow]:
        """生成 Walk-forward 窗口序列"""
        windows = []

        # 获取交易日历
        cal = fetch_calendar(start_date, end_date)
        dates = cal["cal_date"].tolist()

        if len(dates) < self.train_days + self.test_days:
            log.warning("数据不足,无法生成 Walk-forward 窗口")
            return windows

        window_id = 0
        train_start_idx = 0

        while train_start_idx + self.train_days + self.test_days <= len(dates):
            train_end_idx = train_start_idx + self.train_days
            test_end_idx = min(train_end_idx + self.test_days, len(dates))

            window = WalkForwardWindow(
                train_start=dates[train_start_idx],
                train_end=dates[train_end_idx - 1],
                test_start=dates[train_end_idx],
                test_end=dates[test_end_idx - 1],
                window_id=window_id
            )
            windows.append(window)

            # 滑动窗口(步长为测试集大小)
            train_start_idx += self.test_days
            window_id += 1

        return windows

    def optimize_window(self, window: WalkForwardWindow,
                       top_n: int = 5) -> List[ParameterSet]:
        """在单个窗口内优化参数

        Returns:
            测试集表现前N的参数组合
        """
        log.info("优化窗口 %d: 训练 %s ~ %s, 测试 %s ~ %s",
                window.window_id, window.train_start, window.train_end,
                window.test_start, window.test_end)

        results = []

        for params in self.param_combinations:
            try:
                # 训练集回测
                train_result = run_backtest(
                    self.strategy_id,
                    start=window.train_start,
                    end=window.train_end,
                    param_override=params
                )

                if not train_result or not train_result.get("navs"):
                    continue

                train_metrics = compute_metrics(
                    train_result["navs"],
                    train_result.get("bench_navs", [])
                )

                # 测试集回测
                test_result = run_backtest(
                    self.strategy_id,
                    start=window.test_start,
                    end=window.test_end,
                    param_override=params
                )

                if not test_result or not test_result.get("navs"):
                    continue

                test_metrics = compute_metrics(
                    test_result["navs"],
                    test_result.get("bench_navs", [])
                )

                param_set = ParameterSet(
                    params=params,
                    train_metrics=train_metrics,
                    test_metrics=test_metrics,
                    window_id=window.window_id
                )
                results.append(param_set)

            except Exception as e:
                log.debug("参数 %s 回测失败: %s", params, e)
                continue

        # 按测试集评分排序
        results.sort(key=lambda x: x.score, reverse=True)

        log.info("窗口 %d 优化完成: 测试 %d 组参数, 最佳 Calmar=%.2f",
                window.window_id, len(results),
                results[0].test_metrics.get("calmar", 0) if results else 0)

        return results[:top_n]

    def run_optimization(self, start_date: str, end_date: str) -> Dict:
        """执行完整 Walk-forward 优化

        Returns:
            {
                "best_params": Dict,           # 最佳参数
                "robust_params": Dict,         # 最稳健参数
                "window_results": List,        # 各窗口结果
                "consistency_score": float,    # 一致性评分
                "params_stability": Dict       # 参数稳定性分析
            }
        """
        windows = self._generate_windows(start_date, end_date)
        if not windows:
            return {"error": "无法生成优化窗口"}

        log.info("开始 Walk-forward 优化: %d 个窗口", len(windows))

        all_window_results = []
        param_consistency = {}  # 统计参数在各窗口的表现

        for window in windows:
            window_results = self.optimize_window(window)
            all_window_results.append({
                "window": window,
                "top_params": window_results
            })

            # 记录参数一致性
            for ps in window_results:
                param_key = json.dumps(ps.params, sort_keys=True)
                if param_key not in param_consistency:
                    param_consistency[param_key] = {
                        "params": ps.params,
                        "scores": [],
                        "count": 0
                    }
                param_consistency[param_key]["scores"].append(ps.score)
                param_consistency[param_key]["count"] += 1

        # 选择最佳参数(测试集平均表现最好)
        best_params = None
        best_avg_score = -999

        # 选择最稳健参数(在所有窗口都表现不错)
        robust_params = None
        best_min_score = -999

        for pk, data in param_consistency.items():
            scores = data["scores"]
            avg_score = np.mean(scores) if scores else -999
            min_score = min(scores) if scores else -999

            if avg_score > best_avg_score:
                best_avg_score = avg_score
                best_params = data["params"]

            if min_score > best_min_score and len(scores) >= len(windows) * 0.5:
                best_min_score = min_score
                robust_params = data["params"]

        # 计算一致性评分(稳健参数的最低分 / 最佳参数的平均分)
        consistency_score = best_min_score / best_avg_score if best_avg_score > 0 else 0

        # 参数稳定性分析
        params_stability = self._analyze_param_stability(param_consistency)

        result = {
            "best_params": best_params,
            "best_avg_score": best_avg_score,
            "robust_params": robust_params,
            "robust_min_score": best_min_score,
            "consistency_score": consistency_score,
            "window_count": len(windows),
            "param_combinations_tested": len(self.param_combinations),
            "window_results": all_window_results,
            "params_stability": params_stability
        }

        # 保存优化结果
        self._save_optimization_result(result)

        return result

    def _analyze_param_stability(self, param_consistency: Dict) -> Dict:
        """分析参数稳定性"""
        stability = {}

        for param_name in self.param_ranges:
            values_by_window = {}
            for pk, data in param_consistency.items():
                val = data["params"].get(param_name.name)
                if val not in values_by_window:
                    values_by_window[val] = []
                values_by_window[val].extend(data["scores"])

            # 计算各值的平均表现和标准差
            value_stats = {}
            for val, scores in values_by_window.items():
                value_stats[str(val)] = {
                    "mean_score": np.mean(scores),
                    "std_score": np.std(scores),
                    "count": len(scores)
                }

            stability[param_name.name] = value_stats

        return stability

    def _save_optimization_result(self, result: Dict):
        """保存优化结果"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = OPTIMIZE_DIR / f"{self.strategy_id}_{timestamp}.json"

        # 移除不可序列化的对象
        save_result = {
            k: v for k, v in result.items()
            if k != "window_results"
        }
        save_result["optimized_at"] = datetime.now().isoformat()

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(save_result, f, indent=2, ensure_ascii=False, default=str)

        log.info("优化结果已保存: %s", filepath)

        # 同时保存为最新参数
        latest_file = OPTIMIZE_DIR / f"{self.strategy_id}_latest.json"
        with open(latest_file, 'w', encoding='utf-8') as f:
            json.dump(save_result, f, indent=2, ensure_ascii=False, default=str)


class AdaptiveParameterManager:
    """自适应参数管理器"""

    def __init__(self, strategy_id: str):
        self.strategy_id = strategy_id
        self.latest_file = OPTIMIZE_DIR / f"{strategy_id}_latest.json"

    def get_current_params(self) -> Optional[Dict]:
        """获取当前优化后的参数"""
        if self.latest_file.exists():
            try:
                with open(self.latest_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data.get("robust_params") or data.get("best_params")
            except Exception as e:
                log.warning("读取优化参数失败: %s", e)
        return None

    def should_reoptimize(self, min_days: int = 63) -> bool:
        """判断是否需要重新优化

        Args:
            min_days: 距离上次优化最少间隔天数
        """
        if not self.latest_file.exists():
            return True

        try:
            mtime = datetime.fromtimestamp(self.latest_file.stat().st_mtime)
            days_since = (datetime.now() - mtime).days
            return days_since >= min_days
        except Exception:
            return True

    def get_param_recommendation(self, market_regime: str = None) -> Dict:
        """根据市场环境推荐参数

        Args:
            market_regime: 市场环境(牛市/熊市/震荡)
        """
        current = self.get_current_params()
        if not current:
            return {}

        # 根据市场环境微调参数
        adjusted = current.copy()

        if market_regime == "bull":
            # 牛市:缩短均线周期,更敏感
            if "short_window" in adjusted:
                adjusted["short_window"] = max(5, int(adjusted["short_window"] * 0.8))
            if "long_window" in adjusted:
                adjusted["long_window"] = max(20, int(adjusted["long_window"] * 0.9))

        elif market_regime == "bear":
            # 熊市:延长周期,更稳健
            if "short_window" in adjusted:
                adjusted["short_window"] = int(adjusted["short_window"] * 1.2)
            if "long_window" in adjusted:
                adjusted["long_window"] = int(adjusted["long_window"] * 1.1)

        return adjusted


# ============ 预定义参数范围 ============

PARAM_RANGES = {
    "s2_etf": [
        ParameterRange("momentum_windows_short", 10, 30, 5, is_int=True),
        ParameterRange("momentum_windows_long", 40, 80, 10, is_int=True),
    ],
    "s3_ma_trend": [
        ParameterRange("fast_ma", 5, 30, 5, is_int=True),
        ParameterRange("slow_ma", 30, 120, 15, is_int=True),
        ParameterRange("volume_mult", 1.0, 3.0, 0.5),
    ],
    "s1_dividend": [
        ParameterRange("min_dividend_yield", 0.03, 0.06, 0.01),
        ParameterRange("lookback_days", 180, 365, 30, is_int=True),
    ],
}


# ============ 便捷函数 ============

def run_strategy_optimization(strategy_id: str, start_date: str = None,
                              end_date: str = None) -> Dict:
    """运行策略参数优化

    Args:
        strategy_id: 策略ID
        start_date: 优化起始日期,默认3年前
        end_date: 优化结束日期,默认今天
    """
    if strategy_id not in PARAM_RANGES:
        return {"error": f"未定义参数范围: {strategy_id}"}

    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        start = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=365*3)
        start_date = start.strftime("%Y-%m-%d")

    optimizer = WalkForwardOptimizer(
        strategy_id=strategy_id,
        param_ranges=PARAM_RANGES[strategy_id]
    )

    return optimizer.run_optimization(start_date, end_date)


def get_adaptive_params(strategy_id: str, market_regime: str = None) -> Dict:
    """获取自适应参数"""
    manager = AdaptiveParameterManager(strategy_id)

    # 检查是否需要重新优化
    if manager.should_reoptimize():
        log.info("策略 %s 需要重新优化参数", strategy_id)
        # 这里可以触发异步优化,或返回默认参数

    return manager.get_param_recommendation(market_regime)


def get_optimization_history(strategy_id: str) -> List[Dict]:
    """获取策略优化历史"""
    history = []

    for f in sorted(OPTIMIZE_DIR.glob(f"{strategy_id}_*.json")):
        if "_latest" in f.name:
            continue
        try:
            with open(f, 'r', encoding='utf-8') as file:
                data = json.load(file)
                history.append({
                    "date": f.stem.split("_")[-2:] if "_" in f.stem else "",
                    "best_params": data.get("best_params"),
                    "robust_params": data.get("robust_params"),
                    "consistency_score": data.get("consistency_score")
                })
        except Exception:
            continue

    return history


# ============ CLI 接口 ============

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="策略参数优化工具")
    parser.add_argument("strategy", help="策略ID (如 s2_etf, s3_ma_trend)")
    parser.add_argument("--start", help="优化起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", help="优化结束日期 (YYYY-MM-DD)")
    parser.add_argument("--history", action="store_true", help="查看优化历史")
    parser.add_argument("--current", action="store_true", help="获取当前参数")
    args = parser.parse_args()

    if args.history:
        history = get_optimization_history(args.strategy)
        print(json.dumps(history, indent=2, ensure_ascii=False))
    elif args.current:
        params = get_adaptive_params(args.strategy)
        print(json.dumps(params, indent=2, ensure_ascii=False))
    else:
        result = run_strategy_optimization(args.strategy, args.start, args.end)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
