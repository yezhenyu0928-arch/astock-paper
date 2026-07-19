# -*- coding: utf-8 -*-
"""S14 低估反转轮动经理。

模拟"逆向价值型"投资经理: 在中小盘(规模溢价)里,挑选低估值(PE/PB低)+质量(ROE)
的个股,叠加技术面"中期超跌后企稳反转"信号(温和超跌+底部放量+均线拐头),
并做行业轮动。综合基本面(价值+质量)+技术面(反转企稳)多重因子。
控回撤靠 质量门禁 + 分散(25只) + 单行业上限 + 破MA60止损 + 行业轮动。
不激进空仓(避免踏空),仅风险市温和降仓。

与 S13(成长质量) 的差异: S13 追"强"(成长+趋势向上), S14 捡"便宜+企稳反转"
(低估值+超跌反转),两者 universe 同为中小盘、风格互补。
"""
import re
import numpy as np
from models import Order
from strategies.base import BaseStrategy
import macro as _macro
import factors as _fac
import fundamental as F
from strategies import common
from strategies import news_guard
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


def _stance(date, conn):
    """温和仓位系数: 风险市降仓,其余满仓。"""
    try:
        reg = _macro.compute_market_regime(date, conn=conn)
        regime = reg.get("regime", "震荡")
    except Exception:
        regime = "震荡"
    if regime == "风险":
        return 0.6, regime
    return 1.0, regime


class S14ValueReversalRotation(BaseStrategy):
    """低估反转轮动: 低估值+质量 + 技术超跌企稳反转 + 行业轮动。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []

        stance, regime = _stance(date, ctx.conn)

        # ── 行业轮动: 强势行业 ──
        try:
            top_sec, _ = _macro.top_bullish_sectors(date, conn=ctx.conn, top=8)
            strong_ind = {s["name"] for s in top_sec}
        except Exception:
            strong_ind = set()

        min_avg = self.params.get("min_avg_amount", 30_000_000)
        min_cap = self.params.get("min_market_cap", 10e8)
        max_cap = self.params.get("max_market_cap", 150e8)
        hold_n = self.params.get("hold_n", 25)
        max_per_sec = self.params.get("max_per_sector", 3)
        roe_min = self.params.get("roe_min", 0.08)
        pe_cap = self.params.get("pe_cap", 30)
        pb_cap = self.params.get("pb_cap", 3.0)
        w = self.params.get("weights", {"value_pe": 0.20, "value_pb": 0.13, "quality": 0.17,
                                        "reversal": 0.25, "stabilize": 0.25})
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)

        all_codes = [r[0] for r in ctx.conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]
        all_codes = common.main_board_universe(ctx, all_codes, self.config, date)  # 主板宇宙硬约束(手册)

        cands = []  # (code, pe, pb, roe, ret60, vol_ratio, ind)
        for code in all_codes:
            if not _is_stock(code):
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("market_cap"):
                continue
            mcap = f["market_cap"]
            if mcap < min_cap or mcap > max_cap:
                continue
            pe, pb = f.get("pe"), f.get("pb")
            if not (pe and 0 < pe < pe_cap) or not (pb and 0 < pb < pb_cap):
                continue
            if ctx.avg_amount(code, 20) < min_avg:
                continue
            if not ctx.is_tradable(code, date):
                continue
            # 质量门禁: 连续3年 ROE≥8%
            try:
                ok, roe = F.roe_quality(code, date, years=3, min_roe=roe_min, conn=ctx.conn)
            except Exception:
                ok, roe = False, 0.0
            if not ok:
                continue
            c = ctx.close(code, 260)
            if len(c) < 250:
                continue
            closes = np.array(c, dtype=float)
            ma20, ma60 = _ma(closes, 20), _ma(closes, 60)
            if None in (ma20, ma60):
                continue
            # 技术面: 中期温和超跌(非深坑) + 均线拐头企稳 + 底部放量
            ret60 = closes[-1] / closes[-61] - 1 if len(closes) >= 61 else 0
            if not (-0.35 <= ret60 <= -0.05):
                continue
            if ma20 < ma60:           # 未企稳(均线仍空头)
                continue
            amt20 = ctx.avg_amount(code, 20)
            amt60 = ctx.avg_amount(code, 60)
            vol_ratio = amt20 / amt60 if amt60 else 0.0
            if vol_ratio < 1.0:       # 未放量企稳
                continue
            ind = _fac.get_industry(ctx.conn, [code]).get(code)
            # 行业轮动是加分项而非硬门槛: 仅当存在强势行业信号(strong_ind非空)且该股有行业归属时,
            # 才剔除不在强势行业的个股;信号缺失或无行业归属一律降级为不过滤(避免候选池恒空→0成交)
            if strong_ind and ind and ind not in strong_ind:
                continue
            cands.append((code, pe, pb, roe, ret60, vol_ratio, ind))

        # —— 新闻/公告/动态守卫(全量接入) ——
        _cc = [c[0] for c in cands]
        _ind_of = _fac.get_industry(ctx.conn, _cc)
        _ban_n, _ = news_guard.guard_candidates(date, _cc, ctx.conn, self.config)
        _ban_i = news_guard.guard_industry(date, _cc, ctx.conn, self.config, _ind_of)
        _ban_s = {c for c in _cc if news_guard.structural_ban(date, c, ctx)[0]}
        _banned = _ban_n | _ban_i | _ban_s
        if _banned:
            cands = [c for c in cands if c[0] not in _banned]

        if not cands:
            return [Order(self.strategy_id, c, "sell", 0.0,
                          f"低估反转轮动:无符合低估值+质量+超跌企稳+强势行业个股,清仓", date)
                    for c in account.positions.keys()]

        # 排名法打分(名次越小越好)
        pe_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[1]))}
        pb_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[2]))}
        roe_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[3], reverse=True))}
        rev_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[4]))}  # 更超跌名次小
        stab_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[5], reverse=True))}
        scored = sorted(cands, key=lambda x: (
            w["value_pe"] * pe_rank[x[0]] + w["value_pb"] * pb_rank[x[0]]
            + w["quality"] * roe_rank[x[0]] + w["reversal"] * rev_rank[x[0]]
            + w["stabilize"] * stab_rank[x[0]]))

        sec_cnt, target = {}, []
        meta = {}
        for code, pe, pb, roe, ret60, vol_ratio, ind in scored:
            if sec_cnt.get(ind, 0) >= max_per_sec:
                continue
            target.append(code)
            sec_cnt[ind] = sec_cnt.get(ind, 0) + 1
            meta[code] = (pe, pb, roe, ret60, vol_ratio)
            if len(target) >= eff:
                break

        wgt = (1.0 / eff) * stance
        tset = set(target)
        orders = []
        forced = news_guard.guard_holdings(date, list(account.positions.keys()), ctx.conn, self.config)
        for code in list(account.positions.keys()):
            reason = None
            if code in forced:
                reason = f"低估反转轮动:{ctx.name(code)}新闻黑天鹅,同步清仓"
            elif code not in tset:
                reason = f"低估反转轮动:{ctx.name(code)}掉出目标池,卖出"
            else:
                cc = ctx.close(code, 61)
                if len(cc) >= 61 and cc[-1] < float(np.mean(cc[-60:])):
                    reason = f"低估反转轮动:{ctx.name(code)}跌破MA60趋势破坏,止损"
            if reason:
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in account.positions:
                pe, pb, roe, ret60, vol_ratio = meta[code]
                orders.append(Order(self.strategy_id, code, "buy", wgt,
                                    f"低估反转轮动:买入{ctx.name(code)}(PE{pe:.0f}/PB{pb:.1f}/ROE{roe:.0%}"
                                    f"/60日{ret60:+.1%}/放量{vol_ratio:.1f}x/强势行业,大盘{regime})", date))
        return orders
