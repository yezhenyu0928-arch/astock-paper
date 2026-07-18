# -*- coding: utf-8 -*-
"""S15 核心配置经理(手册组合构建:高股息底仓 + 核心成长 + 现金)。

按手册"组合构建"章节,把账户拆成三个部分,按月再平衡:
  · 高股息底仓  dividend_weight(默认35%):防御/收入型,熊市扛跌、提供现金流
  · 核心成长    growth_weight(默认45%):进攻型,吃盈利增长+规模溢价
  · 现金        cash = 1 - 两者(默认20%,手册现金≥10%)

设计(与手册纪律一致):
- 两个袖均过 main_board_universe 硬约束(主板/≥80亿/≥2年/≥8000万成交)。
- 两个袖均接入 news_guard 全量守卫(候选黑名单/行业负面/结构化排雷/持仓黑天鹅)。
- 单行业≤max_per_industry,避免红利天然扎堆银行/公用、成长扎堆单一赛道。
- 宏观择时(总仓位0-90%)与消息面降敞口由 risk 层统一处理(单一权威),本策略只表达配置观点。
- 月度再平衡;无符合标的的袖→该部分转为现金(防御)。

与 s1/s13 的关系:本策略是"配置层",把 s1 的红利哲学与 s13 的成长质量哲学按比例拼成组合,
各自独立选股、各自风控,再由 risk 层统一做宏观/消息面/单票/行业/总仓约束。
"""
import re
import statistics
import numpy as np
from models import Order
from strategies.base import BaseStrategy
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


def _net_profit_positive(conn, code, date):
    """最新一期(已披露,pub_date<=date)净利润>0。无数据→False。"""
    r = conn.execute(
        "SELECT net_profit FROM stock_annual WHERE code=? AND pub_date<=? "
        "ORDER BY stat_year DESC LIMIT 1",
        (code, util.to_date_str(date))).fetchone()
    return bool(r and r[0] is not None and r[0] > 0)


def _cont_div_years(conn, code, years, date):
    """近 years 个自然年(基于 ex_date)中,现金分红(cash_per_share>0)的年数。"""
    try:
        yr = int(str(date)[:4])
        rows = conn.execute(
            "SELECT DISTINCT substr(ex_date,1,4) FROM dividend "
            "WHERE code=? AND cash_per_share>0 AND substr(ex_date,1,4)>=?",
            (code, str(yr - years + 1))).fetchall()
        return len(rows)
    except Exception:
        return 0


class S15CoreAllocation(BaseStrategy):
    """核心配置:高股息底仓 + 核心成长,按比例配置,月度再平衡。"""

    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []

        # ── 参数 ──
        div_w = float(self.params.get("dividend_weight", 0.35))
        grw_w = float(self.params.get("growth_weight", 0.45))
        div_n = int(self.params.get("dividend_hold_n", 5))
        grw_n = int(self.params.get("growth_hold_n", 5))
        min_dy = self.params.get("min_dividend_yield", 0.025)
        div_years = int(self.params.get("dividend_years", 3))
        roe_min = self.params.get("roe_min", 0.08)
        roe_years = int(self.params.get("roe_years", 3))
        g_min = self.params.get("growth_min", 0.05)
        g_max = self.params.get("growth_max", 0.80)
        min_avg = self.params.get("min_avg_amount", 80_000_000)
        min_cap = self.params.get("min_market_cap", 8_000_000_000)
        max_cap = self.params.get("max_market_cap", 500_000_000_000)
        max_per_ind = int(self.params.get("max_per_industry", 2))
        pe_cap = self.params.get("pe_cap", 50)

        # ── 候选宇宙(主板硬约束) ──
        all_codes = [r[0] for r in ctx.conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]
        all_codes = common.main_board_universe(ctx, all_codes, self.config, date)

        div_cands, grw_cands = [], []
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
            # 质量门禁:连续 roe_years 年 ROE≥roe_min
            try:
                ok, roe = F.roe_quality(code, date, years=roe_years, min_roe=roe_min, conn=ctx.conn)
            except Exception:
                ok, roe = False, 0.0
            if not ok:
                continue
            # 盈利为正(避开亏损/僵尸)
            if not _net_profit_positive(ctx.conn, code, date):
                continue
            # 价格历史
            c = ctx.close(code, 250)
            if len(c) < 200:
                continue
            closes = np.array(c, dtype=float)
            ma20, ma60 = _ma(closes, 20), _ma(closes, 60)
            if None in (ma20, ma60):
                continue
            ret20 = closes[-1] / closes[-21] - 1 if len(closes) >= 21 else 0
            rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
            vol = statistics.pstdev(rets) if len(rets) > 1 else 9.9
            ind = _fac.get_industry(ctx.conn, [code]).get(code) or "未知"

            # 红利袖资格:股息率达标 + 连续分红年数达标
            dy = f.get("dividend_yield") or 0.0
            cont = _cont_div_years(ctx.conn, code, div_years, date)
            if dy >= min_dy and cont >= div_years:
                div_cands.append((code, dy, roe, vol, ind))

            # 成长袖资格:盈利同比稳健 + 技术趋势(MA20>MA60 + 动量>0)
            gy = _earnings_yoy(ctx.conn, code, date)
            if gy is not None and g_min <= gy <= g_max and ma20 > ma60 and ret20 > 0:
                grw_cands.append((code, roe, gy, ret20, vol, ind))

        # ── 新闻/公告/动态守卫(全量接入,作用于两袖并集) ──
        _cc = [c[0] for c in div_cands] + [c[0] for c in grw_cands]
        if _cc:
            _ind_of = _fac.get_industry(ctx.conn, _cc)
            _ban_n, _ = news_guard.guard_candidates(date, _cc, ctx.conn, self.config)
            _ban_i = news_guard.guard_industry(date, _cc, ctx.conn, self.config, _ind_of)
            _ban_s = {c for c in _cc if news_guard.structural_ban(date, c, ctx)[0]}
            _banned = _ban_n | _ban_i | _ban_s
            if _banned:
                div_cands = [c for c in div_cands if c[0] not in _banned]
                grw_cands = [c for c in grw_cands if c[0] not in _banned]

        # ── 选股(名次法打分 + 单行业上限) ──
        def pick(cands, n, scorer):
            if not cands:
                return []
            ranked = sorted(cands, key=scorer)
            out, cnt = [], {}
            for item in ranked:
                code = item[0]
                ind = item[-1]
                if cnt.get(ind, 0) >= max_per_ind:
                    continue
                out.append(code)
                cnt[ind] = cnt.get(ind, 0) + 1
                if len(out) >= n:
                    break
            return out

        # 红利袖:股息率降序、低波升序、ROE降序 综合名次
        div_pick = pick(div_cands, div_n,
                        lambda x: (-x[1], x[3], -x[2]))
        # 成长袖:盈利同比降序、动量降序、ROE降序、低波升序
        grw_pick = pick(grw_cands, grw_n,
                        lambda x: (-x[2], -x[3], -x[1], x[4]))

        # ── 构建目标权重 ──
        target = {}  # code -> weight
        meta = {}
        if div_pick:
            per = div_w / len(div_pick)
            dmeta = {c[0]: (c[1], c[2]) for c in div_cands}
            for code in div_pick:
                target[code] = round(per, 6)
                dy, roe = dmeta[code]
                meta[code] = ("红利底仓", dy, roe)
        if grw_pick:
            per = grw_w / len(grw_pick)
            gmeta = {c[0]: (c[1], c[2], c[3]) for c in grw_cands}
            for code in grw_pick:
                target[code] = round(per, 6)
                roe, gy, r20 = gmeta[code]
                meta[code] = ("核心成长", roe, gy, r20)

        tset = set(target.keys())
        orders = []
        held = set(account.positions.keys())
        forced = news_guard.guard_holdings(date, list(held), ctx.conn, self.config)

        # 卖出:持仓黑天鹅 / 掉出目标池(对应袖换仓)
        for code in held:
            if code in target and code not in forced:
                continue
            nm = ctx.name(code)
            if code in forced:
                reason = f"核心配置:{nm}新闻黑天鹅,同步清仓"
            elif code in tset:
                reason = f"核心配置:{nm}权重再平衡"
            else:
                sleeve = "红利底仓" if code in {c[0] for c in div_cands} else (
                    "核心成长" if code in {c[0] for c in grw_cands} else "原持仓")
                reason = f"核心配置:{nm}掉出{sleeve}目标池,卖出"
            orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))

        # 买入:目标池新进入者
        for code, wgt in target.items():
            if code in held:
                continue
            nm = ctx.name(code)
            tag, *vals = meta[code]
            if tag == "红利底仓":
                dy, roe = vals
                r = f"核心配置·{tag}:买入{nm}(股息率{dy:.1%}/ROE{roe:.0%},防御底仓{div_w:.0%})"
            else:
                roe, gy, r20 = vals
                r = f"核心配置·{tag}:买入{nm}(ROE{roe:.0%}/盈利同比{gy*100:.0f}%/" \
                    f"20日{r20:+.1%},进攻仓位{grw_w:.0%})"
            orders.append(Order(self.strategy_id, code, "buy", wgt, r, date))

        # 无符合标的(两袖皆空)→ 清仓持币(防御)
        if not target:
            orders = [Order(self.strategy_id, code, "sell", 0.0,
                            "核心配置:两袖均无符合纪律标的,清仓持币", date)
                      for code in held if code not in forced]
        return orders
