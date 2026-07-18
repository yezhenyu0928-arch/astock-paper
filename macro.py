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

数据来源: akshare macro_china_supply_of_money(M2同比) + 国债ETF sh511010(利率方向,库内即有)
+ fundamental PE分位 + stock_industry + daily_bar。(原 baostock 从海外 Actions 会 login 挂死,已弃用)
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


_M2_DF_CACHE = []   # 进程级单元素缓存哨兵:[]=未拉取, [df_or_None]=已拉取


def _cached_m2_df():
    """M2 全历史(akshare)进程级缓存。原实现每次 macro_factor 调用都联网拉全历史 M2,
    回测数百个调仓日重复联网是回测缓慢的主因(本机/CI 皆然);缓存后同一进程仅联网一次。
    同一进程内 M2 视为不变(回测期统一用最新月,与原 s.iloc[0] 行为一致)。"""
    if not _M2_DF_CACHE:
        try:
            import akshare as ak
            _M2_DF_CACHE.append(ak.macro_china_supply_of_money())
        except Exception as e:
            log.debug("M2(akshare) 获取失败: %s", e)
            _M2_DF_CACHE.append(None)
    return _M2_DF_CACHE[0]


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
        # ── 1. M2 同比增速(akshare 替换 baostock:baostock.login 从海外 Actions 会挂死) ──
        try:
            m2df = _cached_m2_df()               # 进程级缓存,回测不再每调仓日联网(见 _cached_m2_df)
            col = "货币和准货币（广义货币M2）同比增长"
            if m2df is not None and col in m2df.columns:
                s = pd.to_numeric(m2df[col], errors="coerce").dropna()
                if len(s):
                    result["m2_yoy"] = float(s.iloc[0])   # 行首为最新月
        except Exception as e:
            log.debug("M2(akshare) 获取失败: %s", e)

        # ── 2. 利率方向(国债ETF sh511010,库内即有,替换 baostock 国债指数,避免挂死) ──
        # 国债价格涨 ≈ 收益率降 ≈ 宽松;MA20>MA60 → easing。sh511010 是 s2/s6 避险资产,必在库。
        try:
            rows = conn.execute(
                "SELECT close FROM daily_bar WHERE code='sh511010' AND trade_date<=? "
                "ORDER BY trade_date DESC LIMIT 60", (util.to_date_str(date),)).fetchall()
            if len(rows) >= 60:
                closes = [float(r[0]) for r in rows][::-1]   # 转升序
                result["bond_60d_ret"] = (closes[-1] / closes[0] - 1) * 100
                ma20 = float(np.mean(closes[-20:]))
                ma60 = float(np.mean(closes[-60:]))
                result["rate_direction"] = "easing" if ma20 > ma60 else "tightening"
                result["bond_ma20"] = round(ma20, 4)
                result["bond_ma60"] = round(ma60, 4)
        except Exception as e:
            log.debug("国债ETF 利率方向失败: %s", e)
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


# ============ 市场 regime(移植自 K线机 lib/marketRegime.ts) ============
# 多基准价格趋势 + MA20/50/200 + 市场广度 + 风险比 → regime(强势/震荡/转弱/风险) + 0-100 分。
# 纯价格、免费、可回测,比原 L0 词表市场分更全面。接入:看板"市场信号"卡 + s7 赛道旗舰评分。
_REGIME_BENCHES = [("sh510300", "沪深300"), ("sh510500", "中证500"),
                   ("sh512480", "半导体"), ("sh512000", "券商")]

# 行业ETF池(同 S6/S7):库内即有,个股行业动量缺数据时用它兜底算"利好板块"
_SECTOR_ETF_NAMES = {
    "sh512000": "券商", "sh512480": "半导体", "sh512010": "医药", "sz159928": "消费",
    "sh512660": "军工", "sh516160": "新能源", "sh512690": "酒", "sh515790": "光伏",
    "sh512800": "银行",
}


def _bench_closes(conn, code, date, n):
    """某代码截至 date 的最近 n 个收盘(升序)。ETF adj_factor=1,直接用 close。"""
    rows = conn.execute(
        "SELECT close FROM daily_bar WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT ?",
        (code, util.to_date_str(date), int(n))).fetchall()
    if not rows:
        return None
    return [float(r[0]) for r in rows][::-1]


def _ret(closes, days):
    """closes[-1]/closes[-1-days]-1(百分数);不足返回 None。"""
    if closes is None or len(closes) < days + 1 or not closes[-1 - days]:
        return None
    return round((closes[-1] / closes[-1 - days] - 1) * 100, 2)


def _market_breadth(conn, date, pool=None):
    """沪深300成分中 收盘 > MA20 且 > MA50 的占比(广度%);及 收盘 < MA200 的占比(风险比)。
    数据不足返回 (None, None)。依赖库内个股日线(re-backfill 后齐备)。"""
    try:
        import factors
        if pool is None:
            pool = [r[0] for r in conn.execute(
                "SELECT code FROM index_members WHERE index_code='sh000300'").fetchall()]
        if not pool:
            return None, None
        prices, _ = factors._pool_bars(conn, pool, date, lookback=210)
        if prices is None or prices.shape[1] < 20:
            return None, None
        above, total, below200, total200 = 0, 0, 0, 0
        for code in prices.columns:
            s = prices[code].dropna()
            if len(s) < 50:
                continue
            last = float(s.iloc[-1])
            ma20 = float(s.iloc[-20:].mean())
            ma50 = float(s.iloc[-50:].mean())
            total += 1
            if last > ma20 and last > ma50:
                above += 1
            if len(s) >= 200:
                total200 += 1
                if last < float(s.iloc[-200:].mean()):
                    below200 += 1
        breadth = round(above / total * 100) if total else None
        risk_ratio = round(below200 / total200, 3) if total200 else None
        return breadth, risk_ratio
    except Exception as e:
        log.debug("市场广度计算失败: %s", e)
        return None, None


def _classify_regime(ret1m, ret3m, above20, above50, above200, breadth, risk_ratio):
    """K线机 classify() 同款打分(0-100)+ 分档。ret 为百分数。"""
    score = 50.0
    score += (ret1m or 0) * 1.6
    score += (ret3m or 0) * 0.55
    if above20:
        score += 8
    if above50:
        score += 10
    if above200:
        score += 8
    if breadth is not None:
        score += (breadth - 50) * 0.35
    if risk_ratio is not None:
        score -= risk_ratio * 45
    score = int(max(0, min(100, round(score))))
    weak = (above50 is False) or ((ret1m or 0) < -4) or ((risk_ratio or 0) >= 0.32)
    if score >= 68 and above50 is not False and (breadth or 0) >= 50:
        return score, "强势"
    if weak and score < 45:
        return score, "风险"
    if weak or score < 52:
        return score, "转弱"
    return score, "震荡"


def compute_market_regime(date, conn=None):
    """综合市场 regime。返回 dict{regime, score, ret_1w/1m/3m, aboveMa20/50/200,
    breadth, risk_ratio, benchmarks:[{code,name,ret_1m,aboveMa50}], summary}。数据不足则 regime='数据不足'。"""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        primary, pname = _REGIME_BENCHES[0]
        closes = _bench_closes(conn, primary, date, 260)
        if closes is None or len(closes) < 25:
            return {"regime": "数据不足", "score": 50, "summary": "基准数据不足,无法判定 regime",
                    "benchmarks": [], "breadth": None}
        last = closes[-1]
        ma20 = float(np.mean(closes[-20:]))
        ma50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else None
        ma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else None
        above20 = last > ma20
        above50 = (last > ma50) if ma50 is not None else None
        above200 = (last > ma200) if ma200 is not None else None
        ret1w, ret1m, ret3m = _ret(closes, 5), _ret(closes, 21), _ret(closes, 63)
        breadth, risk_ratio = _market_breadth(conn, date)
        score, regime = _classify_regime(ret1m, ret3m, above20, above50, above200, breadth, risk_ratio)

        benchmarks = []
        for code, name in _REGIME_BENCHES:
            c = _bench_closes(conn, code, date, 60)
            if c and len(c) >= 21:
                m50 = float(np.mean(c[-50:])) if len(c) >= 50 else None
                benchmarks.append({"code": code, "name": name, "ret_1m": _ret(c, 21),
                                   "aboveMa50": (c[-1] > m50) if m50 is not None else None})

        r1 = "1月收益数据不足" if ret1m is None else f"近1月{pname}{ret1m:+.1f}%"
        b1 = "广度数据不足" if breadth is None else f"强势广度{breadth}%"
        summary = f"市场状态：{regime}(分{score})，{r1}，{b1}。"
        return {"regime": regime, "score": score, "ret_1w": ret1w, "ret_1m": ret1m, "ret_3m": ret3m,
                "aboveMa20": above20, "aboveMa50": above50, "aboveMa200": above200,
                "breadth": breadth, "risk_ratio": risk_ratio, "benchmarks": benchmarks, "summary": summary}
    except Exception as e:
        log.warning("compute_market_regime 失败: %s", e)
        return {"regime": "数据不足", "score": 50, "summary": f"regime 计算异常: {e}",
                "benchmarks": [], "breadth": None}
    finally:
        if own:
            conn.close()


def top_bullish_sectors(date, conn=None, top=6):
    """近期利好行业板块排行:行业动量(价格,库内) 合并 GLM 产业信号(news_signal)。
    返回 [{name, momentum_pct, news_score}],按综合强度降序。供看板"利好板块"展示。"""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        ind_mom = industry_momentum(date, lookback=60, conn=conn)   # {行业名: 60日涨幅}
        # GLM 行业ETF信号(sector:<etf>);映射到行业名展示
        sector_news = {}
        try:
            rows = conn.execute(
                "SELECT scope, score FROM news_signal WHERE signal_date<=? AND scope LIKE 'sector:%' "
                "ORDER BY signal_date DESC LIMIT 40", (util.to_date_str(date),)).fetchall()
            for scope, score in rows:
                sector_news.setdefault(scope.replace("sector:", ""), float(score))
        except Exception:
            pass
        items = []
        if ind_mom:
            for name, mom in ind_mom.items():
                items.append({"name": name, "momentum_pct": round(mom * 100, 1),
                              "news_score": 0.0})
        else:
            # 回退:个股行业动量无数据(库内缺沪深300成分)→ 用行业ETF池60日动量(库内即有)
            for code, name in _SECTOR_ETF_NAMES.items():
                r = _ret(_bench_closes(conn, code, date, 61), 60)
                if r is not None:
                    items.append({"name": name, "momentum_pct": r,
                                  "news_score": sector_news.get(code, 0.0)})
        items.sort(key=lambda x: x["momentum_pct"], reverse=True)
        return items[:top], sector_news
    except Exception as e:
        log.debug("top_bullish_sectors 失败: %s", e)
        return [], {}
    finally:
        if own:
            conn.close()


# ============ 宏观择时 7 指标(手册宏观择时章节补全) ============
# 7 指标: 沪深300趋势 / 估值分位 / 国债收益率(利率方向) / PMI / 社融 / 北向资金 / 情绪(融资余额变化,缺则广度兜底)
# 数据来源: 前3项来自库内价格/PE/国债ETF(原 macro_score 三因子); 后4项来自 macro_data.macro_indicator 表
# (akshare 拉取入库,详见 macro_data.py)。任一指标缺数据→该指标权重归零(优雅降级),不报错、不中断回测。
# 返回 -1(最不利)~+1(最有利) 综合分 + 分档 regime。

def macro_score_7(date, conn=None, cfg=None):
    """宏观择时 7 指标综合评分(-1~+1)。缺失指标权重归零(优雅降级)。"""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        import macro_data as md
        date = util.to_date_str(date)
        subs = []  # (weight, subscore)

        # 复用一次 regime 计算(趋势/广度/风险比都来自它)
        rg = compute_market_regime(date, conn=conn)
        ret3m = rg.get("ret_3m") or 0
        above50 = rg.get("aboveMa50")

        # 1. 沪深300趋势
        try:
            s_trend = (ret3m or 0) / 10.0
            if above50 is True:
                s_trend += 0.3
            elif above50 is False:
                s_trend -= 0.3
            subs.append((1.0, max(-1.0, min(1.0, s_trend))))
        except Exception:
            pass

        # 2. 估值分位(沪深300 PE 十年分位;越低越利于股票)
        try:
            import fundamental as F
            pct = F.index_pe_percentile("sh000300", date, conn=conn)
            if pct is not None:
                subs.append((1.0, max(-1.0, min(1.0, (0.5 - pct) * 2))))
        except Exception:
            pass

        # 3. 利率方向(国债ETF:宽松=利好)
        try:
            mf = macro_factor(date, conn=conn)
            s_rate = 0.0
            if mf.get("rate_direction") == "easing":
                s_rate += 0.5
            elif mf.get("rate_direction") == "tightening":
                s_rate -= 0.5
            s_rate += max(-0.5, min(0.5, (mf.get("bond_60d_ret", 0) or 0) / 10.0))
            subs.append((1.0, max(-1.0, min(1.0, s_rate))))
        except Exception:
            pass

        # 4. PMI(制造业,~50中性)
        try:
            pmi = md.value_on(conn, "PMI", date)
            if pmi is not None:
                subs.append((1.0, max(-1.0, min(1.0, (pmi - 50.0) / 2.0))))
        except Exception:
            pass

        # 5. 社融(存量同比 %)
        try:
            tsf = md.value_on(conn, "TSF_YOY", date)
            if tsf is not None:
                subs.append((1.0, max(-1.0, min(1.0, tsf / 20.0))))
        except Exception:
            pass

        # 6. 北向资金(近20日累计净买额,亿元)
        try:
            nb = md.window_sum(conn, "NORTHBOUND_NET", date, 20)
            if nb is not None:
                subs.append((1.0, max(-1.0, min(1.0, nb / 300.0))))
        except Exception:
            pass

        # 7. 情绪: 融资余额20日变化(亿元,风险偏好代理);缺则用广度/风险比兜底
        try:
            mg = md.delta(conn, "MARGIN_BALANCE", date, 20)
            if mg is not None:
                subs.append((1.0, max(-1.0, min(1.0, mg / 1500.0))))
            else:
                s2 = 0.0
                rr = rg.get("risk_ratio")
                br = rg.get("breadth")
                if rr is not None:
                    s2 -= (rr - 0.20) * 2
                if br is not None:
                    s2 += (br - 50) / 50.0
                subs.append((1.0, max(-1.0, min(1.0, s2))))
        except Exception:
            pass

        if not subs:
            return 0.0, "数据不足(无可用宏观指标)"
        wsum = sum(w for w, _ in subs)
        score = sum(w * s for w, s in subs) / wsum if wsum else 0.0
        score = max(-1.0, min(1.0, score))
        if score >= 0.5:
            regime = "强势"
        elif score <= -0.5:
            regime = "风险"
        elif score < -0.15:
            regime = "转弱"
        else:
            regime = "震荡"
        return score, regime
    except Exception as e:
        log.warning("macro_score_7 失败: %s", e)
        return 0.0, "异常"
    finally:
        if own:
            conn.close()


def macro_exposure_mult(date, ctx, cfg=None):
    """由 macro_score_7 映射总仓位敞口系数(手册:总仓位0-90%,现金≥10%)。

    分档(中性=0.70,对应约30%现金,与手册"中性偏防御"一致):
      score >= 0 : mult = 0.70 + score*0.20  → [0.70, 0.90]
      score <  0 : mult = 0.70 + score*0.60  → [0.10, 0.70)
    仅降不升(取 min 与消息面系数叠加)。无数据/异常 → 1.0(不干预)。"""
    try:
        # 无数据连接 → 不做宏观干预(回测/实盘 ctx 必带 conn;单测 MockCtx 无 conn 即视为无数据)
        conn = getattr(ctx, "conn", None)
        if conn is None:
            return 1.0
        score, _ = macro_score_7(date, conn=conn, cfg=cfg)
    except Exception:
        return 1.0
    mult = (0.70 + score * 0.20) if score >= 0 else (0.70 + score * 0.60)
    return float(max(0.10, min(0.90, mult)))


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
