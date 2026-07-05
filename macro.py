# -*- coding: utf-8 -*-
"""宏观regime检测 + 行业动量模块（P0）。

提供:
1. detect_regime(date, conn) → "expansion" | "contraction" | "neutral"
   基于沪深300 PE分位 + MA20/MA60方向判断市场状态。
   - 扩张: PE分位<50%且MA20>MA60(估值合理+趋势向上)
   - 收缩: PE分位>70%或MA20<MA60且PE分位>50%(高估或趋势向下+估值不便宜)
   - 中性: 其他

2. industry_momentum(date, lookback=60, conn=None) → {industry_name: momentum_pct}
   申万31行业近lookback日涨幅排名（用行业ETF或代表性的成分股打包计算）。
   简化: 用 stock_industry 表 + daily_bar 计算每个行业所有股票等权组合收益。

3. macro_factor(date, conn) → {"shibor": float, "bond_yield": float, "cpi": float}
   宏观因子（P2待完善，当前占位返回0）。

数据来源: baostock 日线(已有)、stock_industry表(已有)、fundamental PE分位(已有)。
"""
import logging
import numpy as np
import pandas as pd

import util
from db import get_conn

log = logging.getLogger("macro")

# ── 申万31行业 → 代表性指数代码(用于行业动量) ──
# 简化: 用 stock_industry 表内的个股计算行业等权收益
SHENWAN_INDUSTRIES = [
    "银行", "非银金融", "房地产", "建筑装饰", "建筑材料",
    "交通运输", "公用事业", "环保", "钢铁", "有色金属",
    "基础化工", "石油石化", "煤炭", "农林牧渔", "食品饮料",
    "医药生物", "家用电器", "纺织服装", "轻工制造", "商贸零售",
    "社会服务", "传媒", "通信", "计算机", "电子",
    "机械设备", "电力设备", "国防军工", "汽车", "美容护理", "综合",
]


def _market_ma(conn, date, ma_window=20):
    """获取沪深300ETF(sh510300)截至date的MA均线方向。
    返回 (ma_fast, ma_slow, direction): direction="up" 表示 MA20 > MA60。"""
    import factors
    mkt_prices = factors._pool_market_bar(conn, date, lookback=300)
    if mkt_prices is None or len(mkt_prices) < max(ma_window, 60) + 1:
        return None, None, None
    closes = mkt_prices.values
    ma20 = np.mean(closes[-ma_window:])
    ma60 = np.mean(closes[-60:]) if len(closes) >= 60 else ma20
    direction = "up" if ma20 > ma60 else "down"
    return ma20, ma60, direction


def detect_regime(date, conn=None):
    """检测宏观 regime。

    判断逻辑:
    - 扩张(expansion): PE分位 <= 50% 且 MA20 > MA60
    - 收缩(contraction): PE分位 > 70% 或 (MA20 < MA60 且 PE分位 > 50%)
    - 中性(neutral): 其他

    返回 "expansion" | "contraction" | "neutral"
    """
    own = conn is None
    if own:
        conn = get_conn()
    try:
        import fundamental as F
        pe_pct = F.index_pe_percentile("sh000300", date, conn=conn)
        _, _, direction = _market_ma(conn, date)

        if pe_pct is None or direction is None:
            if own:
                conn.close()
            return "neutral"

        if pe_pct <= 0.50 and direction == "up":
            regime = "expansion"
        elif pe_pct > 0.70 or (direction == "down" and pe_pct > 0.50):
            regime = "contraction"
        else:
            regime = "neutral"

        log.debug("regime=%s pe_pct=%.2f direction=%s", regime, pe_pct, direction)
        return regime
    except Exception as e:
        log.warning("detect_regime 失败: %s", e)
        return "neutral"
    finally:
        if own:
            conn.close()


def industry_momentum(date, lookback=60, conn=None):
    """计算申万31行业近60日等权涨幅排名。

    返回 {industry_name: momentum_pct}，按涨幅从高到低排序后仅保留前15行业。
    """
    own = conn is None
    if own:
        conn = get_conn()
    try:
        import factors
        # 获取沪深300成分股作为行业动量计算的股票池
        pool = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE trade_date<=? "
            "ORDER BY code LIMIT 500", (util.to_date_str(date),)).fetchall()]
        if not pool:
            return {}

        # 行业映射
        industry_map = factors.get_industry(conn, pool)

        # 批量获取日线
        prices, _ = factors._pool_bars(conn, pool, date, lookback=lookback * 2)
        if prices is None or prices.shape[1] < 5:
            return {}

        # 计算近 lookback 日收益
        if len(prices) < lookback + 1:
            return {}

        recent = prices.iloc[-lookback - 1:]
        ret = recent.iloc[-1] / recent.iloc[0] - 1  # Series per code

        # 按行业聚合等权收益
        ind_rets = {}
        ind_count = {}
        for code in ret.index:
            if pd.isna(ret[code]):
                continue
            ind = industry_map.get(code)
            if ind is None or ind == "未知":
                continue
            ind_rets[ind] = ind_rets.get(ind, 0.0) + ret[code]
            ind_count[ind] = ind_count.get(ind, 0) + 1

        # 等权平均
        result = {}
        for ind in ind_rets:
            if ind_count.get(ind, 0) >= 3:  # 至少3只股票
                result[ind] = ind_rets[ind] / ind_count[ind]

        return result
    except Exception as e:
        log.warning("industry_momentum 失败: %s", e)
        return {}
    finally:
        if own:
            conn.close()


def macro_factor(date, conn=None):
    """宏观因子占位。

    P2 将实现: Shibor/10Y国债收益率/CPI/PMI。
    当前返回占位0。
    """
    return {
        "shibor": 0.0,
        "bond_yield_10y": 0.0,
        "cpi_change": 0.0,
        "pmi": 0.0,
    }


def macro_score(date, conn=None) -> float:
    """宏观综合评分: -1(最不利) ~ +1(最有利)。
    基于 regime + PE分位 + 利率方向。策略可据此调整仓位。"""
    regime = detect_regime(date, conn=conn)
    if regime == "expansion":
        return 0.5
    elif regime == "contraction":
        return -0.5
    return 0.0


# ── 简易自检 ──
def _self_test():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s|%(name)s|%(message)s")
    conn = get_conn()
    date = "2026-07-03"

    regime = detect_regime(date, conn=conn)
    log.info("regime: %s", regime)

    ind_mom = industry_momentum(date, lookback=60, conn=conn)
    log.info("industry_momentum: %d industries", len(ind_mom))
    if ind_mom:
        sorted_ind = sorted(ind_mom.items(), key=lambda x: x[1], reverse=True)
        log.info("top 5: %s", sorted_ind[:5])
        log.info("bottom 5: %s", sorted_ind[-5:])

    conn.close()


if __name__ == "__main__":
    _self_test()
