# -*- coding: utf-8 -*-
"""S3 趋势择时经理(市场择时 + 行业轮动版)。

模拟"顺势+择时型"投资经理:只在确认的上升通道里持有股票,靠大盘择时躲开
暴跌、靠行业轮动踩准主线、靠个股止损控制回撤,目标是跑赢大盘且回撤收敛。

──────── 三层框架(此前 s3 失败的根因:只做个股均线,没有这三层) ────────
① 市场择时(捕捉短期见顶/见底):
   - 见顶抛出: compute_market_regime 返回 regime='风险'(广度崩+风险比飙升+近1月转负)
     或 基准 sh510300 RSI14>82 超买 → 清仓观望,不逆势硬扛。
   - 见底买入: regime='强势' 或 基准 RSI14<25 超卖且非风险市 → 满仓(stance 上调)。
   - regime='转弱' → 防御半仓;'震荡' → 中性六成。仓位 = stance × 个股目标权重。
② 行业轮动: 仅买入 top_bullish_sectors 强势行业内的个股,单行业上限分散,
   每调仓日重算 → 自动从退潮行业切到走强行业。
③ 回撤控制: 持仓个股跌破 MA50(趋势破坏)即止损;大盘风险市清仓。

选股(趋势维度): 多周期均线多头排列(收盘>MA20>MA50>MA120) + 动量(20/60日正收益)
+ 非极端超买(60日涨幅<120%,避开追高顶部) + 流动性门槛。
"""
import re
import numpy as np
from models import Order
from strategies.base import BaseStrategy
import macro as _macro
import factors as _fac
from strategies import common


def _is_stock(code):
    """仅保留个股,剔除ETF/债券(沪 sh5xxxx、深 sz1[2,5-9]xxx)。"""
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
    """基准/个股 RSI(14),用于短期超买超卖判断。数据不足返回 None。"""
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
    rs = ag / al
    return float(100 - 100 / (1 + rs))


def _market_stance(date, conn):
    """返回 (stance 0~1, regime_str, score, detail)。

    stance 即"计划投入仓位比例": 1.0=满仓, 0.0=清仓。
    大盘短期见顶 → 压低; 短期见底 → 抬高。
    """
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
    else:  # 震荡
        stance = 0.60

    # —— 短期见顶信号: 超买 → 减仓抛出 ——
    if rsi is not None and rsi > 82:
        stance = min(stance, 0.30)
        regime = regime + "(超买)"
    # —— 短期见底信号: 超卖且非风险市 → 加仓买入 ——
    if rsi is not None and rsi < 25 and regime != "风险":
        stance = max(stance, 0.85)
        regime = regime + "(超卖)"

    return stance, regime, score, rsi


class S3TrendTiming(BaseStrategy):
    """趋势择时:多周期均线多头 + 动量 + 大盘择时 + 行业轮动。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_week(date):
            return []

        # ── ① 市场择时 ──
        stance, regime, score, rsi = _market_stance(date, ctx.conn)
        if stance <= 0.05:
            # 短期见顶/风险市: 清仓所有持仓,持币观望
            orders = [Order(self.strategy_id, c, "sell", 0.0,
                            f"趋势择时:市场{regime}(分{score})短期见顶/风险,清仓观望", date)
                      for c in account.positions.keys()]
            return orders

        # ── ② 行业轮动: 取强势行业 ──
        try:
            top_sec, _ = _macro.top_bullish_sectors(date, conn=ctx.conn, top=6)
            strong_ind = {s["name"] for s in top_sec}
            sec_mom = {s["name"]: (s.get("momentum_pct") or 0.0) for s in top_sec}
        except Exception:
            strong_ind, sec_mom = set(), {}

        # ── ③ 个股扫描(趋势 + 轮动) ──
        min_avg = self.params.get("min_avg_amount", 80_000_000)
        min_cap = self.params.get("min_market_cap", 8e9)
        hold_n = self.params.get("hold_n", 12)
        max_per_sec = self.params.get("max_per_sector", 2)
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)

        all_codes = [r[0] for r in ctx.conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]

        cands = []  # (code, trend_score, sector)
        for code in all_codes:
            if not _is_stock(code):
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("market_cap") or f["market_cap"] < min_cap:
                continue
            if ctx.avg_amount(code, 20) < min_avg:
                continue
            if not ctx.is_tradable(code, date):
                continue
            c = ctx.close(code, 260)
            if len(c) < 250:
                continue
            closes = np.array(c, dtype=float)
            ma20, ma50, ma120 = _ma(closes, 20), _ma(closes, 50), _ma(closes, 120)
            if ma20 is None or ma50 is None or ma120 is None:
                continue
            last = closes[-1]
            # 多周期多头排列
            if not (last > ma20 > ma50 > ma120):
                continue
            ret20 = last / closes[-21] - 1 if len(closes) >= 21 else 0
            ret60 = last / closes[-61] - 1 if len(closes) >= 61 else 0
            if ret20 <= 0 or ret60 <= 0:
                continue
            if ret60 > 1.20:        # 60日翻1.2倍=追高顶部,回避
                continue
            # RSI 不极端超买
            r = _rsi(ctx.conn, code, date, 14)
            if r is not None and r > 85:
                continue
            # 行业轮动过滤: 仅留强势行业个股
            ind = _fac.get_industry(ctx.conn, [code]).get(code)
            if not ind or ind not in strong_ind:
                continue
            # 趋势强度评分: 距MA120空间 + 60日动量 + 均线多头锐度
            trend_score = (last / ma120 - 1) * 0.5 + ret60 * 0.5 + (ma20 / ma50 - 1) * 2.0
            cands.append((code, trend_score, ind))

        if not cands:
            return [Order(self.strategy_id, c, "sell", 0.0,
                          f"趋势择时:无符合多头排列+强势行业个股,清仓", date)
                    for c in account.positions.keys()]

        cands.sort(key=lambda x: x[1], reverse=True)
        # 单行业上限,实现轮动分散
        sec_cnt, target = {}, []
        for code, sc, ind in cands:
            if sec_cnt.get(ind, 0) >= max_per_sec:
                continue
            target.append(code)
            sec_cnt[ind] = sec_cnt.get(ind, 0) + 1
            if len(target) >= eff:
                break

        # ── ④ 构建订单(仓位随 stance 缩放,剩余为现金;个股破MA50止损) ──
        w = (1.0 / eff) * stance
        tset = set(target)
        orders = []
        # 卖出: 不在目标 / 或持仓已破MA50(趋势破坏→回撤控制)
        for code in list(account.positions.keys()):
            reason = None
            if code not in tset:
                reason = f"趋势择时:{ctx.name(code)}掉出目标池,卖出"
            else:
                cc = ctx.close(code, 51)
                if len(cc) >= 51 and cc[-1] < float(np.mean(cc[-50:])):
                    reason = f"趋势择时:{ctx.name(code)}跌破MA50趋势破坏,止损"
            if reason:
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        # 买入目标
        for code in target:
            if code not in account.positions:
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"趋势择时:买入{ctx.name(code)}(多头排列+强势行业,"
                                    f"大盘{regime} stance={stance:.0%})", date))
        return orders
