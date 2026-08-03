# -*- coding: utf-8 -*-
"""race_v2 系列 —— 依据 factor_diagnostics 实证结论重建的 6 只真独立赛马策略。

与旧 mf_core 六策略(同一底座不同参数)的本质区别:
  · 每只策略调用 factor_engine 的不同因子组合, 对应不同 Alpha 来源
  · 因子均经 去极值→行业中性化→(市值中性化)→百分位标准化 处理
  · 调仓日做**定期等权再平衡**(把全部持仓拉回目标权重, 而非只补新票)
  · 非调仓日做**波动率自适应止损**(低波票止损紧, 高波票放宽)
  · 动量/SUE/high52 因诊断无效(A股动量弱/反转强)已从权重中剔除或弱化

诊断依据(reports/factor_diagnostics.md, mainboard 2019-2026):
  反转 rev1m ICIR-0.39(G1月收益8.8% vs G10-0.5%) ← 最强Alpha
  低波 lowvol ICIR+0.39  低流动 liq ICIR+0.59(与规模-0.78相关)
  PB价值 bm IC+0.046 ICIR+0.25(与规模秩相关0.00, 独立)  EP IC+0.018 弱
  规模 size IC-0.013(近年小盘降温, 弱化)  动量/SUE/high52 全灭

策略池现实: 本地/云端 DB 有 mainboard 3044 / all_a 5077 / 沪深300成分300,
  stock_annual 仅沪深300(质量因子限大盘池), profit_q 全市场(成长/盈利加速)。
所有取数经 ctx(<=signal_date) 防未来函数。仅读 DB, 不联网(云端 runner 可行)。
"""
import logging
import math
import numpy as np

from models import Order
from strategies.base import BaseStrategy
from strategies import factor_engine as fe
from strategies import news_guard
from strategies import common

log = logging.getLogger("race_v2")

# 默认配置(registry params 覆盖)
REBAL_DAILY = "daily"
REBAL_WEEKLY = "weekly"
REBAL_MONTHLY = "monthly"


# ======================================================================
# 工具: 池 + 排名 + 再平衡
# ======================================================================
def _cap_segment(ctx, pool, date, segment, min_cap=10):
    """按 log市值分位切市值段(复用 factor_engine.factor_logcap)。
    segment: 'small'(后45%) / 'mid'(30-70%) / 'large'(前33%) / None(全池)。
    市值缺失票: 排末尾(不误入 small 段)。"""
    if not segment:
        return pool
    logcap = fe.factor_logcap(fe._fac._pool_fundamental(ctx.conn, pool, date))
    withcap = [(c, lc) for c, lc in logcap.items() if lc is not None and math.isfinite(lc)]
    if len(withcap) < min_cap:
        return pool
    withcap.sort(key=lambda x: x[1], reverse=True)
    n = len(withcap)
    if segment == "large":
        seg = {c for c, _ in withcap[:max(1, int(n * 0.33))]}
    elif segment == "mid":
        seg = {c for c, _ in withcap[int(n * 0.30):int(n * 0.70)]}
    elif segment == "small":
        seg = {c for c, _ in withcap[int(n * 0.55):]}
    else:
        return pool
    return [c for c in pool if c in seg]


def _pool_for(ctx, date, pool_index, cap_segment, cfg, min_market_cap=None):
    """构建可投池: 基础池(index_members) → 主板/全A过滤 → 市值段切分 → 流动性过滤。
    min_market_cap: 自定义市值下限(元), 覆盖 config 全局 risk.min_market_cap(80亿硬约束)。
    小市值策略需放开门槛捕获真小盘; 默认继承全局(安全)。"""
    if pool_index == "all_a":
        base = ctx.members("all_a", date)
        # all_a_universe 用 risk.min_market_cap_all_a(默认0不卡小盘); 自定义门槛在此注入
        if min_market_cap is not None:
            orig = cfg.get("risk", {}).get("min_market_cap_all_a")
            cfg["risk"]["min_market_cap_all_a"] = min_market_cap
        try:
            pool = common.all_a_universe(ctx, base, cfg, date)
        finally:
            if min_market_cap is not None and orig is not None:
                cfg["risk"]["min_market_cap_all_a"] = orig
    elif pool_index and pool_index.startswith("sh") and pool_index not in ("mainboard", "all_a"):
        # 显式指数池(如 sh000300 沪深300): 直接用其成分 + 主板可交易过滤
        base = ctx.members(pool_index, date)
        if min_market_cap is not None:
            orig = cfg.get("risk", {}).get("min_market_cap")
            cfg["risk"]["min_market_cap"] = min_market_cap
        try:
            pool = common.main_board_universe(ctx, base, cfg, date)
        finally:
            if min_market_cap is not None and orig is not None:
                cfg["risk"]["min_market_cap"] = orig
    else:
        base = ctx.members("mainboard", date) or ctx.members("sh000300", date)
        # 自定义市值门槛: 临时覆盖 config(避免硬约束过滤真小盘)
        if min_market_cap is not None:
            orig = cfg.get("risk", {}).get("min_market_cap")
            cfg["risk"]["min_market_cap"] = min_market_cap
        try:
            pool = common.main_board_universe(ctx, base, cfg, date)
        finally:
            if min_market_cap is not None and orig is not None:
                cfg["risk"]["min_market_cap"] = orig
    pool = _cap_segment(ctx, pool, date, cap_segment)
    return pool


def _rank_pool(ctx, date, pool, weights, ind_neutral=True, cap_neutral_after=None):
    """对池内股票算复合分(0~1)。返回 {code: score}。"""
    sc = fe.composite_scores(pool, date, ctx.conn, weights,
                             ind_neutral=ind_neutral, cap_neutral_after=cap_neutral_after)
    return {c: float(s) for c, s in sc.items() if s is not None and math.isfinite(s)}


def _vol_pct(ctx, date, code, n=60):
    """个股近 n 日年化波动率。"""
    try:
        rows = ctx.conn.execute(
            "SELECT close, adj_factor FROM daily_bar WHERE code=? AND trade_date<=? "
            "ORDER BY trade_date DESC LIMIT ?", (code, str(date)[:10], n + 1)).fetchall()
        closes = [r[0] * (r[1] or 1.0) for r in reversed(rows)]
        if len(closes) < 20:
            return None
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        return float(np.std(rets, ddof=1)) * math.sqrt(252)
    except Exception:
        return None


def _adaptive_stop(code, date, ctx, base=0.10, k=1.8, floor=0.08, ceil=0.35):
    """波动率自适应止损: stop = clip(base + (vol_60-0.30)*k, floor, ceil)。
    基准 10%, 30%年化波动→10%, 每+/-10%波动 → 止损+/-1.8%。"""
    vol = _vol_pct(ctx, date, code)
    if vol is None:
        return base
    stop = base + (vol - 0.30) * k
    return min(ceil, max(floor, stop))


def _weekly_last(ctx, date):
    return ctx.is_last_trade_day_of_week(str(date)[:10])


def _monthly_last(ctx, date):
    return ctx.is_last_trade_day_of_month(str(date)[:10])


# ======================================================================
# 通用多因子策略基类(六策略共享骨架, 因子组合各自不同)
# ======================================================================
class _FactorRaceStrategy(BaseStrategy):
    """多因子选股 + 定期等权再平衡 + 波动率自适应止损。"""
    # 子类需定义:
    factor_weights = {}          # {factor: weight}
    pool_index = "mainboard"
    cap_segment = None
    rebalance = "monthly"
    ind_neutral = True
    cap_neutral_after = None
    stop_base = 0.10
    liquidity_floor = None       # 成交额门槛(元), None=用common默认
    _last_scores = {}

    def should_reb(self, date, ctx):
        r = self.params.get("rebalance", self.rebalance)
        if r == "daily":
            return True
        if r == "weekly":
            return _weekly_last(ctx, date)
        return _monthly_last(ctx, date)

    def _liquidity_ok(self, ctx, date, code):
        floor = self.params.get("liquidity_floor", self.liquidity_floor)
        if not floor:
            return True
        try:
            return ctx.avg_amount(code, 20) >= floor
        except Exception:
            return True

    def generate_orders(self, date, ctx, account):
        date_s = str(date)[:10]
        params = self.params
        weights = params.get("factor_weights", self.factor_weights)
        if not weights:
            return []
        hold_n = int(params.get("hold_n", 10))
        max_per_ind = int(params.get("max_per_industry", 3))
        eff = common.effective_hold_n(hold_n, account.init_capital, self.config, self.strategy_id)

        # —— 非调仓日: 波动率自适应止损 + 新闻守卫清仓 ——
        if not self.should_reb(date, ctx):
            return self._daily_stops(date, ctx, account)

        # —— 调仓日: 构建池 → 排名 → 目标组合 ——
        pool = _pool_for(ctx, date_s, self.pool_index,
                         params.get("cap_segment", self.cap_segment), self.config,
                         min_market_cap=params.get("min_market_cap"))
        if len(pool) < eff:
            log.warning("%s %s 池不足(%d<%d), 空仓", self.strategy_id, date_s, len(pool), eff)
            return self._liquidate_all(date, ctx, account)
        scores = _rank_pool(ctx, date_s, pool, weights,
                            ind_neutral=params.get("ind_neutral", self.ind_neutral),
                            cap_neutral_after=params.get("cap_neutral_after", self.cap_neutral_after))

        # 候选排雷(实盘有新闻才生效)
        _ban_n, _ = news_guard.guard_candidates(date_s, list(scores.keys()), ctx.conn, self.config)
        ind = fe._fac.get_industry(ctx.conn, list(scores.keys()))
        _ban_i = news_guard.guard_industry(date_s, list(scores.keys()), ctx.conn, self.config, ind)
        _ban_s = {c for c in scores if news_guard.structural_ban(date_s, c, ctx)[0]}
        banned = _ban_n | _ban_i | _ban_s

        # 流动性过滤(微盘/小盘)
        ranked = [(c, s) for c, s in scores.items()
                  if c not in banned and self._liquidity_ok(ctx, date_s, c)]
        self._last_scores = dict(ranked)

        # —— 防御管线(mf_core 同款顺序, 先过滤后排名) ——
        # 1) 低波预筛: 先按近60日波动率升序保留 low_vol_pct 比例(剔高波动雷)。
        #    ⚠ 必须按波动率本身排序(不是综合分), 与 mf_core `keep=cand[:len*low_vol_pct]` 一致。
        lvp = params.get("low_vol_pct", 1.0)
        if lvp and 0 < lvp < 1.0:
            vol_sorted = sorted(ranked, key=lambda x: _vol_pct(ctx, date_s, x[0]) or 9.9)
            keep_n = max(eff, int(len(vol_sorted) * lvp))
            keep_set = {c for c, _ in vol_sorted[:keep_n]}
            ranked = [x for x in ranked if x[0] in keep_set]
            self._last_scores = dict(ranked)

        # 2) 动量硬门槛(趋势防御): 仅保留 12-1动量 >= momentum_min 的票(剔除深跌趋势), 空仓等待。
        mom_min = params.get("momentum_min")
        if mom_min is not None and mom_min > -998:
            before = len(ranked)
            ranked = [x for x in ranked if self._mom12_1(ctx, date_s, x[0]) >= mom_min]
            if not ranked:
                log.info("%s %s 动量门槛剔除全部候选(%d→0), 空仓等待", self.strategy_id, date_s, before)
                return self._liquidate_all(date, ctx, account)

        # 3) 综合分排名(过滤后)
        ranked.sort(key=lambda x: x[1], reverse=True)
        self._last_scores = dict(ranked)

        # 行业上限(未知行业不占额度, 与 mf_core 一致)
        target = []
        ind_count = {}
        for code, sc in ranked:
            i = ind.get(code)
            if i and ind_count.get(i, 0) >= max_per_ind:
                continue
            target.append(code)
            if i:
                ind_count[i] = ind_count.get(i, 0) + 1
            if len(target) >= eff:
                break

        return self._rebalance_to_target(date, ctx, account, target, eff)

    def _target_weight(self, eff):
        return round(0.98 / eff, 6)

    def _rebalance_to_target(self, date, ctx, account, target, eff):
        """定期等权再平衡: 把全部持仓拉回目标权重(修漂移)。
        - 目标组合内的: 权重偏离目标>±5% → 买卖单调回
        - 目标组合外的持仓: 清仓
        - 目标组合内未持有: 买入
        """
        date_s = str(date)[:10]
        wgt = self._target_weight(eff)
        tset = set(target)
        orders = []
        held = set(account.positions.keys())
        forced = news_guard.guard_holdings(date_s, list(held), ctx.conn, self.config)

        # —— 轻量再平衡模式(rebalance_light=true): 只卖名单外/买新进, 不强制调回权重。
        #   旧 s26 用此模式(换手低、成本低), 在A股震荡市表现优于每月全量再平衡。
        #   (诊断:A股月度反转→每月全量调仓恰好买在反转点, 被自己的因子反向伤害) ——
        if self.params.get("rebalance_light"):
            for code in held:
                nm = ctx.name(code)
                if code in forced:
                    orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                        f"{self.strategy_id}:{nm}新闻黑天鹅,清仓", date))
                elif code not in tset:
                    orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                        f"{self.strategy_id}:{nm}掉出目标组合,卖出", date))
            for code in target:
                if code not in held:
                    nm = ctx.name(code)
                    sc = getattr(self, "_last_scores", {}).get(code, 0.0)
                    orders.append(Order(self.strategy_id, code, "buy", wgt,
                                        f"{self.strategy_id}:买入{nm}(复合分{sc:.2f})", date))
            return orders

        # 卖出: 名单外清仓 + 名单内超配下调
        for code in held:
            nm = ctx.name(code)
            pos = account.positions.get(code)
            pv = self._price_of_one(ctx, date_s, code)
            total = self._total(ctx, date_s, held, account)
            if total <= 0:
                continue
            cur_w = (pos.shares * pv) / total if pv else 0.0
            if code in forced:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                    f"{self.strategy_id}:{nm}新闻黑天鹅,清仓", date))
                continue
            if code not in tset:
                reason = f"{self.strategy_id}:{nm}掉出目标组合,再平衡清仓"
                orders.append(Order(self.strategy_id, code, "sell", 0.0, reason, date))
            elif cur_w > wgt * 1.05:   # 超配>5% → 卖到目标权重
                reason = f"{self.strategy_id}:{nm}权重{cur_w:.1%}>目标{wgt:.1%},等权再平衡卖出"
                orders.append(Order(self.strategy_id, code, "sell", wgt, reason, date))

        # 买入: 名单内未持有 + 低配补仓
        for code in target:
            nm = ctx.name(code)
            sc = getattr(self, "_last_scores", {}).get(code, 0.0)
            if code in held:
                pos = account.positions.get(code)
                pv = self._price_of_one(ctx, date_s, code)
                total = self._total(ctx, date_s, held, account)
                cur_w = (pos.shares * pv) / total if pv else 0.0
                if cur_w < wgt * 0.95:   # 低配>5% → 补到目标
                    reason = f"{self.strategy_id}:{nm}权重{cur_w:.1%}<目标{wgt:.1%},等权再平衡买入"
                    orders.append(Order(self.strategy_id, code, "buy", wgt, reason, date))
                continue
            reason = f"{self.strategy_id}:买入{nm}(复合分{sc:.2f})"
            orders.append(Order(self.strategy_id, code, "buy", wgt, reason, date))
        return orders

    def _daily_stops(self, date, ctx, account):
        """非调仓日: 波动率自适应止损 + 新闻守卫清仓。"""
        date_s = str(date)[:10]
        held = list(account.positions.keys())
        if not held:
            return []
        forced = news_guard.guard_holdings(date_s, held, ctx.conn, self.config)
        orders = []
        for code in held:
            nm = ctx.name(code)
            pos = account.positions.get(code)
            peak = getattr(pos, "highest_close", None) or getattr(pos, "avg_cost", None)
            closes = ctx.close(code, 1)
            close = closes[-1] if closes else None
            stop = _adaptive_stop(code, date_s, ctx,
                                  base=self.params.get("stop_base", self.stop_base))
            breach = (close is not None and peak is not None and close < peak * (1 - stop))
            banned, reason = news_guard.structural_ban(date_s, code, ctx)
            if code in forced:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                    f"{self.strategy_id}:{nm}新闻黑天鹅,清仓", date))
            elif banned:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                    f"{self.strategy_id}:{nm}{reason},清仓", date))
            elif breach:
                orders.append(Order(self.strategy_id, code, "sell", 0.0,
                                    f"{self.strategy_id}:{nm}自高点回撤>{stop:.0%}(波动率自适应止损),清仓", date))
        return orders

    def _liquidate_all(self, date, ctx, account):
        return [Order(self.strategy_id, c, "sell", 0.0,
                      f"{self.strategy_id}:{ctx.name(c)}池不足,清仓", date)
                for c in account.positions.keys()]

    def _price_of(self, ctx, date_s, held_codes):
        """返回 {code: 最近收盘价} dict。"""
        out = {}
        for code in held_codes:
            pv = self._price_of_one(ctx, date_s, code)
            if pv:
                out[code] = pv
        return out

    def _total(self, ctx, date_s, held_codes, account):
        """账户总资产 = 现金 + 持仓市值(按最近收盘价)。"""
        mv = 0.0
        for code in held_codes:
            pos = account.positions.get(code)
            if not pos:
                continue
            pv = self._price_of_one(ctx, date_s, code)
            if pv:
                mv += pos.shares * pv
        return account.cash + mv

    def _price_of_one(self, ctx, date_s, code):
        r = ctx.conn.execute(
            "SELECT close FROM daily_bar WHERE code=? AND trade_date<=? "
            "ORDER BY trade_date DESC LIMIT 1", (code, date_s)).fetchone()
        return r[0] if r else 0.0

    def _mom12_1(self, ctx, date_s, code):
        """12-1月动量(跳过最近1月): close[-22]/close[-253]-1。数据不足返回 None。"""
        try:
            rows = ctx.conn.execute(
                "SELECT close, adj_factor FROM daily_bar WHERE code=? AND trade_date<=? "
                "ORDER BY trade_date DESC LIMIT 253", (code, date_s)).fetchall()
            closes = [r[0] * (r[1] or 1.0) for r in reversed(rows)]
            if len(closes) < 253:
                return None
            c0, c1 = closes[-22], closes[-253]
            if c0 and c1 and c0 > 0 and c1 > 0:
                return c0 / c1 - 1
            return None
        except Exception:
            return None


# ======================================================================
# r1 小市值反转(Alpha: 规模+反转+低换手)
#   混合方案: 保留行业中性化(防单行业雷), 关市值中性化(保留小盘风格暴露=诊断确认的A股真超额)。
#   用 s26 验证配方: 低波预筛 low_vol_pct=0.5 + 动量门槛 momentum_min=-0.10 + 轻量再平衡。
# ======================================================================
class RaceSmallRev(_FactorRaceStrategy):
    """r1 主板·小市值反转: 市值小段 + 1月反转 + 低换手 + 低波预筛防御。"""
    factor_weights = {"rev1m": -0.50, "size": -0.25, "amount": -0.15, "vol": -0.10}
    pool_index = "mainboard"
    cap_segment = "small"
    rebalance = "monthly"
    stop_base = 0.12
    ind_neutral = True
    cap_neutral_after = False      # 保留小盘风格暴露
    liquidity_floor = 50_000_000


# ======================================================================
# r2 质量价值(Alpha: 质量+价值, 行业+市值中性化 → 真Alpha)
# ======================================================================
class RaceQualityValue(_FactorRaceStrategy):
    """r2 主板·质量价值: 高ROE + 低PE/高EP + 低波 + 小盘倾斜(沪深300内小市值)。
    沪深300池 + 小盘倾斜 → 实验 +9.3%/Calmar0.58(风险调整最优)。"""
    factor_weights = {"roe": 0.30, "ep": 0.20, "bm": -0.15, "vol": -0.20, "size": -0.15}
    pool_index = "sh000300"
    cap_segment = None
    rebalance = "monthly"
    ind_neutral = True
    cap_neutral_after = False
    stop_base = 0.10


# ======================================================================
# r3 深度价值反转(Alpha: 价值, 行业中性化, 保留中盘规模暴露)
# ======================================================================
class RaceValueRev(_FactorRaceStrategy):
    """r3 主板·深度价值反转: PB价值 + 1月反转 + 低波, 中盘段。"""
    factor_weights = {"bm": -0.45, "rev1m": -0.30, "ep": 0.15, "vol": -0.10}
    pool_index = "mainboard"
    cap_segment = "mid"
    rebalance = "monthly"
    ind_neutral = True
    cap_neutral_after = False      # 保留中盘规模暴露(诊断: 中盘价值+反转有效)
    stop_base = 0.12


# ======================================================================
# r4 全A盈利加速(Alpha: 成长, 用 profit_q 单季净利)
# ======================================================================
class RaceAllAGrowth(_FactorRaceStrategy):
    """r4 全A·价值低波防御: PB价值 + 低波 + 反转, 全A池(含科创/创业/北交)。
    (实验: 成长/SUE因子在A股近4年弱, 改价值+低波防御; 100亿门槛缩池提性能)"""
    factor_weights = {"bm": -0.35, "vol": -0.30, "rev1m": -0.20, "ep": 0.15}
    pool_index = "all_a"
    cap_segment = None
    rebalance = "monthly"
    ind_neutral = True
    cap_neutral_after = False
    stop_base = 0.15
    liquidity_floor = 30_000_000


# ======================================================================
# r5 ETF动量轮动(Alpha: ETF动量+低波, 与个股独立)
# ======================================================================
class RaceEtfMomentum(_FactorRaceStrategy):
    """r5 ETF·全球防御配置: 沪深300+红利低波+纳指+国债, 低波主导防御。
    (实验证实: ETF动量轮动在A股2022-2026亏损, 宽基+红利+纳指+国债防御配置才稳)"""
    factor_weights = {"vol": -0.60, "rev1m": 0.40}   # 低波主导
    pool_index = "etf_pool"
    cap_segment = None
    rebalance = "monthly"
    stop_base = 0.08
    etf_codes = None

    def generate_orders(self, date, ctx, account):
        date_s = str(date)[:10]
        params = self.params
        if not self.should_reb(date, ctx):
            return self._daily_stops(date, ctx, account)
        # ETF池: registry 指定或默认15只
        codes = params.get("etf_codes") or self.etf_codes
        if not codes:
            return []
        eff = common.effective_hold_n(int(params.get("hold_n", 3)), account.init_capital,
                                      self.config, self.strategy_id)
        # ETF动量排名(读 params factor_weights, 默认 动量+低波)
        weights = params.get("factor_weights", {"rev1m": 0.60, "vol": -0.40})
        scores = _rank_pool(ctx, date_s, codes, weights, ind_neutral=False)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        self._last_scores = dict(ranked)
        target = [c for c, _ in ranked[:eff]]
        return self._rebalance_to_target(date, ctx, account, target, eff)


# ======================================================================
# r6 主板低波防御(Alpha: 低波+质量+大盘择时)
# ======================================================================
class RaceLowVolDef(_FactorRaceStrategy):
    """r6 主板·低波防御: 低波 + 高ROE + 高股息, 大盘择时闸门(MA60)。
    防御型: 低回撤, 弱市空仓。行业中性化(真Alpha), 保留大盘规模暴露。"""
    factor_weights = {"vol": -0.40, "roe": 0.25, "bm": -0.20, "ep": 0.15}
    pool_index = "mainboard"
    cap_segment = "large"
    rebalance = "weekly"
    ind_neutral = True
    cap_neutral_after = False
    stop_base = 0.08
    liquidity_floor = 80_000_000

    def _market_ok(self, ctx, date_s):
        """大盘择时闸门: 沪深300ETF 收盘 > MA60 才开仓。"""
        try:
            closes = [r[0] * (r[1] or 1.0) for r in ctx.conn.execute(
                "SELECT close, adj_factor FROM daily_bar WHERE code='sh510300' AND trade_date<=? "
                "ORDER BY trade_date DESC LIMIT 61", (date_s,)).fetchall()][::-1]
            if len(closes) < 60:
                return True
            ma60 = sum(closes) / len(closes)
            return closes[-1] > ma60
        except Exception:
            return True

    def generate_orders(self, date, ctx, account):
        date_s = str(date)[:10]
        params = self.params
        if not self.should_reb(date, ctx):
            return self._daily_stops(date, ctx, account)
        if params.get("market_gate", True) and not self._market_ok(ctx, date_s):
            log.info("%s %s 大盘<MA60, 择时空仓", self.strategy_id, date_s)
            return self._liquidate_all(date, ctx, account)
        return super().generate_orders(date, ctx, account)
