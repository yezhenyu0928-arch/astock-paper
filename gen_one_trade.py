# -*- coding: utf-8 -*-
"""单策略回测成交生成: python gen_one_trade.py <sid> [END]
生成 reports/<sid>_trades.csv,与 generate_trades() 读取路径一致。
"""
import sys
import conf
import backtest

sid = sys.argv[1]
END = sys.argv[2] if len(sys.argv) > 2 else "2026-07-30"
out = str(conf.REPORTS_DIR / (sid.replace("@", "_at_") + "_trades.csv"))
backtest.run_backtest(sid, "2022-01-01", END, trades_out=out)
print("DONE", sid, "->", out)
