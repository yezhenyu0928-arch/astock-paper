# -*- coding: utf-8 -*-
"""数据适配层 —— 全项目唯一允许 import akshare / baostock 的文件(SPEC 模块1)。
职责:把各源的原始字段翻译成统一 schema,写库。上层只认统一列名,不碰数据源差异。

本机现实(见探针):
- 数据源均为国内主机,本不该走代理;本机代理会间歇性掐断东财接口。
  → 抓取时临时摘掉 HTTP(S)_PROXY 直连(_no_proxy),Actions 无代理时是 no-op。
- akshare 东财接口间歇失败 → 重试 + baostock 兜底(baostock 走裸 socket,稳定)。
- akshare 成交量单位=手,baostock=股;统一存"股"(akshare ×100)。
"""
import os
import time
import logging
import contextlib
import pandas as pd

import util
from db import get_conn, init_db

log = logging.getLogger("data_adapter")

DEFAULT_START = "2018-01-01"
_VOL_LOTS_TO_SHARES = 100      # akshare 成交量:手→股

# 已知 T+0 的 ETF(黄金/QDII跨境/债券),其余 ETF 与个股为 T+1
_T0_PREFIX = ("511", "513", "518", "159012")  # 债券/跨境/黄金;个别跨境159也T+0,按需扩充
_ETF_UNIVERSE_NAME = {
    "sh510300": "沪深300ETF", "sh510500": "中证500ETF", "sh512890": "红利低波ETF",
    "sh518880": "黄金ETF", "sh513100": "纳指ETF", "sh511010": "国债ETF",
}

_bs_logged_in = False


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


def _bs():
    global _bs_logged_in
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
    b = _bs_kline(code, start, end, 3)
    if b is None or b.empty:
        return None, None
    raw = b[["trade_date", "open", "high", "low", "close", "volume", "amount"]].copy()
    susp = b.set_index("trade_date")["tradestatus"] if "tradestatus" in b else None
    return raw, susp


def _fetch_raw(code, start, end):
    """不复权日线。ETF→akshare(fund_etf_hist_em,全历史可靠)优先;个股→baostock(push2his flaky)优先。
    返回 (df, source, susp_series|None)。"""
    etf = is_etf_code(code)
    order = [("sina_etf", _sina_etf_raw), ("baostock", _bs_raw), ("akshare_em", _ak_raw)] if etf \
        else [("baostock", _bs_raw), ("akshare_em", _ak_raw)]
    for name, fn in order:
        try:
            df, susp = fn(code, start, end)
            if df is not None and not df.empty:
                return df, name, susp
        except Exception as e:
            log.warning("%s 不复权失败 %s: %s", name, code, e)
    log.error("双源均失败(不复权) %s", code)
    return None, None, None


def _fetch_hfq_close(code, start, end):
    """后复权收盘(仅算 adj_factor 用),个股 baostock 优先。返回 Series(index=trade_date) 或 None。"""
    for src in ("bs", "ak"):
        try:
            if src == "bs":
                h = _bs_kline(code, start, end, 1)
                if h is not None and not h.empty:
                    return h.set_index("trade_date")["close"]
            else:
                fn = ak_fund_etf if is_etf_code(code) else ak_stock
                h = _retry(lambda: _norm_ak(fn(code, start, end, "hfq")), what=f"ak_hfq {code}")
                if h is not None and not h.empty:
                    return h.set_index("trade_date")["close"]
        except Exception:
            continue
    return None


def fetch_daily(code: str, start: str, end: str) -> pd.DataFrame:
    """统一日线。列:code,trade_date,open,high,low,close,volume,amount,adj_factor,
    is_suspended,limit_up,limit_down,source。volume 单位=股;金额=元。
    ⚠ ETF 用不复权收盘、adj_factor=1.0(ETF现金分红对动量排名影响可忽略,记为取舍);
       个股用后复权因子(baostock 后复权/不复权)。"""
    code = util.with_prefix(code) if code[:2] not in ("sh", "sz", "bj") else code
    etf = is_etf_code(code)

    raw, source, susp = _fetch_raw(code, start, end)
    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.copy()
    if etf:
        df["adj_factor"] = 1.0
    else:
        hclose = _fetch_hfq_close(code, start, end)
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
    cols = ["code", "trade_date", "open", "high", "low", "close", "volume", "amount",
            "adj_factor", "is_suspended", "limit_up", "limit_down", "source"]
    return df[cols]


# ============ 交易日历 ============
def fetch_calendar(start: str, end: str) -> pd.DataFrame:
    """返回 cal_date,is_open(仅开市日,is_open=1)。"""
    try:
        import akshare as ak
        df = _retry(lambda: ak.tool_trade_date_hist_sina(), what="calendar")
        df["cal_date"] = df["trade_date"].astype(str).str[:10]
    except Exception as e:
        log.warning("akshare 日历失败,转 baostock: %s", e)
        bs = _bs()
        rs = bs.query_trade_dates(start_date=start, end_date=end)
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        d = pd.DataFrame(rows, columns=rs.fields)  # calendar_date,is_trading_day
        d = d[d["is_trading_day"] == "1"]
        df = pd.DataFrame({"cal_date": d["calendar_date"].astype(str)})
    df = df[(df["cal_date"] >= start) & (df["cal_date"] <= end)].copy()
    df["is_open"] = 1
    return df[["cal_date", "is_open"]].drop_duplicates("cal_date")


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


def fetch_index_pe(index_name: str = "沪深300") -> pd.DataFrame:
    """指数滚动市盈率历史(乐咕乐股)。返回 trade_date,pe。用于 S5 的PE分位。"""
    import akshare as ak
    df = _retry(lambda: ak.stock_index_pe_lg(symbol=index_name), what=f"index_pe {index_name}")
    df = df.rename(columns={"日期": "trade_date", "滚动市盈率": "pe"})
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
    return df[["trade_date", "pe"]].dropna()


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
