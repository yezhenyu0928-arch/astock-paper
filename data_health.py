# -*- coding: utf-8 -*-
"""数据源健康度监控模块。

功能:
1. 追踪各数据源的成功率和响应时间
2. 自动数据源优先级调整(基于健康度评分)
3. 数据源故障告警
4. 数据质量校验(价格异常、缺失检测)

健康度评分算法:
- 成功率权重 60%: 最近N次调用的成功比例
- 响应时间权重 25%: 平均响应时间与基准的比值
- 数据完整性权重 15%: 返回数据的有效字段比例

评分 >= 0.8: 健康(优先使用)
评分 0.5-0.8: 亚健康(降级使用)
评分 < 0.5: 故障(停用并告警)
"""
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any
from collections import deque
from pathlib import Path
import statistics

log = logging.getLogger("data_health")

# 健康度持久化文件
HEALTH_STATE_FILE = Path(__file__).parent / "state" / "data_source_health.json"

# 评分阈值
HEALTHY_THRESHOLD = 0.8   # 健康线
DEGRADED_THRESHOLD = 0.5  # 故障线

# 历史窗口大小
HISTORY_WINDOW = 50  # 保留最近50次调用记录


@dataclass
class CallRecord:
    """单次调用记录"""
    timestamp: float
    success: bool
    response_time: float  # 秒
    rows_returned: int = 0
    rows_expected: int = 0
    error_msg: str = ""


@dataclass
class SourceHealth:
    """单个数据源健康状态"""
    name: str
    records: deque = field(default_factory=lambda: deque(maxlen=HISTORY_WINDOW))
    total_calls: int = 0
    total_success: int = 0
    disabled: bool = False  # 手动禁用标记
    disabled_until: float = 0  # 自动禁用截止时间

    def add_record(self, success: bool, response_time: float,
                   rows_returned: int = 0, rows_expected: int = 0,
                   error_msg: str = ""):
        """添加调用记录"""
        record = CallRecord(
            timestamp=time.time(),
            success=success,
            response_time=response_time,
            rows_returned=rows_returned,
            rows_expected=rows_expected,
            error_msg=error_msg
        )
        self.records.append(record)
        self.total_calls += 1
        if success:
            self.total_success += 1

    @property
    def success_rate(self) -> float:
        """最近窗口成功率"""
        if not self.records:
            return 1.0  # 无记录时默认健康
        recent_success = sum(1 for r in self.records if r.success)
        return recent_success / len(self.records)

    @property
    def avg_response_time(self) -> float:
        """平均响应时间"""
        if not self.records:
            return 0.5  # 默认值500ms
        times = [r.response_time for r in self.records if r.success]
        return statistics.mean(times) if times else 1.0

    @property
    def completeness_rate(self) -> float:
        """数据完整性率"""
        if not self.records:
            return 1.0
        valid_records = [r for r in self.records if r.success and r.rows_expected > 0]
        if not valid_records:
            return 1.0
        completeness = [min(r.rows_returned / r.rows_expected, 1.0)
                       for r in valid_records]
        return statistics.mean(completeness)

    @property
    def health_score(self) -> float:
        """综合健康度评分 0-1"""
        if self.disabled:
            return 0.0
        if time.time() < self.disabled_until:
            return 0.0

        # 成功率权重 60%
        score = self.success_rate * 0.6

        # 响应时间权重 25% (基准1秒,越快越好)
        response_score = max(0, 1 - self.avg_response_time)
        score += response_score * 0.25

        # 完整性权重 15%
        score += self.completeness_rate * 0.15

        return round(score, 3)

    @property
    def status(self) -> str:
        """健康状态描述"""
        score = self.health_score
        if score >= HEALTHY_THRESHOLD:
            return "healthy"
        elif score >= DEGRADED_THRESHOLD:
            return "degraded"
        else:
            return "unhealthy"

    def auto_disable(self, duration: int = 300):
        """自动禁用指定秒数(默认5分钟)"""
        self.disabled_until = time.time() + duration
        log.warning("数据源 %s 健康度过低,自动禁用 %d 秒", self.name, duration)


class DataSourceHealthMonitor:
    """数据源健康度监控器"""

    def __init__(self):
        self.sources: Dict[str, SourceHealth] = {}
        self._load_state()

    def _load_state(self):
        """从文件加载状态"""
        if HEALTH_STATE_FILE.exists():
            try:
                with open(HEALTH_STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for name, sdata in data.get('sources', {}).items():
                    health = SourceHealth(name=name)
                    health.total_calls = sdata.get('total_calls', 0)
                    health.total_success = sdata.get('total_success', 0)
                    health.disabled = sdata.get('disabled', False)
                    health.disabled_until = sdata.get('disabled_until', 0)
                    self.sources[name] = health
                log.info("加载数据源健康状态: %d 个源", len(self.sources))
            except Exception as e:
                log.warning("加载健康状态失败: %s", e)

    def _save_state(self):
        """保存状态到文件"""
        try:
            HEALTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'sources': {},
                'saved_at': time.time()
            }
            for name, health in self.sources.items():
                data['sources'][name] = {
                    'total_calls': health.total_calls,
                    'total_success': health.total_success,
                    'disabled': health.disabled,
                    'disabled_until': health.disabled_until,
                    'health_score': health.health_score,
                    'status': health.status
                }
            with open(HEALTH_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("保存健康状态失败: %s", e)

    def get_health(self, source_name: str) -> SourceHealth:
        """获取或创建数据源健康状态"""
        if source_name not in self.sources:
            self.sources[source_name] = SourceHealth(name=source_name)
        return self.sources[source_name]

    def record_call(self, source_name: str, success: bool,
                    response_time: float, rows_returned: int = 0,
                    rows_expected: int = 0, error_msg: str = ""):
        """记录一次数据源调用"""
        health = self.get_health(source_name)
        health.add_record(success, response_time, rows_returned,
                         rows_expected, error_msg)

        # 健康度过低时自动禁用
        if health.health_score < DEGRADED_THRESHOLD and not health.disabled:
            consecutive_failures = sum(1 for r in list(health.records)[-5:]
                                     if not r.success)
            if consecutive_failures >= 3:  # 连续3次失败才禁用
                health.auto_disable(duration=300)  # 禁用5分钟

        self._save_state()

    def get_priority_order(self, source_names: List[str]) -> List[str]:
        """根据健康度评分返回优先级排序"""
        scored = []
        for name in source_names:
            health = self.get_health(name)
            scored.append((name, health.health_score))

        # 按健康度降序排列
        scored.sort(key=lambda x: x[1], reverse=True)
        return [name for name, _ in scored]

    def get_health_report(self) -> Dict:
        """生成健康度报告"""
        report = {
            'timestamp': time.time(),
            'sources': {}
        }
        for name, health in self.sources.items():
            report['sources'][name] = {
                'health_score': health.health_score,
                'status': health.status,
                'success_rate': health.success_rate,
                'avg_response_time': health.avg_response_time,
                'completeness_rate': health.completeness_rate,
                'total_calls': health.total_calls,
                'recent_calls': len(health.records),
                'disabled': health.disabled or time.time() < health.disabled_until
            }
        return report

    def reset_source(self, source_name: str):
        """重置指定数据源状态"""
        if source_name in self.sources:
            self.sources[source_name] = SourceHealth(name=source_name)
            self._save_state()
            log.info("重置数据源 %s 健康状态", source_name)


# 全局监控器实例
_health_monitor: Optional[DataSourceHealthMonitor] = None


def get_monitor() -> DataSourceHealthMonitor:
    """获取全局健康度监控器"""
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = DataSourceHealthMonitor()
    return _health_monitor


def monitored_call(source_name: str, fn: Callable, *args, **kwargs) -> Any:
    """包装函数调用,自动监控健康度

    用法:
        result = monitored_call("baostock", fetch_daily, code, start, end)
    """
    monitor = get_monitor()
    start_time = time.time()

    try:
        result = fn(*args, **kwargs)
        elapsed = time.time() - start_time

        # 计算返回数据行数
        rows = 0
        expected = 0
        if result is not None:
            if hasattr(result, '__len__'):
                rows = len(result)
            if hasattr(result, 'empty'):
                rows = 0 if result.empty else len(result)

        # 估算预期数据行数(交易日)
        if 'start' in kwargs and 'end' in kwargs:
            from data_adapter import fetch_calendar
            try:
                cal = fetch_calendar(kwargs['start'], kwargs['end'])
                expected = len(cal)
            except:
                pass

        success = result is not None
        if hasattr(result, 'empty'):
            success = success and not result.empty

        monitor.record_call(source_name, success, elapsed, rows, expected)
        return result

    except Exception as e:
        elapsed = time.time() - start_time
        monitor.record_call(source_name, False, elapsed, 0, 0, str(e))
        raise


# ============ 数据质量校验 ============

class DataQualityChecker:
    """数据质量校验器"""

    @staticmethod
    def check_price_anomalies(df, code: str) -> List[Dict]:
        """检查价格异常

        检测:
        1. 单日涨跌幅超过15%(正常A股限幅10%,科创板/创业板20%)
        2. 价格跳空超过前收±15%
        3. 成交量为0但价格变动(异常)
        4. 高开/低开超过10%
        """
        anomalies = []
        if df is None or df.empty:
            return anomalies

        required_cols = ['trade_date', 'open', 'high', 'low', 'close', 'volume']
        if not all(c in df.columns for c in required_cols):
            return anomalies

        df = df.sort_values('trade_date').reset_index(drop=True)

        for i in range(len(df)):
            row = df.iloc[i]
            date = row['trade_date']

            # 检查价格是否为正
            if row['close'] <= 0 or row['open'] <= 0:
                anomalies.append({
                    'date': date,
                    'type': 'invalid_price',
                    'message': f'价格异常: close={row["close"]}, open={row["open"]}',
                    'severity': 'error'
                })
                continue

            # 检查涨跌幅
            if i > 0:
                prev_close = df.iloc[i-1]['close']
                change_pct = abs(row['close'] / prev_close - 1)

                if change_pct > 0.20:  # 超过20%
                    anomalies.append({
                        'date': date,
                        'type': 'extreme_change',
                        'message': f'极端涨跌幅: {change_pct:.1%}',
                        'severity': 'warning'
                    })
                elif change_pct > 0.15:  # 超过15%
                    anomalies.append({
                        'date': date,
                        'type': 'large_change',
                        'message': f'较大涨跌幅: {change_pct:.1%}',
                        'severity': 'info'
                    })

                # 检查跳空
                gap_up = row['open'] / prev_close - 1
                gap_down = 1 - row['open'] / prev_close

                if gap_up > 0.10:
                    anomalies.append({
                        'date': date,
                        'type': 'gap_up',
                        'message': f'大幅高开: {gap_up:.1%}',
                        'severity': 'info'
                    })
                elif gap_down > 0.10:
                    anomalies.append({
                        'date': date,
                        'type': 'gap_down',
                        'message': f'大幅低开: {gap_down:.1%}',
                        'severity': 'info'
                    })

            # 检查成交量异常
            if row['volume'] == 0 and abs(row['close'] - row['open']) / row['open'] > 0.01:
                anomalies.append({
                    'date': date,
                    'type': 'volume_price_mismatch',
                    'message': '成交量为0但价格变动',
                    'severity': 'warning'
                })

            # 检查OHLC合理性
            if row['low'] > row['high']:
                anomalies.append({
                    'date': date,
                    'type': 'ohlc_error',
                    'message': f'low({row["low"]}) > high({row["high"]})',
                    'severity': 'error'
                })

            if row['close'] > row['high'] or row['close'] < row['low']:
                anomalies.append({
                    'date': date,
                    'type': 'close_out_of_range',
                    'message': f'收盘价超出高低点范围',
                    'severity': 'error'
                })

        return anomalies

    @staticmethod
    def check_data_gaps(df, expected_dates: List[str]) -> List[Dict]:
        """检查数据缺失

        Args:
            df: 实际数据DataFrame
            expected_dates: 期望的交易日列表
        """
        gaps = []
        if df is None or df.empty:
            return [{'type': 'total_missing', 'message': '数据完全缺失', 'severity': 'error'}]

        actual_dates = set(df['trade_date'].astype(str))
        expected_set = set(expected_dates)

        missing = expected_set - actual_dates
        if missing:
            # 连续缺失检测
            missing_sorted = sorted(missing)
            gaps.append({
                'type': 'missing_dates',
                'count': len(missing),
                'dates': list(missing)[:10],  # 只显示前10个
                'message': f'缺失 {len(missing)} 个交易日数据',
                'severity': 'error' if len(missing) > 5 else 'warning'
            })

        return gaps

    @staticmethod
    def validate_daily_data(df, code: str, start: str, end: str) -> Dict:
        """综合校验日线数据

        Returns:
            {
                'valid': bool,
                'anomalies': List[Dict],
                'gaps': List[Dict],
                'stats': Dict
            }
        """
        from data_adapter import fetch_calendar

        result = {
            'valid': True,
            'anomalies': [],
            'gaps': [],
            'stats': {}
        }

        if df is None or df.empty:
            result['valid'] = False
            result['gaps'] = [{'type': 'total_missing', 'message': '无数据', 'severity': 'error'}]
            return result

        # 基础统计
        result['stats'] = {
            'total_rows': len(df),
            'date_range': f"{df['trade_date'].min()} ~ {df['trade_date'].max()}",
            'null_count': df.isnull().sum().sum()
        }

        # 价格异常检测
        result['anomalies'] = DataQualityChecker.check_price_anomalies(df, code)

        # 数据缺失检测
        try:
            cal = fetch_calendar(start, end)
            expected_dates = cal['cal_date'].tolist()
            result['gaps'] = DataQualityChecker.check_data_gaps(df, expected_dates)
        except Exception as e:
            log.warning("获取交易日历失败: %s", e)

        # 判断有效性
        critical_errors = [a for a in result['anomalies'] if a.get('severity') == 'error']
        critical_gaps = [g for g in result['gaps'] if g.get('severity') == 'error']

        if critical_errors or critical_gaps:
            result['valid'] = False

        return result


# 便捷函数
def check_data_source_health() -> Dict:
    """检查所有数据源健康状态"""
    monitor = get_monitor()
    return monitor.get_health_report()


def reset_data_source(source_name: str):
    """重置指定数据源"""
    monitor = get_monitor()
    monitor.reset_source(source_name)
