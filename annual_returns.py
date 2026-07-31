# -*- coding: utf-8 -*-
"""逐年收益统计：单进程顺序跑(ProcessPoolExecutor 在本环境会卡死,故改顺序)。
分析窗口 2022-01-01 ~ 2026-07-30: 覆盖 2022 熊 + 2023-25 牛 + 2026 至今,
与你最初验证口径一致,也避开 2016-2019 预热/未交易段。
产物: results_annual.json (汇总) + results_annual_parts/<sid>.json (每策略独立,可断点续跑)。
"""
import os, json, time, shutil
import numpy as np


def _native(o):
    """递归把 numpy 类型转原生, 避免 json.dump 抛 TypeError。"""
    if isinstance(o, dict):
        return {k: _native(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_native(v) for v in o]
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return _native(o.tolist())
    return o


HERE = os.path.dirname(os.path.abspath(__file__))
PARTS = os.path.join(HERE, "results_annual_parts")
os.makedirs(PARTS, exist_ok=True)
FINAL = os.path.join(HERE, "results_annual.json")

START = "2022-01-01"
END = "2026-07-30"

SIDS = [
    "s26_microcap@v1",
    "s27_dividend_lowvol@v1",
    "s29_smallcap_select@v1",
    "s32_roe_quality@v1",
    "s37_earnings_accel@v1",
    "s42_sue_enriched@v1",
    "s53_all_a_momentum_smallcap@v1",
]


def _compute_one(sid):
    """跑单只回测, 按自然年切分收益, 返回结果 dict。"""
    import backtest
    t = time.time()
    r = backtest.run_backtest(sid, START, END, capital=100000)
    dates = r.get('dates') or []
    navs = r.get('navs') or []
    bench = r.get('bench_navs') or []
    yf, yl, bf, bl = {}, {}, {}, {}
    for d, n in zip(dates, navs):
        y = str(d)[:4]
        if y not in yf:
            yf[y] = n
        yl[y] = n
    for d, b in zip(dates, bench):
        y = str(d)[:4]
        if y not in bf:
            bf[y] = b
        bl[y] = b
    yrs = sorted(yf)
    ann = {y: (round((yl[y] / yf[y] - 1) * 100, 1) if yf[y] and yf[y] > 0 else None) for y in yrs}
    bann = {y: (round((bl[y] / bf[y] - 1) * 100, 1) if bf.get(y) and bf[y] > 0 else None) for y in yrs}
    m = r.get('metrics', {}) or {}
    return {
        'annual': ann,
        'bench_annual': bann,
        'met': {k: m.get(k) for k in ('annual', 'max_dd', 'calmar', 'sharpe', 'total')},
        'secs': round(time.time() - t),
        'start': START, 'end': END,
    }


def _load_done():
    done = {}
    for fn in os.listdir(PARTS):
        if fn.endswith(".json") and fn[:-5] in SIDS:
            try:
                with open(os.path.join(PARTS, fn), encoding="utf-8") as f:
                    d = json.load(f)
                if d.get('start') == START and d.get('end') == END:
                    done[fn[:-5]] = d
            except Exception:
                pass
    return done


def main():
    # 清掉旧窗口的 part(窗口已改, 避免混用)
    for fn in os.listdir(PARTS):
        if fn.endswith(".json"):
            try:
                os.remove(os.path.join(PARTS, fn))
            except Exception:
                pass

    done = _load_done()
    pending = [s for s in SIDS if s not in done]
    print(f"[init] 待跑 {len(pending)}/{len(SIDS)}: {pending}", flush=True)

    for sid in pending:
        try:
            out = _compute_one(sid)
            with open(os.path.join(PARTS, sid + ".json"), "w", encoding="utf-8") as f:
                json.dump(_native(out), f, indent=2, ensure_ascii=False)
            done[sid] = out
            print(f"[ok] {sid} secs={out['secs']} years={out['annual']}", flush=True)
        except Exception as e:
            import traceback
            print(f"[FAIL] {sid} err={e}\n{traceback.format_exc()[:600]}", flush=True)

    with open(FINAL, "w", encoding="utf-8") as f:
        json.dump(_native(done), f, indent=2, ensure_ascii=False)
    print(f"[ALL DONE] {len(done)}/{len(SIDS)} -> {FINAL}", flush=True)


if __name__ == "__main__":
    main()
