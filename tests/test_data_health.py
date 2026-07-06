# -*- coding: utf-8 -*-
"""测试数据源健康度监控模块(data_health.py)。
覆盖: 单次/多次调用记录、健康评分逻辑、自动禁用、数据质量校验。
纯内存测试,无需联网。
"""
import os, sys, io, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
except Exception:
    pass

import time
import data_health as dh


# ----------------------------------------------------------------
# Test 1: 健康评分从完美到故障
# ----------------------------------------------------------------
def test_health_score_perfect():
    """完美记录:成功率100%,低延迟,完整度100% → score≥0.95"""
    health = dh.SourceHealth(name="perfect")
    for _ in range(10):
        health.add_record(success=True, response_time=0.1,
                          rows_returned=100, rows_expected=100)
    assert health.health_score >= 0.95, f"perfect score={health.health_score}"
    assert health.status == "healthy"
    print(f"[PASS] perfect health score={health.health_score}")


def test_health_score_unhealthy():
    """全部失败记录 → score=0"""
    health = dh.SourceHealth(name="unhealthy")
    for _ in range(10):
        health.add_record(success=False, response_time=5.0, error_msg="timeout")
    assert health.health_score < dh.DEGRADED_THRESHOLD, f"unhealthy score={health.health_score}"
    assert health.status == "unhealthy"
    print(f"[PASS] unhealthy health score={health.health_score}")


def test_health_score_mixed():
    """一半成功一半失败 → degraded"""
    health = dh.SourceHealth(name="mixed")
    for i in range(10):
        health.add_record(success=(i % 2 == 0),
                          response_time=0.5,
                          rows_returned=100, rows_expected=100)
    score = health.health_score
    assert dh.DEGRADED_THRESHOLD <= score < dh.HEALTHY_THRESHOLD, f"mixed score={score}"
    assert health.status == "degraded"
    print(f"[PASS] mixed health score={score} status={health.status}")


def test_slow_response_penalty():
    """慢响应降低评分"""
    health = dh.SourceHealth(name="slow")
    for _ in range(10):
        health.add_record(success=True, response_time=3.0,     # 3秒响应
                          rows_returned=100, rows_expected=100)
    assert health.health_score < 0.9, f"slow score={health.health_score}"
    assert health.avg_response_time >= 2.5
    print(f"[PASS] slow response score={health.health_score} avg={health.avg_response_time:.2f}s")


# ----------------------------------------------------------------
# Test 2: 监控器集成——优先级排序
# ----------------------------------------------------------------
def test_monitor_priority_order():
    """健康度越高越靠前"""
    mon = dh.DataSourceHealthMonitor()

    # 通过 record_call 注入不同质量的记录
    names = ["best", "good", "bad"]
    for name in names:
        for _ in range(5):
            mon.record_call(name, success=True, response_time=0.3,
                            rows_returned=100, rows_expected=100)

    # 将 bad 标记为失败
    for _ in range(10):
        mon.record_call("bad", success=False, response_time=5.0, error_msg="fail")

    ordered = mon.get_priority_order(names)
    assert len(ordered) == 3
    assert ordered[0] != "bad", f"bad should not be first: {ordered}"
    print(f"[PASS] priority order: {ordered}")


def test_monitor_report():
    """健康度报告结构正确"""
    mon = dh.DataSourceHealthMonitor()
    mon.record_call("test_a", success=True, response_time=0.2,
                    rows_returned=50, rows_expected=50)
    mon.record_call("test_a", success=False, response_time=1.0, error_msg="err")

    report = mon.get_health_report()
    assert "sources" in report
    assert "test_a" in report["sources"]
    s = report["sources"]["test_a"]
    for key in ("health_score", "status", "success_rate", "avg_response_time",
                "completeness_rate", "total_calls"):
        assert key in s, f"missing key {key}"
    print(f"[PASS] report: {json.dumps(s, ensure_ascii=False)}")


# ----------------------------------------------------------------
# Test 3: 价格异常检测
# ----------------------------------------------------------------
def test_check_price_anomalies():
    import pandas as pd

    # 正常数据
    df_ok = pd.DataFrame({
        "trade_date": ["2024-01-01", "2024-01-02", "2024-01-03"],
        "open":  [10.0, 10.5, 10.3],
        "high":  [10.6, 10.8, 10.5],
        "low":   [9.9, 10.2, 10.0],
        "close": [10.5, 10.3, 10.2],
        "volume":[100000, 120000, 90000],
    })
    anoms = dh.DataQualityChecker.check_price_anomalies(df_ok, "test")
    assert len(anoms) == 0, f"正常数据不应有异常: {anoms}"
    print(f"[PASS] no anomalies on clean data")

    # 异常数据: 价格为零
    df_bad = pd.DataFrame({
        "trade_date": ["2024-01-01"],
        "open":  [0.0],
        "high":  [10.0],
        "low":   [9.0],
        "close": [0.0],
        "volume":[100],
    })
    anoms = dh.DataQualityChecker.check_price_anomalies(df_bad, "bad")
    assert len(anoms) > 0, "异常数据应检出"
    assert any(a["severity"] == "error" for a in anoms), "应有error级别"
    print(f"[PASS] detected anomalies: {[a['type'] for a in anoms]}")

    # 数据: 极端涨跌超20%
    df_ext = pd.DataFrame({
        "trade_date": ["2024-01-01", "2024-01-02"],
        "open":  [10.0, 10.0],
        "high":  [10.5, 13.0],
        "low":   [9.5, 9.0],
        "close": [10.0, 12.5],  # 25%涨幅
        "volume":[100000, 200000],
    })
    anoms = dh.DataQualityChecker.check_price_anomalies(df_ext, "ext")
    assert len(anoms) > 0
    print(f"[PASS] extreme change: {[a['type'] for a in anoms]}")

    # 数据: OHLC矛盾(low>high)
    df_ohlc = pd.DataFrame({
        "trade_date": ["2024-01-01", "2024-01-02"],
        "open": [10.0, 10.0],
        "high": [9.5, 10.0],     # high < low!
        "low":  [10.5, 10.0],
        "close":[10.0, 10.0],
        "volume":[100, 100],
    })
    anoms = dh.DataQualityChecker.check_price_anomalies(df_ohlc, "ohlc")
    assert any(a["type"] == "ohlc_error" for a in anoms)
    print(f"[PASS] OHLC error detected")


# ----------------------------------------------------------------
# Test 4: 集成校验 validate_daily_data
# ----------------------------------------------------------------
def test_validate_daily_data():
    import pandas as pd

    df = pd.DataFrame({
        "trade_date": ["2024-01-02", "2024-01-03"],
        "open":  [10.0, 10.5],
        "high":  [10.5, 10.8],
        "low":   [9.9, 10.3],
        "close": [10.3, 10.5],
        "volume":[100000, 120000],
    })
    result = dh.DataQualityChecker.validate_daily_data(
        df, "test", "2024-01-02", "2024-01-03"
    )
    assert "stats" in result
    assert result["stats"]["total_rows"] == 2
    print(f"[PASS] validate_daily_data stats={result['stats']} valid={result['valid']}")


# ----------------------------------------------------------------
# Test 5: 自动禁用恢复
# ----------------------------------------------------------------
def test_auto_disable_and_recovery():
    health = dh.SourceHealth(name="auto")
    health.auto_disable(duration=0.1)  # 禁用 0.1 秒
    assert health.health_score == 0.0, "禁用中评分应为0"

    time.sleep(0.15)
    # 禁用到期后恢复
    assert health.health_score > 0, f"恢复后评分应>0: {health.health_score}"
    print(f"[PASS] auto disable/recovery score={health.health_score}")


# ================================================================
def _run_all():
    fns = [
        test_health_score_perfect,
        test_health_score_unhealthy,
        test_health_score_mixed,
        test_slow_response_penalty,
        test_monitor_priority_order,
        test_monitor_report,
        test_check_price_anomalies,
        test_validate_daily_data,
        test_auto_disable_and_recovery,
    ]
    ok = 0
    for fn in fns:
        try:
            fn()
            ok += 1
        except Exception as e:
            print(f"[FAIL] {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n数据健康度测试: {ok}/{len(fns)} 通过")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
