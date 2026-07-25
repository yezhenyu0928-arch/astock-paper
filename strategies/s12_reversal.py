# -*- coding: utf-8 -*-
"""S12 反转均值回归经理 (Mean-Reversion / Contrarian)。

投资经理画像: 逆向交易型,赚"过度反应后的均值回归"的钱。
设计依据(来自 A 股因子实证):
- A 股短中期(1~3 月)呈显著【反转】,尤其中小盘价格动量甚至为负——追涨杀跌导致错误定价。
  与已下线的 s3/s9(顺价格趋势/动量)恰好相反: 本策略主动买"近期相对超跌"的优质股,等回归。
- 但纯反转会接"下跌飞刀",故加【基本面安全网】: ROE>0、净利润为正、估值不荒诞,排除基本面恶化的真垃圾。
- 限制回撤幅度: 只选"温和超跌"(如 60 日收益 -35%~-5%),避开暴跌腰斩的价值陷阱。
- 质量/估值打分在超跌股中优中选优,月度(或双月)再平衡捕捉反转。
选股: 全 A 中近期收益靠后(超跌)+ 质量过关 + 估值合理,综合"超跌强度+质量+便宜"取前 N 等权。
风控: regime=风险 时大幅收紧(只留质量极高 + 跌幅温和),避免下行趋势中接飞刀;单行业上限分散。
信息面: 宏观(regime)+ 行业(申万中性)+ 基本面(ROE/盈利/估值)+ 技术(近期收益反转)+ 资金面(成交额流动性)。
不含 ETF,仅交易个股。严格用 pub_date<=date 防未来函数。
"""
import logging
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s12")

import re
_ETF_RE = re.compile(r"^(sh5\d{2}|sz1[5-9]\d{2})")


def _is_etf(code):
    return bool(_ETF_RE.match(code))


def _recent_return(ctx, code, win):
    c = ctx.close(code, win + 1)
    if len(c) < win or c[0] <= 0:
        return None
    return c[-1] / c[0] - 1.0


class S12Reversal(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        import fundamental as F
        import macro as _macro
        try:
            import factors as _fac
        except Exception:
            _fac = None

        hold_n = self.params.get("hold_n", 18)
        min_avg_amount = self.params.get("min_avg_amount", 40_000_000)
        min_market_cap = self.params.get("min_market_cap", 3_000_000_000)
        lookback = self.params.get("lookback", 60)            # 反转观察窗口(日)
        drop_min = self.params.get("drop_min", -0.35)         # 超跌下限(跌太多=陷阱,排除)
        drop_max = self.params.get("drop_max", -0.04)         # 超跌上限(几乎没跌=不算超跌)
        roe_min = self.params.get("roe_min", 0.05)
        max_pe = self.params.get("max_pe", 50.0)
        max_pb = self.params.get("max_pb", 8.0)
        ind_cap = self.params.get("max_per_industry", 3)
        de_risk_hold_ratio = self.params.get("derisk_hold_ratio", 0.5)

        # 宏观 regime
        try:
            _reg = _macro.compute_market_regime(date, conn=ctx.conn).get("regime", "震荡")
        except Exception:
            _reg = "震荡"
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        if _reg == "风险":
            eff = max(3, int(eff * de_risk_hold_ratio))
        w = common.target_weight(eff)

        all_codes = [r[0] for r in ctx.conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]
        cand = []  # (code, ret, roe, pe, pb)
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
            ret = _recent_return(ctx, code, lookback)
            if ret is None or ret > drop_max or ret < drop_min:
                continue  # 仅保留"温和超跌"区间,排除没跌的与暴跌陷阱
            ok, roe = F.roe_quality(code, date, years=2, min_roe=roe_min, conn=ctx.conn)
            if not ok:
                continue
            cand.append((code, ret, roe, f["pe"], f["pb"]))

        if not cand:
            return []
        cand.sort(key=lambda x: x[1])  # 候选池先按超跌程度截前 2*eff
        keep = cand[: max(eff * 2, 40)]
        by_ret = sorted(keep, key=lambda x: x[1])  # 跌越多名次越前(反转强度)
        ret_rank = {c[0]: i for i, c in enumerate(by_ret)}
        by_roe = sorted(keep, key=lambda x: x[2], reverse=True)
        roe_rank = {c[0]: i for i, c in enumerate(by_roe)}
        by_pe = sorted(keep, key=lambda x: (x[3] is None, x[3] if x[3] is not None else 1e9))
        pe_rank = {c[0]: i for i, c in enumerate(by_pe)}
        # 综合: 反转强度45% + 质量30% + 估值便宜25%(PE低)
        scored = sorted(keep, key=lambda x: (0.45 * ret_rank[x[0]] + 0.30 * roe_rank[x[0]] + 0.25 * pe_rank[x[0]]))
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
                    reason = f"反转调仓:{nm}综合排名第{full_rank[code]}/{n_keep}掉出前{eff}(已反弹或质量变差),卖出"
                else:
                    reason = f"反转:{nm}不再满足超跌/质量/估值门槛,卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                ret = keep_info[code][1]; roe = keep_info[code][2]
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"反转均值回归:买入{ctx.name(code)}(60日超跌{ret:.1%}·ROE{roe:.1%}"
                                    f"·综合第{full_rank[code]}/{n_keep}{'·[风险市减仓]' if _reg=='风险' else ''})", date))
        return orders
