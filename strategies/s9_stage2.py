# -*- coding: utf-8 -*-
"""S9 Stage2 趋势模板(卡M.3,OPTIMIZE_V4.md;finance-skills sepa-strategy Minervini SEPA 转译)。
周频(每周最后交易日,降低whipsaw——结构性替代已归档的 s3 日频短均线交叉策略),沪深300池。

硬门槛(全过才候选,daily_bar 直接计算,不依赖 factors.compute_factor_exposures 那套七列 NaN
故障的管线):
  G1 close>MA60>MA120>MA250(均线多头排列,后复权价)
  G2 MA250 较21个交易日前上行
  G3 close≥52周低点×1.30      G4 close≥52周高点×0.75
  G5 20日均成交额≥min_avg_amount(默认5000万,复用现有流动性惯例)
候选内按 RSTR(12-1月动量)池内排名,取 top hold_n(默认6),等权持有。
卖出:跌破 MA120 或 硬门槛失效(周频复核)。

数据只经 ctx.conn 批量 SQL 直查(整池向量化,禁止逐股循环查库)。
防未来函数:一切窗口截至信号日。

本文件不注册 registry.yaml/config.yaml(留给验证批,按"验证完成才冻结"纪律)。"""
import logging
from datetime import datetime, timedelta

import pandas as pd

from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s9")
POOL_INDEX = "sh000300"

WEEK52 = 252              # 52周≈252个交易日(与 s8_checklist 一致)
LOOKBACK_DAYS = 280       # MA250 + 21日前MA250 需要 271 个交易日,留buffer取280


def _to_date_str(d):
    return str(d)[:10]


def _cal_start(date_str, days):
    """date_str 往前推 days 个自然日(不依赖 trade_calendar 表;daily_bar 本就只存交易日行,
    500自然日足以覆盖 LOOKBACK_DAYS 个交易日窗口)。"""
    d = datetime.strptime(_to_date_str(date_str), "%Y-%m-%d") - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _ma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _stage2_stats(closes, amounts):
    """单只标的收盘价序列(升序,已后复权)+成交额序列 → MA60/120/250·MA250(21日前)·
    52周高低点·12-1月动量·20日均成交额。"""
    n = len(closes)
    stat = {
        "close": closes[-1] if n else None,
        "ma60": _ma(closes, 60),
        "ma120": _ma(closes, 120),
        "ma250": _ma(closes, 250),
    }
    if n >= 271:
        stat["ma250_21ago"] = _ma(closes[:-21], 250)     # 截至21个交易日前的250日均线
    else:
        stat["ma250_21ago"] = None
    if n >= WEEK52:
        w = closes[-WEEK52:]
        stat["high52"] = max(w)
        stat["low52"] = min(w)
    else:
        stat["high52"] = None
        stat["low52"] = None
    if n >= 252 and closes[-252]:
        stat["mom_12_1"] = closes[-21] / closes[-252] - 1
    else:
        stat["mom_12_1"] = None
    valid_amt = [a for a in amounts[-20:] if a is not None] if len(amounts) >= 20 else []
    stat["avg_amount20"] = (sum(valid_amt) / len(valid_amt)) if valid_amt else None
    return stat


def _bulk_bars(conn, codes, date, lookback_days=LOOKBACK_DAYS):
    """批量取 codes 截至 date 的后复权收盘价+成交额,一次 SQL 覆盖全池,组内 pandas 派生
    MA60/120/250·MA250(21日前)·52周高低点·12-1月动量·20日均成交额。非逐股查库。"""
    if not codes:
        return pd.DataFrame()
    date = _to_date_str(date)
    start = _cal_start(date, 500)
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, trade_date, close, adj_factor, amount FROM daily_bar "
        f"WHERE code IN ({placeholders}) AND trade_date BETWEEN ? AND ? "
        f"ORDER BY code, trade_date", (*codes, start, date)).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["code", "trade_date", "close", "adj_factor", "amount"])
    df["adj_close"] = df["close"] * df["adj_factor"].fillna(1.0)
    out = {}
    for code, g in df.groupby("code"):
        g = g.sort_values("trade_date")
        out[code] = _stage2_stats(g["adj_close"].tolist(), g["amount"].tolist())
    _ = lookback_days   # 保留参数位供未来调窗(当前 _stage2_stats 内部窗口固定,见 WEEK52/LOOKBACK_DAYS)
    return pd.DataFrame.from_dict(out, orient="index")


def _hard_gate(b, vol_floor):
    """硬门槛(全过才候选)。返回 (passed, fail_tag)——fail_tag 供 reason 摘要,首个失败项。"""
    close, ma60, ma120, ma250 = b.get("close"), b.get("ma60"), b.get("ma120"), b.get("ma250")
    if any(v is None or pd.isna(v) for v in (close, ma60, ma120, ma250)):
        return False, "均线数据不足"
    if not (close > ma60 > ma120 > ma250):
        return False, "均线未多头排列"
    ma250_21ago = b.get("ma250_21ago")
    if ma250_21ago is None or pd.isna(ma250_21ago):
        return False, "MA250历史不足"
    if not (ma250 > ma250_21ago):
        return False, "MA250未上行"
    low52, high52 = b.get("low52"), b.get("high52")
    if low52 is None or high52 is None or pd.isna(low52) or pd.isna(high52):
        return False, "52周数据不足"
    if not (close >= low52 * 1.30):
        return False, "距52周低点不足30%"
    if not (close >= high52 * 0.75):
        return False, "距52周高点回撤超25%"
    avg_amt = b.get("avg_amount20")
    if avg_amt is None or pd.isna(avg_amt) or avg_amt < vol_floor:
        return False, "20日均成交额不足流动性门槛"
    return True, ""


def score_pool(conn, codes, date, vol_floor, lookback_days=LOOKBACK_DAYS):
    """整池硬门槛判定+RSTR动量入口(供策略与测试共用,整池向量化)。
    返回 (gate dict{code:bool}, tag dict{code:str}, mom dict{code:float}(仅gate通过者), bars DataFrame)。"""
    bars = _bulk_bars(conn, codes, date, lookback_days)
    gate, tag, mom = {}, {}, {}
    for code in codes:
        if code not in bars.index:
            gate[code], tag[code] = False, "无数据"
            continue
        b = bars.loc[code]
        passed, fail_tag = _hard_gate(b, vol_floor)
        gate[code], tag[code] = passed, fail_tag
        if passed and pd.notna(b.get("mom_12_1")):
            mom[code] = b["mom_12_1"]
    return gate, tag, mom, bars


class S9Stage2Trend(BaseStrategy):
    """S9 Stage2 趋势模板(周频,沪深300池)。generate_orders 只做编排,硬门槛/批量取数见上方
    模块级函数(可独立单测)。类结构/资金自适应/首建仓与置换逻辑仿照 s1_dividend.py/s6_sector.py。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_week(date):
            return []

        # 宏观面防御:先计算市场regime,风险市清仓观望、不逆势开仓
        try:
            import macro as _macro
            _reg = _macro.compute_market_regime(date, conn=ctx.conn)
            _regime = _reg.get("regime", "震荡")
        except Exception:
            _regime = "震荡"

        hold_n = self.params.get("hold_n", 6)
        vol_floor = self.params.get("min_avg_amount", 50_000_000)
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        pool = ctx.members(POOL_INDEX, date)
        if not pool:
            return []
        held = set(account.positions.keys())
        # 风险regime:清仓全部持仓并观望(顺势不逆势)
        if _regime == "风险":
            return [Order(self.strategy_id, code, "sell", 0.0,
                          f"Stage2趋势:{ctx.name(code)}市场regime=风险,清仓观望", date)
                    for code in held]
        codes = list(dict.fromkeys(list(pool) + list(held)))
        tradable = set(c for c in pool if ctx.is_tradable(c, date))
        if not tradable and not held:
            return []

        gate, tag, mom, bars = score_pool(ctx.conn, codes, date, vol_floor)

        candidates = [c for c in mom if c in tradable]
        ranked = sorted(candidates, key=lambda c: mom[c], reverse=True)
        target = ranked[:eff]
        full_rank = {c: i + 1 for i, c in enumerate(ranked)}

        orders = []
        for code in held:
            if code in target:
                continue
            b = bars.loc[code] if code in bars.index else None
            close = b.get("close") if b is not None else None
            ma120 = b.get("ma120") if b is not None else None
            broke_ma120 = bool(close is not None and ma120 is not None
                               and pd.notna(close) and pd.notna(ma120) and close < ma120)
            gate_fail = not gate.get(code, False)
            if broke_ma120 or gate_fail:
                nm = ctx.name(code)
                if broke_ma120:
                    reason = f"Stage2趋势:{nm}跌破MA120,卖出"
                else:
                    reason = f"Stage2趋势:{nm}硬门槛失效({tag.get(code, '')}),卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                nm = ctx.name(code)
                m = mom[code]
                reason = (f"Stage2趋势:买入{nm}(12-1月动量{m:+.1%},均线多头排列且MA250上行,"
                          f"52周区间达标,20日均成交额达标,候选{len(ranked)}只中第{full_rank[code]})")
                orders.append(Order(self.strategy_id, code, "buy", w, reason, date))
        return orders
