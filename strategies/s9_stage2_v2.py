# -*- coding: utf-8 -*-
"""S9v2 Stage2 成长趋势(借鉴 v1 的 Minervini SEPA Stage2 模板,修复其致命缺陷并补全信息面)。

v1 失败根因(蒙卡5%分位总收益-52.7%,已离线):宇宙锁在沪深300大盘股——大盘价值股极少出现
干净 Stage2 形态,且买点常落在已透支高位,胜率仅19.9%/回撤40%。

v2 相对 v1 的借鉴式重构(非直接使用v1):
  1) 宇宙扩到全A动态池(剔除微盘/壳股:20日均额≥门槛 且 总市值≥下限),真正能找到 Stage2 成长股。
  2) 保留 Stage2 硬门槛签名(close>MA60>MA120>MA250 多头排列 + MA250上行 + 52周区间达标 + 流动性),
     这是"成长趋势经理"的决策逻辑主体。
  3) 补全 v1 缺失的信息面(回应"参考信息不够全面"):
     - 宏观:market_regime='风险' 清仓观望、不开新仓(顺势不逆势);
     - 行业:偏好 top_bullish_sectors 强势行业,行业动量给排名加权;
     - 基本面:轻量质量门槛(PE>0 且 PB>0,剔除亏损/负净资);
     - 资金面:20日均额硬门槛(复用流动性惯例)+ 成交额截面过滤。
  4) 排名:12-1月动量(RSTR)为主,叠加行业动量加权,等权持有 top hold_n。
卖出:跌破 MA120 或硬门槛失效(周频复核)。"""
import logging
from datetime import datetime, timedelta

import pandas as pd

from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s9v2")

WEEK52 = 252              # 52周≈252个交易日
LOOKBACK_DAYS = 280       # MA250 + 21日前MA250 需 271 个交易日,留buffer取280


def _to_date_str(d):
    return str(d)[:10]


def _cal_start(date_str, days):
    d = datetime.strptime(_to_date_str(date_str), "%Y-%m-%d") - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _ma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _stage2_stats(closes, amounts):
    """单只标的后复权收盘价序列+成交额序列 → MA60/120/250·MA250(21日前)·52周高低点·
    12-1月动量·20日均成交额。"""
    n = len(closes)
    stat = {
        "close": closes[-1] if n else None,
        "ma60": _ma(closes, 60),
        "ma120": _ma(closes, 120),
        "ma250": _ma(closes, 250),
    }
    if n >= 271:
        stat["ma250_21ago"] = _ma(closes[:-21], 250)
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
    Stage2 指标。非逐股查库。"""
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
    _ = lookback_days
    return pd.DataFrame.from_dict(out, orient="index")


def _hard_gate(b, vol_floor):
    """Stage2 硬门槛(全过才候选)。返回 (passed, fail_tag)。"""
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
    """整池硬门槛判定 + RSTR 动量入口。返回 (gate, tag, mom, bars)。"""
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


class S9Stage2TrendV2(BaseStrategy):
    """S9v2 Stage2 成长趋势(周频,全A动态池)。generate_orders 只做编排。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_week(date):
            return []

        # ── 宏观面防御 ──
        try:
            import macro as _macro
            _reg = _macro.compute_market_regime(date, conn=ctx.conn).get("regime", "震荡")
        except Exception:
            _reg = "震荡"
        risk_regime = (_reg == "风险")

        hold_n = self.params.get("hold_n", 6)
        vol_floor = self.params.get("min_avg_amount", 30_000_000)
        cap_min = self.params.get("cap_min", 3_000_000_000)   # 剔除微盘/壳股
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        # ── 全A动态宇宙:可交易 + 上市满1年 + 流动性 + 市值下限(剔除微盘) ──
        try:
            all_codes = [r[0] for r in ctx.conn.execute(
                "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]
        except Exception:
            all_codes = []
        univ = []
        for code in all_codes:
            if not ctx.is_tradable(code, date):
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("market_cap") or f["market_cap"] <= cap_min:
                continue
            c = ctx.close(code, 280)
            if len(c) < 271:
                continue
            if ctx.avg_amount(code, 20) < vol_floor:
                continue
            # 轻量基本面门槛:PE>0 且 PB>0(剔除亏损/负净资)
            if not (f.get("pe") and f["pe"] > 0 and f.get("pb") and f["pb"] > 0):
                continue
            univ.append(code)
        if not univ:
            return []
        held = set(account.positions.keys())

        # ── 风险regime:清仓全部持仓并观望 ──
        if risk_regime:
            return [Order(self.strategy_id, code, "sell", 0.0,
                          f"Stage2v2:{ctx.name(code)}市场regime=风险,清仓观望", date)
                    for code in held]

        codes = list(dict.fromkeys(list(univ) + list(held)))
        tradable = set(c for c in univ if ctx.is_tradable(c, date))
        if not tradable and not held:
            return []

        gate, tag, mom, bars = score_pool(ctx.conn, codes, date, vol_floor)

        # ── 行业面:偏好强势行业(信息补全) ──
        bull_sectors = set()
        try:
            import macro as _macro
            for s in _macro.top_bullish_sectors(date, conn=ctx.conn, top=6):
                bull_sectors.add(s.get("name"))
        except Exception:
            pass
        ind_map = {}
        try:
            import factors as _fac
            ind_map = _fac.get_industry(ctx.conn, list(mom.keys()))
        except Exception:
            pass

        candidates = [c for c in mom if c in tradable]
        if bull_sectors:
            # 行业动量加权:强势行业候选 +0.05 绝对动量加成
            def _score(c):
                s = mom[c]
                if ind_map.get(c) in bull_sectors:
                    s += 0.05
                return s
            ranked = sorted(candidates, key=_score, reverse=True)
        else:
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
                    reason = f"Stage2v2:{nm}跌破MA120,卖出"
                else:
                    reason = f"Stage2v2:{nm}硬门槛失效({tag.get(code, '')}),卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                nm = ctx.name(code)
                m = mom[code]
                ind_boost = "·强势行业" if ind_map.get(code) in bull_sectors else ""
                reason = (f"Stage2v2:买入{nm}(12-1月动量{m:+.1%},均线多头排列且MA250上行,"
                          f"52周区间达标{ind_boost},候选{len(ranked)}只中第{full_rank[code]},regime={_reg})")
                orders.append(Order(self.strategy_id, code, "buy", w, reason, date))
        return orders
