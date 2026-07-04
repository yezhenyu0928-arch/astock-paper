# -*- coding: utf-8 -*-
"""策略共享助手(非冻结)。持仓数随资金自适应、权重计算等。SPEC_FILL F2.2。"""
import math


def effective_hold_n(hold_n, capital, cfg, sid):
    """effective_hold_n = min(registry.hold_n, floor(capital×0.98/min_ticket));
    custom.hold_n_override[sid] 可手动锁定(仍受上限约束)。"""
    custom = cfg.get("custom", {}) or {}
    min_ticket = custom.get("min_ticket", 8000)
    cap_limit = int(math.floor(capital * 0.98 / min_ticket)) if min_ticket else hold_n
    eff = min(hold_n, max(1, cap_limit))
    override = (custom.get("hold_n_override") or {}).get(sid)
    if override:
        eff = min(int(override), max(1, cap_limit))
    return max(1, eff)


def target_weight(eff_hold_n, buffer=0.98):
    """等权目标权重(留 2% 现金缓冲)。"""
    return round(buffer / eff_hold_n, 6)


def returns_over(ctx, code, windows):
    """各窗口收益率 {w: r};数据不足的窗口返回 None。r_w = close[-1]/close[-(w+1)]-1(后复权)。"""
    maxw = max(windows)
    c = ctx.close(code, maxw + 1)
    out = {}
    for w in windows:
        if len(c) >= w + 1 and c[-(w + 1)]:
            out[w] = c[-1] / c[-(w + 1)] - 1
        else:
            out[w] = None
    return out
