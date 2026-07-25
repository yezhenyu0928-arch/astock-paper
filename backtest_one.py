# -*- coding: utf-8 -*-
"""单策略主回测 CLI —— 供 search_arms 以**独立子进程**调用, 隔离内存/段错误。

背景(2026-07-24): search_arms 原本在同一进程内连续 import backtest 并调 18~30 次
run_backtest。run_backtest 内部 cache_bars=True 把全市场日线缓存进内存, 多次
调用叠加导致内存暴涨, 最终 C 层段错误(0xC0000005 / 退出码 3221225477) 直接
干掉 search_arms 本身, 连 _arm_search.md 都写不出。

改为子进程后: 每次回测在独立解释器内运行, 进程结束内存自动释放,
且即便某次回测触发段错误也只拖垮子进程, 主进程捕获 stderr 跳过该组合。

用法: python backtest_one.py <sid> <start> <end> <capital>
  - 从环境变量读取 macro 趋势闸阈值覆盖(MACRO_BASELINE / MACRO_BEAR_FLOOR / MACRO_WEAK_MULT / MACRO_MA200_MULT)
  - 成功: 向 stdout 写一行 JSON(metrics dict, 含 annual/max_dd/sharpe 等)
  - 失败: 非零退出码 + stderr 报错
"""
import os
import sys
import json

import backtest
import util


def main():
    sid = sys.argv[1]
    start = sys.argv[2]
    end = sys.argv[3]
    cap = int(sys.argv[4]) if len(sys.argv) > 4 else None
    r = backtest.run_backtest(sid, start, end, capital=cap)
    # 只输出 metrics(易解析); 其余诊断走 logging/stderr
    sys.stdout.write(json.dumps(r["metrics"], ensure_ascii=False))


if __name__ == "__main__":
    main()
