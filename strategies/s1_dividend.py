# -*- coding: utf-8 -*-
"""S1 红利低波(SPEC 模块3+产业逻辑增强)。池:沪深300(大盘红利票)。
过滤:股息率≥4% + 连续3年现金分红 + 250日波动率处于剩余池后30%(低波);
打分=股息率排名40% + 低波排名30% + 基本面评分20% + 产业信号10%,取前N等权。月末调仓。

产业逻辑增强: 政策利好行业的高股息股获得额外加分(如化债利好银行股、设备更新利好制造业)。"""
import logging
from statistics import mean, pstdev
from models import Order
from strategies.base import BaseStrategy
from strategies import common

log = logging.getLogger("s1")
POOL_INDEX = "sh000300"   # 沪深300(大盘红利票;可改中证800需相应扩充回填)


class S1DividendLowVol(BaseStrategy):
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        min_dy = self.params.get("min_dividend_yield", 0.04)
        years = self.params.get("dividend_years", 3)
        low_vol_pct = self.params.get("low_vol_pct", 0.30)
        hold_n = self.params.get("hold_n", 10)
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        pool = ctx.members(POOL_INDEX, date)
        cand = []   # (code, div_yield, vol, fund_score)
        for code in pool:
            if not ctx.is_tradable(code, date):
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("dividend_yield") or f["dividend_yield"] < min_dy:
                continue
            if ctx.dividend_years(code, years) < years:
                continue
            c = ctx.close(code, 251)
            if len(c) < 200:
                continue
            rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
            vol = pstdev(rets) if len(rets) > 1 else 9.9
            fund_score = common.get_fundamental_score(ctx, code, date)
            cand.append((code, f["dividend_yield"], vol, fund_score))

        if not cand:
            return []
        # 低波后30%:按 vol 升序保留前 (1-0.3)? SPEC"位于剩余池后30%"=波动率最低的30%
        cand.sort(key=lambda x: x[2])
        keep = cand[:max(eff, int(len(cand) * low_vol_pct))]
        # 打分:股息率降序名次 + 低波(vol升序)名次 + 基本面评分
        by_dy = sorted(keep, key=lambda x: x[1], reverse=True)
        dy_rank = {c[0]: i for i, c in enumerate(by_dy)}
        by_vol = sorted(keep, key=lambda x: x[2])
        vol_rank = {c[0]: i for i, c in enumerate(by_vol)}
        # 基本面排名(分数越高越好,转为排名越小越好)
        by_fund = sorted(keep, key=lambda x: x[3], reverse=True)
        fund_rank = {c[0]: i for i, c in enumerate(by_fund)}

        # 综合打分: 股息率40% + 低波30% + 基本面30%
        n_keep = len(keep)
        scored = sorted(keep, key=lambda x: (0.4 * dy_rank[x[0]]
                                             + 0.3 * vol_rank[x[0]]
                                             + 0.3 * fund_rank[x[0]]))
        target = [c[0] for c in scored[:eff]]

        # —— 仅供理由展示(卡H):只读排名/数值,不参与选股 ——
        cand_codes = {c[0] for c in cand}
        keep_dy = {c[0]: c[1] for c in keep}
        keep_fund = {c[0]: c[3] for c in keep}
        full_rank = {c[0]: i + 1 for i, c in enumerate(scored)}   # 综合分名次(1=最优)

        held = set(account.positions.keys())
        orders = []
        for code in held:
            if code not in target:
                nm = ctx.name(code)
                if code in full_rank:
                    reason = f"红利低波调仓:{nm}综合排名第{full_rank[code]}/{n_keep}掉出前{eff},卖出"
                elif code in cand_codes:
                    reason = f"红利低波:{nm}波动率升高、掉出低波区,卖出"
                else:
                    reason = f"红利低波:{nm}不再满足股息率≥{min_dy:.0%}或连续{years}年分红门槛,卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                dy = keep_dy[code]
                dyr = dy_rank[code] + 1                            # 股息率名次(降序,1=最高)
                volpct = round((vol_rank[code] + 1) / n_keep * 100)  # 波动率池内分位(越低越稳)
                fund = keep_fund[code]
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"红利低波:买入{ctx.name(code)}(股息率{dy:.1%}第{dyr}/{n_keep}"
                                    f"·波动率池内最低{volpct}%·基本面{fund:.2f})", date))
        return orders


class S1DividendQuality(BaseStrategy):
    """S1 v2 质量增强(卡D+产业逻辑增强)。在 v1 基础上:①过滤加"连续3年 ROE>8% 且 净利润>0";
    ②打分=股息率35% + 低波25% + ROE25% + 产业信号15%(排名法)。v1 并行不受影响。月末调仓,等权持有。"""
    def generate_orders(self, date, ctx, account):
        if not ctx.is_last_trade_day_of_month(date):
            return []
        import fundamental as F
        min_dy = self.params.get("min_dividend_yield", 0.04)
        years = self.params.get("dividend_years", 3)
        low_vol_pct = self.params.get("low_vol_pct", 0.30)
        roe_years = self.params.get("roe_years", 3)
        roe_min = self.params.get("roe_min", 0.08)
        weights = self.params.get("weights", {"dividend": 0.35, "low_vol": 0.25, "roe": 0.25, "news": 0.15})
        hold_n = self.params.get("hold_n", 10)
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)
        w = common.target_weight(eff)

        pool = ctx.members(POOL_INDEX, date)
        cand = []   # (code, div_yield, vol, roe, news_score)
        for code in pool:
            if not ctx.is_tradable(code, date):
                continue
            f = ctx.fundamental(code)
            if not f or not f.get("dividend_yield") or f["dividend_yield"] < min_dy:
                continue
            if ctx.dividend_years(code, years) < years:
                continue
            ok, roe = F.roe_quality(code, date, years=roe_years, min_roe=roe_min, conn=ctx.conn)
            if not ok:
                continue
            c = ctx.close(code, 251)
            if len(c) < 200:
                continue
            rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
            vol = pstdev(rets) if len(rets) > 1 else 9.9
            # 获取新闻语义分
            news_score = 0.0
            try:
                import news_engine as ne
                news_score = ne.get_stock_sentiment_score(date, code, conn=ctx.conn)
            except Exception:
                pass
            cand.append((code, f["dividend_yield"], vol, roe, news_score, f.get("pe")))
        if not cand:
            return []

        cand.sort(key=lambda x: x[2])                              # 低波后30%(同v1)
        keep = cand[:max(eff, int(len(cand) * low_vol_pct))]
        by_dy = sorted(keep, key=lambda x: x[1], reverse=True)
        dy_rank = {c[0]: i for i, c in enumerate(by_dy)}
        by_vol = sorted(keep, key=lambda x: x[2])
        vol_rank = {c[0]: i for i, c in enumerate(by_vol)}
        by_roe = sorted(keep, key=lambda x: x[3], reverse=True)
        roe_rank = {c[0]: i for i, c in enumerate(by_roe)}
        # 新闻面排名(利好排名靠前)
        by_news = sorted(keep, key=lambda x: x[4], reverse=True)
        news_rank = {c[0]: i for i, c in enumerate(by_news)}
        # 估值分位:PE 升序排名(越低越便宜→名次越小=越优);缺失PE者给中性名次
        by_pe = sorted(keep, key=lambda x: (x[5] is None, x[5] if x[5] is not None else 1e9))
        pe_rank = {c[0]: i for i, c in enumerate(by_pe)}
        wd = weights.get("dividend", 0.32)
        wv = weights.get("low_vol", 0.22)
        wr = weights.get("roe", 0.22)
        wv2 = weights.get("valuation", 0.11)   # 估值(PE分位,越低越好)
        wn = weights.get("news", 0.13)
        scored = sorted(keep, key=lambda x: (wd * dy_rank[x[0]]
                                             + wv * vol_rank[x[0]]
                                             + wr * roe_rank[x[0]]
                                             + wv2 * pe_rank[x[0]]
                                             + wn * news_rank[x[0]]))
        # 行业中性化:单行业持股上限,避免红利天然扎堆银行/公用/煤炭
        ind_cap = self.params.get("max_per_industry", 3)
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
        cand_codes = {c[0] for c in cand}
        keep_dy = {c[0]: c[1] for c in keep}
        keep_roe = {c[0]: c[3] for c in keep}
        full_rank = {c[0]: i + 1 for i, c in enumerate(scored)}

        held = set(account.positions.keys())
        orders = []
        for code in held:
            if code not in target:
                nm = ctx.name(code)
                if code in full_rank:
                    reason = f"红利质量调仓:{nm}综合排名第{full_rank[code]}/{n_keep}掉出前{eff},卖出"
                elif code in cand_codes:
                    reason = f"红利质量:{nm}波动率升高、掉出低波区,卖出"
                else:
                    reason = f"红利质量:{nm}不再满足股息率≥{min_dy:.0%}/连续分红/ROE≥{roe_min:.0%}门槛,卖出"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
        for code in target:
            if code not in held:
                dy = keep_dy[code]; roe = keep_roe[code]
                dyr = dy_rank[code] + 1
                orders.append(Order(self.strategy_id, code, "buy", w,
                                    f"红利质量:买入{ctx.name(code)}(股息率{dy:.1%}第{dyr}/{n_keep}"
                                    f"·ROE{roe:.1%}·低波·综合第{full_rank[code]}/{n_keep})", date))
        return orders
