# -*- coding: utf-8 -*-
"""全A亏损根因对照实验: 隔离 新板拖累/冻结豁免/流动性/基准回归 四个变量。"""
import contextlib, io, sys, time
import backtest, conf

START, END, CAP = "2022-01-01", "2025-12-31", 100000

def run(tag, sid, param_override=None, risk_patch=None):
    t0 = time.time()
    orig = conf.load_config
    if risk_patch:
        def patched(*a, **k):
            cfg = orig(*a, **k)
            (cfg.setdefault("risk", {})).update(risk_patch)
            return cfg
        conf.load_config = patched
        backtest.conf.load_config = patched
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = backtest.run_backtest(sid, START, END, capital=CAP,
                                      param_override=param_override)
        m = r.get("metrics", {})
        print("[%s] ann=%.4f dd=%.4f sharpe=%.2f  (%.0fs)" % (
            tag, m.get("annual", 0), m.get("max_dd", 0), m.get("sharpe", 0),
            time.time() - t0), flush=True)
    except Exception as e:
        print("[%s] ERR %s: %s" % (tag, type(e).__name__, str(e)[:200]), flush=True)
    finally:
        if risk_patch:
            conf.load_config = orig
            backtest.conf.load_config = orig

S53 = "s53_all_a_momentum_smallcap@v1"
S42 = "s42_sue_enriched@v1"

# E4 基准回归: s42 主板(应≈16.6%, 验证 risk.py 改动无回归)
run("E4_s42主板基准", S42)
# E1 新板拖累隔离: s53 同配方但池换回主板
run("E1_s53主板池", S53, param_override={"pool_index": "mainboard"})
# E2 冻结豁免副作用: s53 全A + 打开防御降仓
run("E2_s53全A防御", S53, param_override={"regime_downsize": True,
                                          "regime_good": 1.0,
                                          "regime_mid": 0.85,
                                          "regime_bad": 0.60})
# E3 流动性门槛: 全A门槛提到8000万(与主板一致)
run("E3_s53全A高流动", S53, risk_patch={"min_avg_amount_all_a": 80_000_000})
print("ALL DONE", flush=True)
