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
from strategies import news_guard
from strategies import mf_core

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
        # 市场分/宏观敞口统一由 risk 层 _exposure_mult 处理(单一权威,避免双重缩放)

        pool = ctx.members(POOL_INDEX, date)
        pool = common.main_board_universe(ctx, pool, self.config, date)  # 主板宇宙硬约束(手册)
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
        # —— 新闻/公告/动态守卫(全量接入) ——
        _cc = [c[0] for c in cand]
        try:
            import factors as _fac
            _ind = _fac.get_industry(ctx.conn, _cc)
        except Exception:
            _ind = {}
        _ban_n, _ = news_guard.guard_candidates(date, _cc, ctx.conn, self.config)
        _ban_i = news_guard.guard_industry(date, _cc, ctx.conn, self.config, _ind)
        _ban_s = {c for c in _cc if news_guard.structural_ban(date, c, ctx)[0]}
        _banned = _ban_n | _ban_i | _ban_s
        if _banned:
            cand = [c for c in cand if c[0] not in _banned]
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
        forced = news_guard.guard_holdings(date, held, ctx.conn, self.config)
        for code in held:
            if code in target and code not in forced:
                continue
            nm = ctx.name(code)
            if code in forced:
                reason = f"红利低波:{nm}新闻黑天鹅,同步清仓"
            elif code in full_rank:
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
    """S1 v2 质量增强 —— 重建在已验证的红利质量多因子底座(mf_core)之上。

    原 v2 独立打分逻辑在 42 只大蓝筹宇宙里出现严重回归(年化仅 1.0%), 根因是
    动量/行业地位排名与股息低波逻辑相互打架。本版直接复用 mf_core(被 s4/s14 验证
    达标的底座), 仅以"股息率倾斜"作为 s1 的风格标签, 叠加动量(0.35)/宏观降仓
    (regime_bad 0.70)/跟踪止损(0.11) 控回撤, 并由 risk 层熔断线 0.05 兜底硬约束(回撤≤5%)。

    消息面最大化: news 权重 0.10 + industry(个股行业地位/ROE龙头代理) 0.08 已内置于
    mf_core 打分; 回测中 news 库为空(恒为0, 由 industry 代理), 实盘接 news_engine 真实舆情。
    """
    def generate_orders(self, date, ctx, account):
        if not mf_core.should_rebalance(date, self.params):
            return mf_core.risk_orders(date, ctx, account, self.params, self.strategy_id, self.config)

        # 调优锁定(s1/C, 锚定 s4/s14 已验证配方 + 股息率倾斜):
        # 股息率权重最高(0.22, s1 身份标志) + 动量0.35(收益引擎) + regime_bad 0.75(松化降仓保收益)
        # + 低波0.55(扩候选) + 止损0.13(控回撤入5%) + news0.10/industry0.08(消息面最大化)。
        _defaults = {
            "min_dividend_yield": 0.035,   # s1 偏红利, floor 略高于 s4(0.025)/s14(0.03)
            "dividend_years": 3,
            "roe_years": 3,
            "roe_min": 0.08,
            "hold_n": 10,                   # s1 等权持有 10 只(比 s4/s14 的 8 只更分散, 压回撤)
            "max_per_industry": 3,
            "low_vol_pct": 0.55,            # 放宽低波过滤, 保留更多含收益标的
            "momentum_window": 252,
            "momentum_skip": 21,
            "momentum_min": 0.0,            # 上行趋势门槛(同 s14): 剔除走弱票, 控回撤
            # round-6 对齐 s4/s14 达标配方: regime_mid 1.0(不降仓)/ bad 0.75(温和集中),
            # 敞口降仓交给 risk 层; 早期压低 mid/bad 造成过度集中反拖累, 已回退。
            "regime_downsize": True,
            "regime_good": 1.0, "regime_mid": 1.0, "regime_bad": 0.75,
            "weights": {"dividend": 0.22, "low_vol": 0.10, "roe": 0.15,
                        "valuation": 0.10, "news": 0.10, "industry": 0.08, "momentum": 0.35},
        }
        # registry params 覆盖硬编码默认(扩池差异化: pool_index/cap_segment/weights 等由 registry 注入)
        params = {**_defaults, **dict(self.params)}
        sel = mf_core.select(ctx, date, account, params, self.strategy_id, self.config)
        if not sel["target"]:
            forced = news_guard.guard_holdings(date, list(account.positions.keys()), ctx.conn, self.config)
            return [Order(self.strategy_id, code, "sell", 0.0,
                          f"红利质量:{ctx.name(code)}新闻黑天鹅,清仓", date)
                    for code in account.positions.keys() if code in forced] + \
                   [Order(self.strategy_id, code, "sell", 0.0,
                          f"红利质量:{ctx.name(code)}无候选,清仓", date)
                    for code in account.positions.keys() if code not in forced]
        return mf_core.build_orders(ctx, date, account, sel, params,
                                    self.strategy_id, self.config, stop_pct=0.11)
