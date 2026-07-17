# -*- coding: utf-8 -*-
"""S10 低波防御经理 (Low-Volatility / Min-Vol Defensive)。

投资经理画像: 稳健配置型,追求"安稳向上"的收益曲线,熊市抗跌、牛市不落后太多。
核心因子(均经 A 股实证长期有效):
- 低波动异象: 低波动股票长期跑赢高波动(RankIC 0.1~0.18,各市值均显著)。
  A 股高波股多为题材炒作,长期收益反而差;低波股估值低、泡沫小,夏普更高。
- 质量门槛: ROE>0 且为正,排除亏损/壳股,避免"低波价值陷阱"(如长期低迷的僵尸股)。
- 估值约束: PE/PB 合理(排除极度高估与负值),避免低波但昂贵的防御陷阱。
综合: 在流动性充裕的个股中,选"波动率最低 + 质量过关 + 估值合理"的一篮子,等权持有,月度再平衡。
风控: 市场 regime=风险 时自动减仓(有效持仓数减半),不逆势加仓;单行业持股上限做分散。
信息面: 宏观(regime)+ 基本面(ROE/估值)+ 技术(波动率)+ 资金面(成交额流动性),可行业轮动。
不含任何 ETF,仅交易 A 股个股。
"""
import logging
from statistics import pstdev, mean
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s10")

# ETF 代码特征(沪 sh5xx / 深 sz1[5-9]xx),显式排除,确保纯个股
import re
_ETF_RE = re.compile(r"^(sh5\d{2}|sz1[5-9]\d{2})")


def _is_etf(code):
    return bool(_ETF_RE.match(code))


def _volatility(ctx, code, win):
    c = ctx.close(code, win + 1)
    if len(c) < win:
        return None
    rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c)) if c[i - 1] > 0]
    if len(rets) < 20:
        return None
    return pstdev(rets)


class S10LowVol(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        import fundamental as F
        import macro as _macro

        hold_n = self.params.get("hold_n", 20)
        min_avg_amount = self.params.get("min_avg_amount", 50_000_000)
        min_market_cap = self.params.get("min_market_cap", 5_000_000_000)  # 5亿,排除微盘极端波动
        max_market_cap = self.params.get("max_market_cap", 1_000_000_000_000)
        vol_win = self.params.get("vol_window", 250)
        roe_min = self.params.get("roe_min", 0.05)
        max_pe = self.params.get("max_pe", 40.0)
        max_pb = self.params.get("max_pb", 6.0)
        ind_cap = self.params.get("max_per_industry", 3)
        derisk_factor = self.params.get("derisk_hold_ratio", 0.5)

        # 宏观 regime 防御
        try:
            _reg = _macro.compute_market_regime(date, conn=ctx.conn).get("regime", "震荡")
        except Exception:
            _reg = "震荡"
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        if _reg == "风险":
            eff = max(3, int(eff * derisk_factor))
        w = common.target_weight(eff)

        # 动态宇宙: 全 A 个股(排除 ETF),流动性 + 市值门槛
        all_codes = [r[0] for r in ctx.conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code LIKE 'sh%' OR code LIKE 'sz%'").fetchall()]
        cand = []  # (code, vol, roe, pe, pb)
        for code in all_codes:
            if _is_etf(code):
                continue
            if not ctx.is_tradable(code, date):
                continue
            f = ctx.fundamental(code)
            if not f:
                continue
            mc = f.get("market_cap") or 0
            if mc < min_market_cap or mc > max_market_cap:
                continue
            if (f.get("pe") is None or f["pe"] <= 0 or f["pe"] > max_pe):
                continue
            if (f.get("pb") is None or f["pb"] <= 0 or f["pb"] > max_pb):
                continue
            if ctx.avg_amount(code, 20) < min_avg_amount:
                continue
            vol = _volatility(ctx, code, vol_win)
            if vol is None:
                continue
            ok, roe = F.roe_quality(code, date, years=2, min_roe=roe_min, conn=ctx.conn)
            if not ok:
                continue
            cand.append((code, vol, roe, f["pe"], f["pb"]))

        if not cand:
            return []
        # 主排序: 波动率升序(越低越优)
        cand.sort(key=lambda x: x[1])
        keep = cand[: max(eff * 2, 40)]  # 先留候选池,再做行业中性截断
        # 打分: 低波为主 + 质量(ROE) + 估值便宜度(PE升序)
        by_vol = sorted(keep, key=lambda x: x[1])
        vol_rank = {c[0]: i for i, c in enumerate(by_vol)}
        by_roe = sorted(keep, key=lambda x: x[2], reverse=True)
        roe_rank = {c[0]: i for i, c in enumerate(by_roe)}
        by_pe = sorted(keep, key=lambda x: (x[3] is None, x[3] if x[3] is not None else 1e9))
        pe_rank = {c[0]: i for i, c in enumerate(by_pe)}
        scored = sorted(keep, key=lambda x: (0.6 * vol_rank[x[0]] + 0.25 * roe_rank[x[0]] + 0.15 * pe_rank[x[0]]))
        # 行业中性化
        try:
            import factors as _fac
            ind_map = _fac.get_industry(ctx.conn, [c[0] for c in keep])
        except Exception:
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
        keep_vol = {c[0]: c[1] for c in keep}
        keep_roe = {c[0]: c[2] for c in keep}
        full_rank = {c[0]: i + 1 for i, c in enumerate(scored)}
        held = set(account.positions.keys())
        orders = []
        for code in held:
            if code not in target:
                nm = ctx.name(code)
                if code in full_rank:
                    reason = f"低波防御调仓:{nm}波动率分位上升,综合排名第{full_rank[code]}/{n_keep}掉出前{eff},卖出"
                else:
                    reason = f"低波防御:{nm}不再满足低波/质量/估值门槛,卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                vol = keep_vol[code]; roe = keep_roe[code]
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"低波防御:买入{ctx.name(code)}(250日波动率{vol:.3f}·ROE{roe:.1%}·综合第{full_rank[code]}/{n_keep}"
                                    f"{'·[风险市减仓]' if _reg=='风险' else ''})", date))
        return orders
