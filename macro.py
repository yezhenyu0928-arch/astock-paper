# -*- coding: utf-8 -*-
"""宏观regime检测 + 行业动量 + 真实宏观因子模块（P0+P2）。

提供:
1. detect_regime(date, conn) → "expansion" | "contraction" | "neutral"
   基于沪深300 PE分位 + MA20/MA60方向判断市场状态。

2. industry_momentum(date, lookback=60, conn=None) → {industry_name: momentum_pct}
   申万31行业近lookback日涨幅排名。

3. macro_factor(date, conn) → {"m2_yoy": float, "bond_60d_ret": float, "rate_direction": str}
   真实宏观因子: M2同比增速(baostock货币供应表) + 国债指数收益(利率方向代理)。

4. macro_score(date, conn) → float
   综合评分(-1~+1)，策略可用此调整仓位或因子权重。

数据来源: baostock query_money_supply_data_month + query_history_k_data_plus(国债指数sh.000012)
+ fundamental PE分位 + stock_industry + daily_bar。
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
    """获取真实宏观因子数据。

    P2 实现:
    - M2 同比增速(%)：从 baostock query_money_supply_data_month 获取
    - 国债收益率代理(%)：从国债指数 sh.000012 的 60 日收益率反推（指数涨=收益率降）
    - 利率方向：基于国债指数 MA20 vs MA60 判断（利率下行=宽松）

    返回 {"m2_yoy": float, "bond_60d_ret": float, "rate_direction": str}
    数据不可用时各类返回 0.0 / "unknown"。
    """
    import factors
    result = {
        "m2_yoy": 0.0,
        "bond_60d_ret": 0.0,
        "rate_direction": "unknown",
    }
    own = conn is None
    if own:
        conn = get_conn()
    try:
        # ── 1. M2 同比增速 ──
        try:
            import baostock as bs
            lg = bs.login()
            if lg.error_code == "0":
                date_str = util.to_date_str(date)
                # 取最近6个月的 M2 数据
                year = int(date_str[:4])
                month = int(date_str[5:7])
                start_ym = f"{year - 1}-{month:02d}"
                end_ym = f"{year}-{month:02d}"
                rs = bs.query_money_supply_data_month(start_date=start_ym, end_date=end_ym)
                if rs.error_code == "0":
                    m2_rows = []
                    while rs.next():
                        r = rs.get_row_data()
                        if r[9]:  # m2YOY
                            m2_rows.append((r[0] + "-" + r[1], float(r[9])))
                    if m2_rows:
                        result["m2_yoy"] = m2_rows[-1][1]  # 最新 M2 同比
                bs.logout()
        except Exception as e:
            log.debug("M2 数据获取失败: %s", e)
            try:
                bs.logout()
            except Exception:
                pass

        # ── 2. 国债指数收益(收益率代理) ──
        # 国债指数 sh.000012 涨 ≈ 国债收益率降 ≈ 宽松
        try:
            import baostock as bs
            lg = bs.login()
            if lg.error_code == "0":
                rs = bs.query_history_k_data_plus(
                    "sh.000012", "date,close",
                    start_date="2024-01-01",
                    end_date=util.to_date_str(date),
                    frequency="d")
                if rs.error_code == "0":
                    bond_rows = []
                    while rs.next():
                        r = rs.get_row_data()
                        if r[1]:
                            bond_rows.append((r[0], float(r[1])))
                    if len(bond_rows) >= 60:
                        # 60 日收益
                        result["bond_60d_ret"] = (bond_rows[-1][1] / bond_rows[-60][1] - 1) * 100
                        # MA20 vs MA60 方向
                        closes = [r[1] for r in bond_rows]
                        ma20 = np.mean(closes[-20:])
                        ma60 = np.mean(closes[-60:])
                        result["rate_direction"] = "easing" if ma20 > ma60 else "tightening"
                        result["bond_ma20"] = round(ma20, 4)
                        result["bond_ma60"] = round(ma60, 4)
                bs.logout()
        except Exception as e:
            log.debug("国债指数数据获取失败: %s", e)
            try:
                bs.logout()
            except Exception:
                pass
    except Exception as e:
        log.warning("macro_factor 失败: %s", e)
    finally:
        if own:
            conn.close()

    return result


def macro_score(date, conn=None) -> float:
    """宏观综合评分: -1(最不利) ~ +1(最有利)。
    基于 regime + M2增速 + 利率方向。
    - expansion + M2高增长 + 利率下行 = 高分(利于股票)
    - contraction + M2低增长 + 利率上行 = 低分(防御)
    """
    regime = detect_regime(date, conn=conn)
    mf = macro_factor(date, conn=conn)

    score = 0.0
    # regime 基础分
    if regime == "expansion":
        score += 0.6
    elif regime == "contraction":
        score -= 0.6
    # M2 增速调整：>10% 宽松 +0.2, <8% 偏紧 -0.2
    m2 = mf.get("m2_yoy", 0) or 0
    if m2 > 10:
        score += 0.2
    elif m2 < 8 and m2 > 0:
        score -= 0.2
    # 利率方向：宽松
    if mf.get("rate_direction") == "easing":
        score += 0.2
    elif mf.get("rate_direction") == "tightening":
        score -= 0.2

    return max(-1.0, min(1.0, score))


# ── 简易自检 ──
def _self_test():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s|%(name)s|%(message)s")
    conn = get_conn()
    date = "2026-07-03"

    regime = detect_regime(date, conn=conn)
    log.info("regime: %s", regime)

    mf = macro_factor(date, conn=conn)
    log.info("macro_factor: M2 YOY=%.1f%%  bond_60d=%.2f%%  rate=%s",
             mf.get("m2_yoy", 0) or 0, mf.get("bond_60d_ret", 0) or 0, mf.get("rate_direction", "unknown"))

    ms = macro_score(date, conn=conn)
    log.info("macro_score: %+.2f", ms)

    ind_mom = industry_momentum(date, lookback=60, conn=conn)
    log.info("industry_momentum: %d industries", len(ind_mom))
    if ind_mom:
        sorted_ind = sorted(ind_mom.items(), key=lambda x: x[1], reverse=True)
        log.info("top 5: %s", sorted_ind[:5])
        log.info("bottom 5: %s", sorted_ind[-5:])

    conn.close()


if __name__ == "__main__":
    _self_test()
