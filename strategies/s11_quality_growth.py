# -*- coding: utf-8 -*-
"""S11 质量稳健成长经理 (Quality + Stable Earnings Growth)。

投资经理画像: 基本面成长型,赚"公司盈利真实、稳健增长"的钱,而非追价格动量。
设计依据(来自 A 股因子实证):
- 质量因子(ROE)全市场稳健有效(胜率>75%);盈利"稳定性/质量"指标近年走强。
- 成长因子: "稳健增长比高增长更可靠"——高增长常被过度预期、超买,反而不赚钱;
  用"net_profit 同比稳健正增长 + 增长波动小"刻画真正可持续的成长。
- 区别于已下线的 s9(Stage2 用价格 52 周高 + 12-1 月价格动量,在 A 股价格动量弱/易反转而失败):
  本策略完全基于【盈利基本面】驱动选股,不依赖任何价格动量信号。
- 估值约束: PE/PB 合理,避免为成长支付过高溢价(成长陷阱)。
选股: 全 A 中 ROE>阈值 + ROE 稳定 + 净利润同比稳健正增(如 5%~60% 且波动小) + 估值合理,综合打分取前 N 等权。
风控: regime=风险 时收紧(只留高 ROE 稳定 + 便宜);单行业上限分散;可行业轮动(偏好 top_bullish_sectors 强势行业加分)。
信息面: 宏观(regime/行业动量)+ 行业(申万)+ 基本面(ROE/盈利增长/估值)+ 技术(仅作流动性/可交易性过滤)+ 资金面(成交额)。
不含 ETF,仅交易个股。严格用 pub_date<=date 防未来函数。
"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s11")

import re
_ETF_RE = re.compile(r"^(sh5\d{2}|sz1[5-9]\d{2})")


def _is_etf(code):
    return bool(_ETF_RE.match(code))


def _earnings_profile(ctx, code, date, n=4):
    """返回 (latest_roe, roe_std, yoy_growth, growth_std, ok)。仅用 pub_date<=date 的历史年报,防未来函数。"""
    rows = ctx.conn.execute(
        "SELECT stat_year, roe, net_profit, pub_date FROM stock_annual "
        "WHERE code=? AND pub_date<=? ORDER BY stat_year DESC", (code, date)
    ).fetchall()
    if not rows:
        return (None, None, None, None, False)
    roes = [r[1] for r in rows if r[1] is not None]
    if len(roes) < 2:
        return (roes[0] if roes else None, None, None, None, False)
    latest_roe = roes[0]
    roe_std = (max(roes) - min(roes)) if len(roes) >= 2 else 0.0  # 用极差近似稳定性(越小越稳)
    # 净利润同比: 用相邻两年 net_profit
    profits = [(r[0], r[2]) for r in rows if r[2] is not None]
    growths = []
    for (y0, p0), (y1, p1) in zip(profits, profits[1:]):
        if p1 is not None and p1 > 0 and p0 is not None and p0 > 0:
            growths.append(p0 / p1 - 1.0)
    if len(growths) < 1:
        return (latest_roe, roe_std, None, None, False)
    yoy = growths[0]  # 最新一期同比
    growth_std = (max(growths) - min(growths)) if len(growths) >= 2 else 0.0
    return (latest_roe, roe_std, yoy, growth_std, True)


class S11QualityGrowth(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        import fundamental as F
        import macro as _macro
        try:
            import factors as _fac
        except Exception:
            _fac = None

        hold_n = self.params.get("hold_n", 15)
        min_avg_amount = self.params.get("min_avg_amount", 50_000_000)
        min_market_cap = self.params.get("min_market_cap", 3_000_000_000)
        roe_min = self.params.get("roe_min", 0.10)
        roe_std_max = self.params.get("roe_std_max", 0.15)        # ROE 极差上限(稳定性)
        yoy_min = self.params.get("yoy_min", 0.05)                # 净利润同比下限
        yoy_max = self.params.get("yoy_max", 0.60)                # 上限,排除一次性暴增
        growth_std_max = self.params.get("growth_std_max", 0.50)  # 增长波动上限
        max_pe = self.params.get("max_pe", 45.0)
        max_pb = self.params.get("max_pb", 8.0)
        ind_cap = self.params.get("max_per_industry", 3)
        derisk_factor = self.params.get("derisk_hold_ratio", 0.6)

        # 宏观 regime
        try:
            _reg = _macro.compute_market_regime(date, conn=ctx.conn).get("regime", "震荡")
        except Exception:
            _reg = "震荡"
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        if _reg == "风险":
            eff = max(3, int(eff * derisk_factor))
        w = common.target_weight(eff)

        # 行业动量偏好(强势行业加分)
        bull_set = set()
        try:
            secs = _macro.top_bullish_sectors(date, conn=ctx.conn, top=6)
            for s in secs:
                bull_set.add(s[0] if isinstance(s, (list, tuple)) else s)
        except Exception:
            pass

        all_codes = [r[0] for r in ctx.conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]
        cand = []  # (code, roe, yoy, growth_std, pe, pb, bull_bonus)
        for code in all_codes:
            if _is_etf(code):
                continue
            if not ctx.is_tradable(code, date):
                continue
            f = ctx.fundamental(code)
            if not f:
                continue
            mc = f.get("market_cap") or 0
            if mc < min_market_cap:
                continue
            if (f.get("pe") is None or f["pe"] <= 0 or f["pe"] > max_pe):
                continue
            if (f.get("pb") is None or f["pb"] <= 0 or f["pb"] > max_pb):
                continue
            if ctx.avg_amount(code, 20) < min_avg_amount:
                continue
            prof = _earnings_profile(ctx, code, date, n=4)
            roe, roe_std, yoy, gstd, ok = prof
            if not ok or roe is None or yoy is None:
                continue
            if roe < roe_min or (roe_std is not None and roe_std > roe_std_max):
                continue
            if yoy < yoy_min or yoy > yoy_max:
                continue
            if gstd is not None and gstd > growth_std_max:
                continue
            bull_bonus = 0.0
            if _fac is not None:
                try:
                    ind = _fac.get_industry(ctx.conn, [code]).get(code)
                    if ind in bull_set:
                        bull_bonus = 1.0
                except Exception:
                    pass
            cand.append((code, roe, yoy, gstd or 0.0, f["pe"], f["pb"], bull_bonus))

        if not cand:
            return []
        cand.sort(key=lambda x: -x[1])  # 候选池先按 ROE 截前 2*eff
        keep = cand[: max(eff * 3, 45)]
        by_roe = sorted(keep, key=lambda x: x[1], reverse=True)
        roe_rank = {c[0]: i for i, c in enumerate(by_roe)}
        by_yoy = sorted(keep, key=lambda x: x[2], reverse=True)
        yoy_rank = {c[0]: i for i, c in enumerate(by_yoy)}
        by_gstd = sorted(keep, key=lambda x: x[3])  # 增长波动越小越优
        gstd_rank = {c[0]: i for i, c in enumerate(by_gstd)}
        by_pe = sorted(keep, key=lambda x: (x[4] is None, x[4] if x[4] is not None else 1e9))
        pe_rank = {c[0]: i for i, c in enumerate(by_pe)}
        by_bull = sorted(keep, key=lambda x: x[6], reverse=True)
        bull_rank = {c[0]: i for i, c in enumerate(by_bull)}
        # 综合: 质量(ROE)35% + 成长(yoy)25% + 成长稳定(低波动)15% + 估值(PE低)15% + 行业动量10%
        scored = sorted(keep, key=lambda x: (0.35 * roe_rank[x[0]] + 0.25 * yoy_rank[x[0]]
                                            + 0.15 * gstd_rank[x[0]] + 0.15 * pe_rank[x[0]]
                                            + 0.10 * bull_rank[x[0]]))
        # 行业中性化
        if _fac is not None:
            try:
                ind_map = _fac.get_industry(ctx.conn, [c[0] for c in keep])
            except Exception:
                ind_map = {}
        else:
            ind_map = {}
        ind_count, target = {}, []
        for c in scored:
            code = c[0]
            ind = ind_map.get(code) or "未知"
            if ind_count.get(ind, 0) >= ind_cap:
                continue
            target.append(code)
            ind_count[ind] = ind_count.get(ind, 0) + 1
            if len(target) >= eff:
                break

        n_keep = len(keep)
        keep_info = {c[0]: c for c in keep}
        full_rank = {c[0]: i + 1 for i, c in enumerate(scored)}
        held = set(account.positions.keys())
        orders = []
        for code in held:
            if code not in target:
                nm = ctx.name(code)
                if code in full_rank:
                    reason = f"质量成长调仓:{nm}综合排名第{full_rank[code]}/{n_keep}掉出前{eff},卖出"
                else:
                    reason = f"质量成长:{nm}不再满足 ROE/盈利增长/估值门槛,卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                roe = keep_info[code][1]; yoy = keep_info[code][2]
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"质量成长:买入{ctx.name(code)}(ROE{roe:.1%}·净利同比{yoy:.1%}"
                                    f"·综合第{full_rank[code]}/{n_keep}{'·强势行业' if keep_info[code][6] else ''})", date))
        return orders
