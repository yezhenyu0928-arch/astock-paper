# -*- coding: utf-8 -*-
"""S13 景气成长质量轮动经理。

模拟"景气成长型"投资经理: 在中小盘(规模溢价, A股 最确定的收益来源)里,
挑选 ROE质量高、盈利同比稳健增长、估值合理的成长股,叠加技术面趋势确认
(均线多头排列+动量向上+不过热+波动适中),并做行业轮动(仅买强势行业),
分散持仓 + 破位止损 控制回撤。综合基本面 + 技术面 多重因子。

设计原则(吸取 s3/s9 教训):
- 不激进空仓择时(避免 whipsaw 踏空牛市);仅在大盘 regime='风险' 时温和降仓(0.6)。
- 控回撤靠: 质量门禁(避开价值陷阱/暴雷) + 分散(25只) + 单行业上限 + 破MA120止损
  + 行业轮动(避开退潮行业)。
- universe 锁定中小盘,吃规模溢价(确保年化收益的确定性来源)。
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


def _stance(date, conn):
    """温和仓位系数: 风险市降仓,其余满仓(避免激进择时踏空)。"""
    try:
        reg = _macro.compute_market_regime(date, conn=conn)
        regime = reg.get("regime", "震荡")
    except Exception:
        regime = "震荡"
    if regime == "风险":
        return 0.6, regime
    return 1.0, regime


class S13GrowthQualityRotation(BaseStrategy):
    """成长质量轮动: ROE质量+盈利稳健增长+估值 + 技术趋势确认 + 行业轮动。"""

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
        g_min = self.params.get("growth_min", 0.05)
        g_max = self.params.get("growth_max", 0.80)
        pe_cap = self.params.get("pe_cap", 50)
        w = self.params.get("weights", {"roe": 0.18, "growth": 0.18, "value": 0.19,
                                        "momentum": 0.25, "lowvol": 0.20})
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)

        all_codes = [r[0] for r in ctx.conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]
        all_codes = common.main_board_universe(ctx, all_codes, self.config, date)  # 主板宇宙硬约束(手册)

        cands = []  # (code, roe, gy, pe, ret20, vol, ind)
        for code in all_codes:
            if not _is_stock(code):
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("market_cap"):
                continue
            mcap = f["market_cap"]
            if mcap < min_cap or mcap > max_cap:
                continue
            if not (f.get("pe") and 0 < f["pe"] < pe_cap) or not f.get("pb"):
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
            # 盈利同比稳健增长
            gy = _earnings_yoy(ctx.conn, code, date)
            if gy is None or not (g_min <= gy <= g_max):
                continue
            c = ctx.close(code, 260)
            if len(c) < 250:
                continue
            closes = np.array(c, dtype=float)
            ma20, ma60, ma120 = _ma(closes, 20), _ma(closes, 60), _ma(closes, 120)
            if None in (ma20, ma60, ma120):
                continue
            # 技术面: 多头排列 + MA120上行 + 动量>0 + 不过热 + 波动适中
            if not (ma20 > ma60 > ma120):
                continue
            if closes[-1] <= ma120:
                continue
            ma120_prev = _ma(closes[:-20], 120)
            if ma120_prev is None or ma120 <= ma120_prev:
                continue
            ret20 = closes[-1] / closes[-21] - 1 if len(closes) >= 21 else 0
            if ret20 <= 0:
                continue
            ret60 = closes[-1] / closes[-61] - 1 if len(closes) >= 61 else 0
            if ret60 > 1.0:          # 近60日>100%过热,避开接盘
                continue
            rets = np.diff(closes[-60:])
            vol = float(np.std(rets)) if len(rets) > 1 else 9.9
            ind = _fac.get_industry(ctx.conn, [code]).get(code)
            if not ind or ind not in strong_ind:
                continue
            cands.append((code, roe, gy, f["pe"], ret20, vol, ind))

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
                          f"成长质量轮动:无符合ROE质量+盈利增长+强势行业个股,清仓", date)
                    for c in account.positions.keys()]

        # 排名法打分(名次越小越好)
        n = len(cands)
        roe_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[1], reverse=True))}
        gy_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[2], reverse=True))}
        pe_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[3]))}
        mom_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[4], reverse=True))}
        vol_rank = {c[0]: i for i, c in enumerate(sorted(cands, key=lambda x: x[5]))}
        scored = sorted(cands, key=lambda x: (
            w["roe"] * roe_rank[x[0]] + w["growth"] * gy_rank[x[0]] + w["value"] * pe_rank[x[0]]
            + w["momentum"] * mom_rank[x[0]] + w["lowvol"] * vol_rank[x[0]]))

        sec_cnt, target = {}, []
        meta = {}
        for code, roe, gy, pe, r20, vol, ind in scored:
            if sec_cnt.get(ind, 0) >= max_per_sec:
                continue
            target.append(code)
            sec_cnt[ind] = sec_cnt.get(ind, 0) + 1
            meta[code] = (roe, gy, pe, r20)
            if len(target) >= eff:
                break

        wgt = (1.0 / eff) * stance
        tset = set(target)
        orders = []
        forced = news_guard.guard_holdings(date, list(account.positions.keys()), ctx.conn, self.config)
        for code in list(account.positions.keys()):
            reason = None
            if code in forced:
                reason = f"成长质量轮动:{ctx.name(code)}新闻黑天鹅,同步清仓"
            elif code not in tset:
                reason = f"成长质量轮动:{ctx.name(code)}掉出目标池,卖出"
            else:
                cc = ctx.close(code, 121)
                if len(cc) >= 121 and cc[-1] < float(np.mean(cc[-120:])):
                    reason = f"成长质量轮动:{ctx.name(code)}跌破MA120趋势破坏,止损"
            if reason:
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in account.positions:
                roe, gy, pe, r20 = meta[code]
                orders.append(Order(self.strategy_id, code, "buy", wgt,
                                    f"成长质量轮动:买入{ctx.name(code)}(ROE{roe:.0%}/盈利同比{gy*100:.0f}%"
                                    f"/PE{pe:.0f}/20日{r20:+.1%}/强势行业,大盘{regime})", date))
        return orders
