# -*- coding: utf-8 -*-
"""S9 成长择时经理(基本面成长 + 市场择时 + 行业轮动版)。

模拟"基本面成长型"投资经理:挑选 ROE 质量高、盈利同比稳健增长、估值合理的
成长股,但**同样叠加大盘择时与行业轮动**——这是旧 s9(Stage2 价格动量)崩成
-40% 回撤的根因:它满仓穿越 2022-23 熊市。本版在 regime='风险'/超买时清仓,
熊市自动空仓,把回撤压下来。

──────── 三层框架(与 S3 共用,选股维度不同) ────────
① 市场择时: compute_market_regime 见顶(风险/超买)清仓、见底(强势/超卖)满仓;
   仓位 = stance × 个股目标权重。
② 行业轮动: 仅买 top_bullish_sectors 强势行业内的成长股 → 自动切换主线。
③ 回撤控制: 持仓个股跌破 MA60 即止损;大盘风险市清仓。

选股(成长维度,纯基本面驱动、非价格动量):
   - ROE 质量: roe_quality 连续3年 ROE≥8%;
   - 盈利同比稳健增长: 最新年度 net_profit 同比 +5%~+80%(既要增长、又防透支/暴雷);
   - 估值约束: PE>0 且 PE<60、PB>0(避开极端估值);
   - 动量确认: 收盘价>MA60(不在自由落体,避开价值陷阱);
   - 流动性门槛。
"""
import re
import numpy as np
from models import Order
from strategies.base import BaseStrategy
import macro as _macro
import factors as _fac
import fundamental as F
from strategies import common
import util


def _is_stock(code):
    if not (code.startswith("sh") or code.startswith("sz")):
        return False
    if code.startswith("sh") and code[2:3] == "5":
        return False
    if code.startswith("sz") and re.match(r"^sz1[25-9]", code):
        return False
    return True


def _ma(closes, n):
    return float(np.mean(closes[-n:])) if (closes is not None and len(closes) >= n) else None


def _rsi(conn, code, date, n=14):
    rows = conn.execute(
        "SELECT close FROM daily_bar WHERE code=? AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT ?",
        (code, date, n + 1)).fetchall()
    if len(rows) < n + 1:
        return None
    closes = np.array([float(r[0]) for r in rows][::-1])
    diffs = np.diff(closes)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    ag, al = gains.mean(), losses.mean()
    if al == 0:
        return 100.0
    return float(100 - 100 / (1 + rs)) if (rs := ag / al) else 100.0


def _earnings_yoy(conn, code, date):
    """最近两个 stat_year 的净利润同比(防未来函数: pub_date<=date)。无数据返回 None。"""
    rows = conn.execute(
        "SELECT net_profit FROM stock_annual WHERE code=? AND pub_date<=? "
        "ORDER BY stat_year DESC LIMIT 2",
        (code, util.to_date_str(date))).fetchall()
    if len(rows) < 2:
        return None
    np0, np1 = rows[0][0], rows[1][0]
    if np1 is None or np0 is None or np1 == 0:
        return None
    return (np0 - np1) / abs(np1)


def _market_stance(date, conn):
    """同 S3:返回 (stance 0~1, regime_str, score, rsi)。"""
    try:
        reg = _macro.compute_market_regime(date, conn=conn)
        regime = reg.get("regime", "震荡")
        score = reg.get("score", 50)
    except Exception:
        regime, score = "震荡", 50
    try:
        ms = _macro.macro_score(date, conn=conn)
    except Exception:
        ms = 0.0
    rsi = _rsi(conn, "sh510300", date, 14)
    if regime == "数据不足":
        regime = "震荡"
    if regime == "风险":
        stance = 0.0
    elif regime == "转弱":
        stance = 0.35
    elif regime == "强势":
        stance = 1.0 * (0.75 + 0.25 * max(0.0, ms))
    else:
        stance = 0.60
    if rsi is not None and rsi > 82:
        stance = min(stance, 0.30)
        regime = regime + "(超买)"
    if rsi is not None and rsi < 25 and regime != "风险":
        stance = max(stance, 0.85)
        regime = regime + "(超卖)"
    return stance, regime, score, rsi


class S9GrowthTiming(BaseStrategy):
    """成长择时: ROE质量+盈利稳健增长+估值 + 大盘择时 + 行业轮动。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_week(date):
            return []

        # ── ① 市场择时 ──
        stance, regime, score, rsi = _market_stance(date, ctx.conn)
        if stance <= 0.05:
            return [Order(self.strategy_id, c, "sell", 0.0,
                          f"成长择时:市场{regime}(分{score})短期见顶/风险,清仓观望", date)
                    for c in account.positions.keys()]

        # ── ② 行业轮动: 强势行业 ──
        try:
            top_sec, _ = _macro.top_bullish_sectors(date, conn=ctx.conn, top=6)
            strong_ind = {s["name"] for s in top_sec}
        except Exception:
            strong_ind = set()

        # ── ③ 个股扫描(成长 + 轮动) ──
        min_avg = self.params.get("min_avg_amount", 80_000_000)
        min_cap = self.params.get("min_market_cap", 8e9)
        hold_n = self.params.get("hold_n", 10)
        max_per_sec = self.params.get("max_per_sector", 2)
        roe_min = self.params.get("roe_min", 0.08)
        g_min = self.params.get("growth_min", 0.05)
        g_max = self.params.get("growth_max", 0.80)
        pe_cap = self.params.get("pe_cap", 60)
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)

        all_codes = [r[0] for r in ctx.conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]

        cands = []  # (code, growth_score, roe, industry)
        for code in all_codes:
            if not _is_stock(code):
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("market_cap") or f["market_cap"] < min_cap:
                continue
            if not (f.get("pe") and 0 < f["pe"] < pe_cap) or not f.get("pb"):
                continue
            if ctx.avg_amount(code, 20) < min_avg:
                continue
            if not ctx.is_tradable(code, date):
                continue
            # ROE 质量
            try:
                ok, roe = F.roe_quality(code, date, years=3, min_roe=roe_min, conn=ctx.conn)
            except Exception:
                ok, roe = False, 0.0
            if not ok:
                continue
            # 盈利同比稳健增长
            gy = _earnings_yoy(ctx.conn, code, date)
            if gy is None or not (g_min <= gy <= g_max):
                continue
            c = ctx.close(code, 130)
            if len(c) < 120:
                continue
            closes = np.array(c, dtype=float)
            ma60 = _ma(closes, 60)
            if ma60 is None or closes[-1] <= ma60:   # 动量确认: 价在MA60上,避开自由落体
                continue
            ret20 = closes[-1] / closes[-21] - 1 if len(closes) >= 21 else 0
            # 行业轮动过滤
            ind = _fac.get_industry(ctx.conn, [code]).get(code)
            if not ind or ind not in strong_ind:
                continue
            # 成长强度评分: 盈利增速 + ROE + 价格动量,综合排序
            growth_score = gy * 0.5 + roe * 1.5 + max(0.0, ret20) * 0.5
            cands.append((code, growth_score, roe, ind))

        if not cands:
            return [Order(self.strategy_id, c, "sell", 0.0,
                          f"成长择时:无符合ROE质量+盈利增长+强势行业个股,清仓", date)
                    for c in account.positions.keys()]

        cands.sort(key=lambda x: x[1], reverse=True)
        sec_cnt, target = {}, []
        for code, sc, roe, ind in cands:
            if sec_cnt.get(ind, 0) >= max_per_sec:
                continue
            target.append(code)
            sec_cnt[ind] = sec_cnt.get(ind, 0) + 1
            if len(target) >= eff:
                break

        # ── ④ 构建订单(仓位随 stance 缩放;个股破MA60止损) ──
        w = (1.0 / eff) * stance
        tset = set(target)
        orders = []
        for code in list(account.positions.keys()):
            reason = None
            if code not in tset:
                reason = f"成长择时:{ctx.name(code)}掉出目标池,卖出"
            else:
                cc = ctx.close(code, 61)
                if len(cc) >= 61 and cc[-1] < float(np.mean(cc[-60:])):
                    reason = f"成长择时:{ctx.name(code)}跌破MA60趋势破坏,止损"
            if reason:
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in account.positions:
                gy = _earnings_yoy(ctx.conn, code, date)
                gy_pct = f"{gy*100:.0f}%" if gy is not None else "?"
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"成长择时:买入{ctx.name(code)}(ROE{roe:.0%}/盈利同比{gy_pct}/"
                                    f"强势行业,大盘{regime} stance={stance:.0%})", date))
        return orders
