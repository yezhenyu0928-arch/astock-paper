# -*- coding: utf-8 -*-
"""数据适配层 —— 全项目唯一允许 import akshare / baostock 的文件(SPEC 模块1)。
职责:把各源的原始字段翻译成统一 schema,写库。上层只认统一列名,不碰数据源差异。

本机现实(见探针):
- 数据源均为国内主机,本不该走代理;本机代理会间歇性掐断东财接口。
  → 抓取时临时摘掉 HTTP(S)_PROXY 直连(_no_proxy),Actions 无代理时是 no-op。
- akshare 东财接口间歇失败 → 重试 + baostock 兜底(baostock 走裸 socket,稳定)。
- akshare 成交量单位=手,baostock=股;统一存"股"(akshare ×100)。

数据健康度优化(新增):
- 集成 data_health 模块,自动监控各数据源成功率/响应时间
- 支持配置化数据源优先级(config.yaml)
- 自动数据源故障切换和健康度评分
- 数据质量校验(价格异常、缺失检测)
"""
import os
import time
import logging
import contextlib
import pandas as pd
import requests

import util
import conf
from db import get_conn, init_db
from data_health import (
    get_monitor, monitored_call, DataQualityChecker,
    check_data_source_health, HEALTHY_THRESHOLD, DEGRADED_THRESHOLD
)

log = logging.getLogger("data_adapter")

DEFAULT_START = "2018-01-01"
_VOL_LOTS_TO_SHARES = 100      # akshare 成交量:手→股

# 已知 T+0 的 ETF(黄金/QDII跨境/债券),其余 ETF 与个股为 T+1
_T0_PREFIX = ("511", "513", "518", "159012")  # 债券/跨境/黄金;个别跨境159也T+0,按需扩充
_ETF_UNIVERSE_NAME = {
    "sh510300": "沪深300ETF", "sh510500": "中证500ETF", "sh512890": "红利低波ETF",
    "sh518880": "黄金ETF", "sh513100": "纳指ETF", "sh511010": "国债ETF",
    # S6 行业ETF池(卡C)
    "sh512000": "券商ETF", "sh512480": "半导体ETF", "sh512010": "医药ETF",
    "sz159928": "消费ETF", "sh512660": "军工ETF", "sh516160": "新能源ETF",
    "sh512690": "酒ETF", "sh515790": "光伏ETF", "sh512800": "银行ETF",
}

_bs_logged_in = False

# 全局短路标志:一旦确认所有国内数据源均不可达(海外Runner),后续所有股票跳过取数。
_GLOBAL_ALL_DISABLED = False


# ============ 重试 ============
# 说明:本机实测"摘掉代理直连"反而让 akshare 东财接口(push2his)全部失败,
# 而保留环境代理时时好时坏 → 不动代理,用 ambient 环境 + 少量重试 + baostock 兜底。
def _retry(fn, tries=2, delay=0.6, what=""):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa
            last = e
            log.warning("取数重试 %s (%d/%d): %s", what, i + 1, tries, e)
            if i + 1 < tries:
                time.sleep(delay * (i + 1))
    raise last


def _retry_df(fn, tries=3, delay=0.5, what=""):
    """重试直到拿到非空 DataFrame(baostock 偶发返回空,不抛异常,故需对"空"也重试)。
    否则 fetch_daily 会因一次空响应就跌到被限流的东财,导致整只股票取数失败。"""
    for i in range(tries):
        try:
            df = fn()
            if df is not None and not getattr(df, "empty", True):
                return df
        except Exception as e:  # noqa
            log.warning("取数重试(空/异常) %s (%d/%d): %s", what, i + 1, tries, e)
        if i + 1 < tries:
            time.sleep(delay * (i + 1))
    try:
        return fn()
    except Exception:
        return None


def _bs():
    global _bs_logged_in, _GLOBAL_ALL_DISABLED
    if _GLOBAL_ALL_DISABLED:
        raise ConnectionError("全局短路:国内数据源不可达")
    import baostock as bs
    if not _bs_logged_in:
        bs.login()
        _bs_logged_in = True
    return bs


def _bs_code(code: str) -> str:
    """sh510300 -> sh.510300"""
    m, six = util.market(code), util.bare(code)
    return f"{m}.{six}"


def is_etf_code(code: str) -> bool:
    six = util.bare(code)
    return six[0] == "5" or six[:2] in ("15", "16", "18")


def _is_t0(code: str) -> int:
    six = util.bare(code)
    return int(any(six.startswith(p) for p in _T0_PREFIX))


# ============ 日线 ============
def _ak_etf(code, start, end, adjust):
    df = ak_fund_etf(code, start, end, adjust)
    return df


def ak_fund_etf(code, start, end, adjust):
    import akshare as ak
    six = util.bare(code)
    df = ak.fund_etf_hist_em(symbol=six, period="daily",
                             start_date=start.replace("-", ""),
                             end_date=end.replace("-", ""), adjust=adjust)
    return df


def ak_stock(code, start, end, adjust):
    import akshare as ak
    six = util.bare(code)
    df = ak.stock_zh_a_hist(symbol=six, period="daily",
                            start_date=start.replace("-", ""),
                            end_date=end.replace("-", ""), adjust=adjust)
    return df


def _sina_etf_raw(code, start, end):
    """ETF 全历史(Sina 口径,本机最稳)。列 date/open/high/low/close/volume/amount;
    ⚠ Sina 的 volume 已是"股"(与东财"手"不同),不再 ×100。"""
    import akshare as ak
    df = _retry(lambda: ak.fund_etf_hist_sina(symbol=code), tries=3, what=f"sina_etf {code}")
    if df is None or df.empty:
        return None, None
    df = df.rename(columns={"date": "trade_date"})
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[(df["trade_date"] >= start) & (df["trade_date"] <= end)]
    return df[["trade_date", "open", "high", "low", "close", "volume", "amount"]].copy(), None


_AK_COLMAP = {
    "日期": "trade_date", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount",
}


def _norm_ak(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=_AK_COLMAP)
    keep = ["trade_date", "open", "high", "low", "close", "volume", "amount"]
    df = df[[c for c in keep if c in df.columns]].copy()
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * _VOL_LOTS_TO_SHARES
    for c in ("open", "high", "low", "close", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _bs_kline(code, start, end, adjustflag):
    """baostock 日线。adjustflag: 1=后复权 2=前复权 3=不复权。返回统一列(含 tradestatus/isST)。"""
    bs = _bs()
    fields = "date,open,high,low,close,volume,amount,tradestatus,isST"
    rs = bs.query_history_k_data_plus(_bs_code(code), fields,
                                      start_date=start, end_date=end,
                                      frequency="d", adjustflag=str(adjustflag))
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame(columns=["trade_date", "open", "high", "low", "close",
                                     "volume", "amount", "tradestatus", "isST"])
    df = pd.DataFrame(rows, columns=rs.fields)
    df = df.rename(columns={"date": "trade_date"})
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")  # baostock volume 已是股
    return df


def _ak_raw(code, start, end):
    fn = ak_fund_etf if is_etf_code(code) else ak_stock
    df = _retry(lambda: _norm_ak(fn(code, start, end, "")), tries=3, what=f"ak_raw {code}")
    return df, None


def _bs_raw(code, start, end):
    b = _retry_df(lambda: _bs_kline(code, start, end, 3), what=f"bs_raw {code}")
    if b is None or b.empty:
        return None, None
    raw = b[["trade_date", "open", "high", "low", "close", "volume", "amount"]].copy()
    susp = b.set_index("trade_date")["tradestatus"] if "tradestatus" in b else None
    return raw, susp


def _fetch_raw(code, start, end, cfg=None):
    """不复权日线。支持健康度监控和配置化优先级。
    返回 (df, source, susp_series|None)。"""
    global _GLOBAL_ALL_DISABLED
    if _GLOBAL_ALL_DISABLED:
        return None, None, None
    cfg = cfg or conf.load_config()
    etf = is_etf_code(code)

    # 获取配置的数据源优先级
    priority_key = "etf_daily" if etf else "stock_daily"
    source_priority = cfg.get("data_source_priority", {}).get(priority_key, [])

    # 数据源名称到函数的映射
    source_map = {
        "sina_etf": _sina_etf_raw,
        "baostock": _bs_raw,
        "akshare_em": _ak_raw,
    }

    # 获取健康度监控器,按健康度排序
    monitor = get_monitor()
    available_sources = []
    for name in source_priority:
        if name in source_map:
            health = monitor.get_health(name)
            # 跳过被禁用的源
            if health.disabled or time.time() < health.disabled_until:
                log.debug("数据源 %s 被禁用,跳过", name)
                continue
            available_sources.append((name, source_map[name], health.health_score))

    # 按健康度降序排列
    available_sources.sort(key=lambda x: x[2], reverse=True)

    if not available_sources:
        log.warning("所有数据源均被禁用或不可用,尝试使用默认顺序")
        # 回退前再确认所有源是否仍在禁用期——是则直接跳过(海外Runner白费~12秒/股)
        if all(
            monitor.get_health(name).disabled or time.time() < monitor.get_health(name).disabled_until
            for name in source_priority if name in source_map
        ):
            log.warning("所有数据源仍处于禁用期,跳过 %s", code)
            return None, None, None
        if etf:
            available_sources = [("sina_etf", _sina_etf_raw, 1.0),
                                ("baostock", _bs_raw, 1.0),
                                ("akshare_em", _ak_raw, 1.0)]
        else:
            available_sources = [("baostock", _bs_raw, 1.0),
                                ("akshare_em", _ak_raw, 1.0)]

    for name, fn, score in available_sources:
        try:
            start_time = time.time()
            df, susp = fn(code, start, end)
            elapsed = time.time() - start_time

            success = df is not None and not df.empty
            rows = len(df) if success else 0

            # 记录健康度
            monitor.record_call(name, success, elapsed, rows, rows)

            if success:
                log.debug("使用数据源 %s (健康度%.2f) 获取 %s 数据, %d 行, %.2fs",
                         name, score, code, rows, elapsed)
                return df, name, susp

        except Exception as e:
            elapsed = time.time() - start_time if 'start_time' in dir() else 0
            monitor.record_call(name, False, elapsed, 0, 0, str(e))
            log.warning("%s 不复权失败 %s: %s", name, code, e)

    # 所有源全失败 → 全局短路(海外Runner场景)
    _GLOBAL_ALL_DISABLED = True
    log.warning("所有数据源均失败(不复权) %s → 全局短路", code)
    return None, None, None


def _fetch_hfq_close(code, start, end, cfg=None):
    """后复权收盘(仅算 adj_factor 用),集成健康度监控。
    返回 Series(index=trade_date) 或 None。"""
    global _GLOBAL_ALL_DISABLED
    if _GLOBAL_ALL_DISABLED:
        return None
    cfg = cfg or conf.load_config()
    monitor = get_monitor()

    # 获取配置的数据源优先级
    source_priority = cfg.get("data_source_priority", {}).get("hfq_close", ["baostock", "akshare_em"])

    for src in source_priority:
        try:
            start_time = time.time()
            result = None

            if src == "baostock":
                h = _retry_df(lambda: _bs_kline(code, start, end, 1), what=f"bs_hfq {code}")
                if h is not None and not h.empty:
                    result = h.set_index("trade_date")["close"]
            elif src == "akshare_em":
                fn = ak_fund_etf if is_etf_code(code) else ak_stock
                h = _retry(lambda: _norm_ak(fn(code, start, end, "hfq")), what=f"ak_hfq {code}")
                if h is not None and not h.empty:
                    result = h.set_index("trade_date")["close"]

            elapsed = time.time() - start_time
            success = result is not None and len(result) > 0
            rows = len(result) if success else 0

            monitor.record_call(f"{src}_hfq", success, elapsed, rows, rows)

            if success:
                return result

        except Exception as e:
            elapsed = time.time() - start_time if 'start_time' in dir() else 0
            monitor.record_call(f"{src}_hfq", False, elapsed, 0, 0, str(e))
            continue

    _GLOBAL_ALL_DISABLED = True
    log.warning("后复权所有源均失败 %s → 全局短路", code)
    return None


def fetch_daily(code: str, start: str, end: str, validate: bool = True) -> pd.DataFrame:
    """统一日线。列:code,trade_date,open,high,low,close,volume,amount,adj_factor,
    is_suspended,limit_up,limit_down,source。volume 单位=股;金额=元。

    新增:
    - validate=True 时启用数据质量校验
    - 集成健康度监控
    - 价格异常告警

    ⚠ ETF 用不复权收盘、adj_factor=1.0(ETF现金分红对动量排名影响可忽略,记为取舍);
       个股用后复权因子(baostock 后复权/不复权)。"""
    code = util.with_prefix(code) if code[:2] not in ("sh", "sz", "bj") else code
    etf = is_etf_code(code)
    cfg = conf.load_config()

    raw, source, susp = _fetch_raw(code, start, end, cfg)
    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.copy()
    if etf:
        df["adj_factor"] = 1.0
    else:
        hclose = _fetch_hfq_close(code, start, end, cfg)
        if hclose is not None:
            df["adj_factor"] = (df["trade_date"].map(hclose) / df["close"]).fillna(1.0)
        else:
            df["adj_factor"] = 1.0
    df["adj_factor"] = df["adj_factor"].round(6)

    if susp is not None:
        df["is_suspended"] = df["trade_date"].map(lambda d: 0 if str(susp.get(d, "1")) == "1" else 1)
    else:
        df["is_suspended"] = 0

    # 涨跌停兜底:前收×(1±pct)
    df = df.sort_values("trade_date").reset_index(drop=True)
    prev_close = df["close"].shift(1)
    prev_close.iloc[0] = df["close"].iloc[0] if len(df) else 0
    ups, downs = [], []
    for pc in prev_close:
        u, d = util.price_limits(pc, code)
        ups.append(u); downs.append(d)
    df["limit_up"] = ups
    df["limit_down"] = downs
    df["code"] = code
    df["source"] = source
    df["volume"] = df["volume"].fillna(0)
    df["amount"] = df["amount"].fillna(0)

    # 数据质量校验
    if validate:
        try:
            quality = DataQualityChecker.validate_daily_data(df, code, start, end)
            if not quality['valid']:
                log.warning("数据质量校验未通过 %s: %d 个异常, %d 处缺失",
                           code, len(quality['anomalies']), len(quality['gaps']))
                # 记录严重错误到数据源健康度
                if quality['anomalies']:
                    critical = [a for a in quality['anomalies'] if a.get('severity') == 'error']
                    if critical and source:
                        monitor = get_monitor()
                        monitor.record_call(f"{source}_quality", False, 0, 0, 0,
                                          f"数据质量错误: {critical[0]['message']}")
            else:
                log.debug("数据质量校验通过 %s: %d 行数据正常", code, len(df))
        except Exception as e:
            log.warning("数据质量校验异常 %s: %s", code, e)

    cols = ["code", "trade_date", "open", "high", "low", "close", "volume", "amount",
            "adj_factor", "is_suspended", "limit_up", "limit_down", "source"]
    return df[cols]


# ============ 交易日历 ============
def fetch_calendar(start: str, end: str, cfg=None) -> pd.DataFrame:
    """返回 cal_date,is_open(仅开市日,is_open=1)。支持健康度监控。"""
    cfg = cfg or conf.load_config()
    monitor = get_monitor()

    # 获取配置的数据源优先级
    source_priority = cfg.get("data_source_priority", {}).get("calendar", ["akshare_sina", "baostock"])

    for src in source_priority:
        try:
            start_time = time.time()
            result = None

            if src == "akshare_sina":
                import akshare as ak
                df = _retry(lambda: ak.tool_trade_date_hist_sina(), what="calendar")
                df["cal_date"] = df["trade_date"].astype(str).str[:10]
                result = df
            elif src == "baostock":
                bs = _bs()
                rs = bs.query_trade_dates(start_date=start, end_date=end)
                rows = []
                while (rs.error_code == "0") and rs.next():
                    rows.append(rs.get_row_data())
                d = pd.DataFrame(rows, columns=rs.fields)
                d = d[d["is_trading_day"] == "1"]
                result = pd.DataFrame({"cal_date": d["calendar_date"].astype(str)})

            elapsed = time.time() - start_time
            success = result is not None and not result.empty
            rows = len(result) if success else 0

            monitor.record_call(src, success, elapsed, rows, rows)

            if success:
                result = result[(result["cal_date"] >= start) & (result["cal_date"] <= end)].copy()
                result["is_open"] = 1
                return result[["cal_date", "is_open"]].drop_duplicates("cal_date")

        except Exception as e:
            elapsed = time.time() - start_time if 'start_time' in dir() else 0
            monitor.record_call(src, False, elapsed, 0, 0, str(e))
            log.warning("日历源 %s 失败: %s", src, e)
            continue

    # 所有源失败,返回空
    log.error("所有日历数据源均失败")
    return pd.DataFrame(columns=["cal_date", "is_open"])


# ============ 指数(基准净值 & 成分) ============
def fetch_index_daily(index_code: str, start=DEFAULT_START, end=None) -> pd.DataFrame:
    """指数日线,用于基准对比。返回 code,trade_date,close(+ohlc)。"""
    import akshare as ak
    sym = index_code if index_code[:2] in ("sh", "sz") else util.with_prefix(index_code)
    df = _retry(lambda: ak.stock_zh_index_daily(symbol=sym), what=f"index_daily {sym}")
    df["trade_date"] = df["date"].astype(str).str[:10]
    df["code"] = sym
    if end:
        df = df[df["trade_date"] <= end]
    df = df[df["trade_date"] >= start]
    return df[["code", "trade_date", "open", "high", "low", "close"]]


def fetch_index_members(index_code: str) -> pd.DataFrame:
    """成分股(中证官方 csindex 快照,支持 000300/000906/000852/932000 等)。
    ⚠ 免费源仅当前快照,无历史剔除日 → 存在幸存者偏差(in_date 统一置 DEFAULT_START,
    strategies/报告须注明)。剔除北交所。返回 code,in_date,out_date(NULL=仍在)。"""
    import akshare as ak
    six = util.bare(index_code)
    try:
        df = _retry(lambda: ak.index_stock_cons_csindex(symbol=six), what=f"index_cons {six}")
        codes = df["成分券代码"].map(util.with_prefix)
        out = pd.DataFrame({"code": codes})
        out = out[~out["code"].map(util.is_bj)]                # 剔北交所
        out["in_date"] = DEFAULT_START
        out["out_date"] = None
        return out.drop_duplicates("code").reset_index(drop=True)
    except Exception as e:
        log.warning("成分股抓取失败 %s: %s", index_code, e)
        return pd.DataFrame(columns=["code", "in_date", "out_date"])


def fetch_stock_fundamental(code: str, start: str, end: str) -> pd.DataFrame:
    """个股 PE/PB/流通市值(baostock,可靠)。市值≈amount×100/turn(换手率反推)。
    返回 code,trade_date,pe,pb,market_cap。dividend_yield 由 fundamental.py 用分红表补。"""
    try:
        bs = _bs()
        rs = bs.query_history_k_data_plus(
            _bs_code(code), "date,close,amount,turn,peTTM,pbMRQ",
            start_date=start, end_date=end, frequency="d", adjustflag="3")
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=rs.fields).rename(columns={"date": "trade_date"})
        for c in ("close", "amount", "turn", "peTTM", "pbMRQ"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["pe"] = df["peTTM"]
        df["pb"] = df["pbMRQ"]
        df["market_cap"] = (df["amount"] * 100 / df["turn"]).where(df["turn"] > 0)
        df["code"] = util.with_prefix(code)
        return df[["code", "trade_date", "pe", "pb", "market_cap"]]
    except Exception as e:
        log.warning("基本面抓取失败 %s: %s", code, e)
        return pd.DataFrame()


def fetch_annual_profit(code: str, start_year: int, end_year: int) -> pd.DataFrame:
    """年度盈利质量(卡D)。baostock query_profit_data Q4=全年:roeAvg(净资产收益率,小数)、netProfit(净利润,元)、
    pubDate(公告日,防未来函数用)。返回 code,stat_year,roe,net_profit,pub_date。"""
    bs = _bs()
    bscode = _bs_code(code)
    rows = []
    for y in range(start_year, end_year + 1):
        try:
            rs = bs.query_profit_data(code=bscode, year=y, quarter=4)
            data = []
            while (rs.error_code == "0") and rs.next():
                data.append(rs.get_row_data())
            if not data:
                continue
            d = dict(zip(rs.fields, data[0]))
            roe = pd.to_numeric(d.get("roeAvg"), errors="coerce")
            npf = pd.to_numeric(d.get("netProfit"), errors="coerce")
            pub = d.get("pubDate") or ""
            if pd.notna(roe):
                rows.append({"code": util.with_prefix(code), "stat_year": y,
                             "roe": float(roe),
                             "net_profit": (float(npf) if pd.notna(npf) else None),
                             "pub_date": pub})
        except Exception as e:
            log.warning("年报ROE抓取失败 %s %d: %s", code, y, e)
    return pd.DataFrame(rows)


def fetch_index_pe(index_name: str = "沪深300") -> pd.DataFrame:
    """指数滚动市盈率历史(乐咕乐股)。返回 trade_date,pe。用于 S5 的PE分位。"""
    import akshare as ak
    df = _retry(lambda: ak.stock_index_pe_lg(symbol=index_name), what=f"index_pe {index_name}")
    df = df.rename(columns={"日期": "trade_date", "滚动市盈率": "pe"})
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
    return df[["trade_date", "pe"]].dropna()


# ============ ETF 份额折算/拆分校正(卡C) ============
def reconcile_etf_splits(codes, conn=None, threshold=0.15):
    """ETF 不设 adj_factor 会被"份额折算/拆分"污染:ETF 单日价格不可能超 ±10%(涨跌停),
    故 |日收益|>threshold 必为折算/拆分(或坏点)。据此:
      1) 写 送转事件到 dividend 表(shares_ratio),让引擎在折算日按比例调整持仓股数 → NAV 连续;
      2) 重算后复权 adj_factor(最新=1.0,折算日之前按比例缩放)→ 动量信号连续。
    仅影响有折算的 ETF;宽基 ETF(510300 等)无折算,为 no-op。返回 (更新的bar行数, 写入的送转事件数)。"""
    own = conn is None
    if own:
        conn = get_conn()
    bar_updated, div_written = 0, 0
    try:
        for code in codes:
            if not is_etf_code(code):
                continue
            rows = conn.execute(
                "SELECT trade_date, close, adj_factor FROM daily_bar WHERE code=? ORDER BY trade_date",
                (code,)).fetchall()
            if len(rows) < 2:
                continue
            dates = [r[0] for r in rows]
            closes = [r[1] for r in rows]
            old_adj = [(r[2] if r[2] is not None else 1.0) for r in rows]
            n = len(rows)
            adj = [1.0] * n
            splits = []                                   # (ex_date, ratio)
            for i in range(n - 1, 0, -1):
                r = (closes[i] / closes[i - 1]) if (closes[i - 1] and closes[i]) else 1.0
                if abs(r - 1) > threshold:                # 折算/拆分日
                    adj[i - 1] = adj[i] * r
                    splits.append((dates[i], r))
                else:
                    adj[i - 1] = adj[i]
            # 1) 写 送转(shares_ratio):price×ratio ⇒ shares×(1/ratio),故 shares_ratio = 1/ratio - 1
            for ex_date, r in splits:
                if r <= 0:
                    continue
                sr = round(1.0 / r - 1.0, 6)
                conn.execute(
                    "INSERT OR REPLACE INTO dividend (code, ex_date, cash_per_share, shares_ratio) "
                    "VALUES (?,?,0,?)", (code, ex_date, sr))
                div_written += 1
            # 2) 回写变化的 adj_factor
            changed = [(round(adj[i], 8), code, dates[i]) for i in range(n)
                       if abs(adj[i] - old_adj[i]) > 1e-9]
            if changed:
                conn.executemany("UPDATE daily_bar SET adj_factor=? WHERE code=? AND trade_date=?", changed)
                bar_updated += len(changed)
            if splits:
                conn.commit()
                log.info("ETF折算校正 %s:%d 个折算点,更新 %d 行 adj_factor", code, len(splits), len(changed))
        conn.commit()
    finally:
        if own:
            conn.close()
    return bar_updated, div_written


# ============ 实时行情(卡G:盘中开盘校准用) ============
# 唯一取数入口原则:实时价也走本文件。腾讯 qt.gtimg.cn(无需 referer,主) → 新浪 hq.sinajs.cn(备)。
# 均 GBK 编码、返回 JS 变量赋值。Actions 无代理直连;单只失败跳过(上层回退昨收)。
def _parse_tencent(text):
    out = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        try:
            head, payload = line.split("=", 1)
            code = head[2:].strip()                       # v_sh510300 -> sh510300
            f = payload.strip().strip('"').split("~")
            price, prev = float(f[3]), float(f[4])
            openp = float(f[5]) if len(f) > 5 and f[5] else 0.0
            t = f[30] if len(f) > 30 else ""
            if price > 0:
                out[code] = {"price": price, "prev_close": prev, "open": openp, "time": t}
        except Exception:
            continue
    return out


def _parse_sina(text):
    out = {}
    for line in text.split(";"):
        line = line.strip()
        if "hq_str_" not in line or "=" not in line:
            continue
        try:
            head, payload = line.split("=", 1)
            code = head.split("hq_str_")[1].strip()
            f = payload.strip().strip('"').split(",")
            if len(f) < 4:
                continue
            openp, prev, price = float(f[1]), float(f[2]), float(f[3])
            t = (f[30] + " " + f[31]) if len(f) > 31 else ""
            if price > 0:
                out[code] = {"price": price, "prev_close": prev, "open": openp, "time": t}
        except Exception:
            continue
    return out


def _fetch_tencent(codes):
    r = requests.get("https://qt.gtimg.cn/q=" + ",".join(codes), timeout=6)
    r.encoding = "gbk"
    return _parse_tencent(r.text)


def _fetch_sina(codes):
    r = requests.get("https://hq.sinajs.cn/list=" + ",".join(codes), timeout=6,
                     headers={"Referer": "https://finance.sina.com.cn"})
    r.encoding = "gbk"
    return _parse_sina(r.text)


def fetch_realtime(codes, cfg=None) -> dict:
    """实时行情。返回 {code: {price, prev_close, open, time}}。
    多源兜底+健康度监控,缺失的标的不在返回里。"""
    cfg = cfg or conf.load_config()
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return {}

    # 获取配置的数据源优先级
    source_priority = cfg.get("data_source_priority", {}).get("realtime", ["tencent", "sina"])

    # 数据源名称到函数的映射
    source_map = {
        "tencent": _fetch_tencent,
        "sina": _fetch_sina,
    }

    monitor = get_monitor()
    out = {}

    for name in source_priority:
        if name not in source_map:
            continue
        fn = source_map[name]
        missing = [c for c in codes if c not in out]
        if not missing:
            break

        try:
            start_time = time.time()
            result = _retry(lambda: fn(missing), tries=2, what=f"realtime_{name}")
            elapsed = time.time() - start_time

            success = bool(result)
            rows = len(result) if result else 0

            monitor.record_call(name, success, elapsed, rows, len(missing))

            if result:
                out.update(result)
                log.debug("实时行情 %s 获取 %d/%d 只, %.2fs", name, len(result), len(missing), elapsed)

        except Exception as e:
            elapsed = time.time() - start_time if 'start_time' in dir() else 0
            monitor.record_call(name, False, elapsed, 0, len(missing), str(e))
            log.warning("实时行情 %s 失败: %s", name, e)

    return out


# ============ 分红(除权) ============
def fetch_dividend(code: str) -> pd.DataFrame:
    """返回 code,ex_date,cash_per_share,shares_ratio。
    源:stock_fhps_detail_em。现金分红比例是"每10股派X元",送转是"每10股送转Y股"→均÷10。"""
    import akshare as ak
    six = util.bare(code)
    try:
        df = _retry(lambda: ak.stock_fhps_detail_em(symbol=six), what=f"dividend {six}")
    except Exception as e:
        log.warning("分红抓取失败 %s: %s", code, e)
        return pd.DataFrame(columns=["code", "ex_date", "cash_per_share", "shares_ratio"])
    if df is None or df.empty:
        return pd.DataFrame(columns=["code", "ex_date", "cash_per_share", "shares_ratio"])
    ex = pd.to_datetime(df.get("除权除息日"), errors="coerce")
    cash = pd.to_numeric(df.get("现金分红-现金分红比例"), errors="coerce").fillna(0) / 10.0
    sr = pd.to_numeric(df.get("送转股份-送转总比例"), errors="coerce").fillna(0) / 10.0
    out = pd.DataFrame({
        "code": util.with_prefix(code),
        "ex_date": ex.dt.strftime("%Y-%m-%d"),
        "cash_per_share": cash.round(6),
        "shares_ratio": sr.round(6),
    })
    out = out[out["ex_date"].notna() & ((out["cash_per_share"] > 0) | (out["shares_ratio"] > 0))]
    return out.reset_index(drop=True)


# ============ 证券信息 ============
def fetch_security_info(codes: list) -> pd.DataFrame:
    """返回 code,name,type,is_t0,list_date,status。
    个股走 baostock query_stock_basic(稳定);ETF 用内置名+前缀判 T+0。"""
    rows = []
    for code in codes:
        code = util.with_prefix(code) if code[:2] not in ("sh", "sz", "bj") else code
        if is_etf_code(code):
            rows.append({
                "code": code, "name": _ETF_UNIVERSE_NAME.get(code, util.bare(code)),
                "type": "etf", "is_t0": _is_t0(code), "list_date": None, "status": "L",
            })
            continue
        try:
            bs = _bs()
            rs = bs.query_stock_basic(code=_bs_code(code))
            data = []
            while (rs.error_code == "0") and rs.next():
                data.append(rs.get_row_data())
            if data:
                d = dict(zip(rs.fields, data[0]))  # code,code_name,ipoDate,outDate,type,status
                name = d.get("code_name", util.bare(code))
                status = "D" if d.get("status") == "0" else ("ST" if "ST" in name.upper() else "L")
                rows.append({
                    "code": code, "name": name, "type": "stock", "is_t0": 0,
                    "list_date": d.get("ipoDate") or None, "status": status,
                })
                continue
        except Exception as e:
            log.warning("证券信息失败 %s: %s", code, e)
        rows.append({"code": code, "name": util.bare(code), "type": "stock",
                     "is_t0": 0, "list_date": None, "status": "L"})
    return pd.DataFrame(rows)


# ============ 写库 ============
def upsert(df: pd.DataFrame, table: str, conn=None):
    """写库,主键冲突则替换(INSERT OR REPLACE)。只写库表中存在的列。"""
    if df is None or df.empty:
        return 0
    own = conn is None
    if own:
        conn = get_conn()
    cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    table_cols = [c[1] for c in cols_info]
    use = [c for c in df.columns if c in table_cols]
    if not use:
        if own:
            conn.close()
        raise ValueError(f"upsert: df 无与 {table} 匹配的列。df={list(df.columns)} table={table_cols}")
    placeholders = ",".join("?" * len(use))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(use)}) VALUES ({placeholders})"
    data = [tuple(None if pd.isna(v) else v for v in row) for row in df[use].itertuples(index=False, name=None)]
    conn.executemany(sql, data)
    conn.commit()
    n = len(data)
    if own:
        conn.close()
    return n


def bs_logout():
    global _bs_logged_in
    if _bs_logged_in:
        try:
            import baostock as bs
            bs.logout()
        except Exception:
            pass
        _bs_logged_in = False


# ============ 健康度报告接口 ============

def get_data_source_health_report() -> dict:
    """获取数据源健康度报告"""
    return check_data_source_health()


def reset_data_source_health(source_name: str = None):
    """重置数据源健康状态

    Args:
        source_name: 指定源名,None则重置所有
    """
    from data_health import get_monitor
    monitor = get_monitor()
    if source_name:
        monitor.reset_source(source_name)
    else:
        for name in list(monitor.sources.keys()):
            monitor.reset_source(name)
