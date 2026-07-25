# -*- coding: utf-8 -*-
"""strategy_optimize.py 冒烟测试(卡L.3)。

背景:strategy_optimize.py 的 WalkForwardOptimizer.optimize_window() 原以
start_date=/end_date= 关键字调用 backtest.run_backtest(sid, start, end, ...),
形参名不符,必抛 TypeError(全仓库无调用方无测试,死代码)。本测试只做"函数可调用性"级冒烟:
断言修复后的调用不再抛 TypeError。当前本机库正在重建(见 docs/OPTIMIZE_V4.md 第一节问题2),
数据不足导致的业务性失败(如空结果/其它异常)一律捕获后视为通过并打印说明——本测试不对回测
结果的正确性做任何断言。

可直接运行: python tests/test_optimize_smoke.py"""
import os
import sys
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from strategy_optimize import WalkForwardOptimizer, WalkForwardWindow, ParameterRange

SID = "s2_etf@v1"
# 极短窗口(按任务要求):2025-06-01 ~ 2025-09-01,内部切一段训练/测试
WINDOW = WalkForwardWindow(
    train_start="2025-06-01", train_end="2025-07-15",
    test_start="2025-07-16", test_end="2025-09-01",
    window_id=0,
)


def test_optimize_window_no_typeerror():
    """optimize_window 内部两处 run_backtest 调用不再因 start_date=/end_date= 关键字不符抛 TypeError。
    只给单一参数组合(min=max)以保持冒烟测试快速——仍走真实的 optimize_window 代码路径。"""
    optimizer = WalkForwardOptimizer(
        strategy_id=SID,
        param_ranges=[ParameterRange("momentum_windows_short", 20, 20, 5, is_int=True)],
    )
    assert len(optimizer.param_combinations) == 1, \
        f"参数组合数应为1(冒烟测试保持最小),实际={len(optimizer.param_combinations)}"

    try:
        results = optimizer.optimize_window(WINDOW, top_n=5)
        print(f"[INFO] optimize_window 正常返回,结果数={len(results)}"
              f"(本机库现处于重建中,不对业务结果正确性做断言)")
    except TypeError as e:
        raise AssertionError(
            f"run_backtest 调用签名回归——optimize_window 内部仍以不符签名的关键字调用: {e}") from e
    except Exception as e:
        # 数据不足/库重建中导致的业务性失败,按任务要求捕获后视为通过
        print(f"[INFO] 业务性失败(库数据不足等,视为通过): {type(e).__name__}: {e}")

    print("[PASS] test_optimize_window_no_typeerror")


def test_run_backtest_direct_kwargs():
    """直接复现修复点:run_backtest(sid, start=..., end=..., param_override=...) 关键字均合法。
    独立于 optimize_window,作为签名修复的最小反例复核(不共享同一段调用代码)。"""
    from backtest import run_backtest
    try:
        run_backtest(SID, start=WINDOW.train_start, end=WINDOW.train_end,
                     param_override={"momentum_windows_short": 20})
        print("[INFO] run_backtest(start=,end=,param_override=) 关键字调用正常完成")
    except TypeError as e:
        raise AssertionError(f"run_backtest 关键字签名不符: {e}") from e
    except Exception as e:
        print(f"[INFO] 业务性失败(库数据不足等,视为通过): {type(e).__name__}: {e}")
    print("[PASS] test_run_backtest_direct_kwargs")


def _run_all():
    fns = [test_optimize_window_no_typeerror, test_run_backtest_direct_kwargs]
    ok = 0
    for fn in fns:
        try:
            fn()
            ok += 1
        except Exception as e:
            print(f"[FAIL] {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\noptimize smoke 测试: {ok}/{len(fns)} 通过")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
