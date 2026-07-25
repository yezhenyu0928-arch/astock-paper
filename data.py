# -*- coding: utf-8 -*-
"""数据编排:增量更新(update_all) + 质检(check)。SPEC 模块1。
增量:每标的取库内最大 trade_date,从其次日拉到今天;空库从 2018-01-01。
基准指数亦存入 daily_bar(code=sh000300 等),复用 schema,供净值对比。
"""
import logging
import pandas as pd

import util
import trade_calendar as cal
import data_adapter as da
from data_adapter import DEFAULT_START
from db import get_conn, init_db
import conf

log = logging.getLogger("data")


# ---------- 目标代码集 ----------
def core_etf_codes(cfg, registry) -> set:
    """交易赖以运行的核心 ETF 池:S2 动量池 + S5 网格标的 + S6 行业池(注册后自动纳入)。
    这些是"当日必须有数据、否则暂停跟单"的标的。"""
    codes = set(registry.get("s2_etf@v1", {}).get("universe", []) or [])
    codes |= set((cfg.get("custom") or {}).get("s2_universe_extra", []) or [])
    codes |= set(registry.get("s5_grid@v1", {}).get("universe", []) or [])
    codes |= set(registry.get("s6_sector@v1", {}).get("universe", []) or [])   # 卡B/卡C:S6 行业ETF池
    return {c for c in codes if da.is_etf_code(c)}


def held_codes_from_state() -> set:
    """扫描 state/*.json 得到所有策略当前持仓标的(卡B:持仓当日缺数据要告警)。"""
    import json
    import glob
    out = set()
    for f in glob.glob(str(conf.STATE_DIR / "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
            out |= set((d.get("positions") or {}).keys())
        except Exception:
            pass
    return out


def benchmark_codes(registry) -> set:
    out = set()
    for v in registry.values():
        b = v.get("benchmark")
        if b:
            out.add(b)
    return out


# ---------- 增量更新 ----------
def _max_date(conn, code) -> str | None:
    r = conn.execute("SELECT max(trade_date) FROM daily_bar WHERE code=?", (code,)).fetchone()
    return r[0] if r and r[0] else None


def update_calendar(end_pad_year=1):
    end = f"{util.now_cn().year + end_pad_year}-12-31"
    df = da.fetch_calendar(DEFAULT_START, end)
    n = da.upsert(df, "trade_calendar")
    cal.reset_cache()
    log.info("日历更新 %d 行", n)
    return n


def update_daily(codes, conn=None, timeout_flag=None, timeout_check=None) -> dict:
    """增量更新日线。codes 过多时支持通过 timeout_flag/check 回调提前终止。"""
    own = conn is None
    if own:
        conn = get_conn()
    today = util.today_str()
    summary = {}
    for i, code in enumerate(codes):
        if timeout_check and timeout_flag and timeout_flag["expired"]:
            log.warning("update_daily 超时,提前终止(已处理 %d/%d 个)", i, len(codes))
            break
        mx = _max_date(conn, code)
        start = cal.next_trade_day(mx) if mx else DEFAULT_START
        if mx and start <= mx:                    # 冗余保护
            start = cal.next_trade_day(mx)
        if start > today:
            summary[code] = 0
            continue
        try:
            df = da.fetch_daily(code, start, today)
            n = da.upsert(df, "daily_bar", conn=conn)
            summary[code] = n
        except Exception as e:
            log.error("日线更新失败 %s: %s", code, e)
            summary[code] = -1
    if own:
        conn.close()
    return summary


def update_index_daily(codes, conn=None) -> dict:
    """指数日线写入 daily_bar(仅价格,volume/amount=0)。"""
    own = conn is None
    if own:
        conn = get_conn()
    summary = {}
    for code in codes:
        if da.is_etf_code(code):     # 已在 update_daily 处理
            continue
        try:
            df = da.fetch_index_daily(code, start=DEFAULT_START, end=util.today_str())
            if df.empty:
                summary[code] = 0
                continue
            df = df.sort_values("trade_date").reset_index(drop=True)
            df["volume"] = 0.0
            df["amount"] = 0.0
            df["adj_factor"] = 1.0
            df["is_suspended"] = 0
            prev = df["close"].shift(1)
            prev.iloc[0] = df["close"].iloc[0]
            df["limit_up"] = (prev * 1.1).round(3)
            df["limit_down"] = (prev * 0.9).round(3)
            df["source"] = "akshare_index"
            n = da.upsert(df, "daily_bar", conn=conn)
            summary[code] = n
        except Exception as e:
            log.warning("指数更新失败 %s: %s", code, e)
            summary[code] = -1
    if own:
        conn.close()
    return summary


def update_security(codes, conn=None) -> int:
    df = da.fetch_security_info(list(codes))
    return da.upsert(df, "security", conn=conn)


def update_members(index_code="sh000300", conn=None) -> int:
    df = da.fetch_index_members(index_code)
    if df.empty:
        return 0
    df["index_code"] = index_code
    # 先删该指数旧成分再插,避免重复累加(index_members 无唯一约束)
    own = conn is None
    if own:
        conn = get_conn()
    conn.execute("DELETE FROM index_members WHERE index_code=?", (index_code,))
    conn.commit()
    n = da.upsert(df, "index_members", conn=conn)
    if own:
        conn.close()
    return n


def update_dividend(codes, conn=None) -> int:
    total = 0
    for code in codes:
        if da.is_etf_code(code):
            continue
        df = da.fetch_dividend(code)
        total += da.upsert(df, "dividend", conn=conn)
    return total


def update_all(cfg=None, registry=None, extra_codes=None, with_members=True,
               with_dividend=False, stock_codes=None,
               _timeout_check=None, _timeout_flag=None) -> dict:
    """主更新流程。默认更新:日历 + 核心ETF + 基准指数 + 沪深300成分 + 证券信息。
    stock_codes:额外要更新日线的个股(S3/S4 池),None 则不拉个股(避免全A重负载)。
    _timeout_check/_timeout_flag:海外Runner超时提前终止,由 run_daily 传入。"""
    cfg = cfg or conf.load_config()
    registry = registry or conf.load_registry()
    init_db()
    result = {}
    result["calendar"] = update_calendar()

    etfs = core_etf_codes(cfg, registry)
    if extra_codes:
        etfs |= set(c for c in extra_codes if da.is_etf_code(c))
    conn = get_conn()
    try:
        result["etf_daily"] = update_daily(sorted(etfs), conn=conn)
        if _timeout_check and _timeout_flag and _timeout_flag["expired"]:
            log.warning("update_all 超时,跳过指数/成分更新(已有ETF数据)")
            return result
        try:
            bu, dw = da.reconcile_etf_splits(sorted(etfs), conn=conn)
            result["etf_split_fix"] = {"adj_rows": bu, "div_events": dw}
        except Exception as e:
            log.warning("ETF折算校正失败(不阻断):%s", e)
        if _timeout_check and _timeout_flag and _timeout_flag["expired"]:
            log.warning("update_all 超时,跳过指数更新")
            return result
        result["index_daily"] = update_index_daily(sorted(benchmark_codes(registry)), conn=conn)
        if with_members:
            result["members_sh000300"] = update_members("sh000300", conn=conn)
        if stock_codes:
            result["stock_daily"] = update_daily(sorted(set(stock_codes)), conn=conn)
        sec_codes = set(etfs)
        if stock_codes:
            sec_codes |= set(stock_codes)
        result["security"] = update_security(sec_codes, conn=conn)
        if with_dividend and stock_codes:
            result["dividend"] = update_dividend(set(stock_codes), conn=conn)
    finally:
        conn.close()
        da.bs_logout()
    return result


# ---------- 质检 ----------
class DataCheckError(Exception):
    pass


def check(date, tradable_codes=None, index_codes=None, conn=None):
    """SPEC 质检:①开市日**可交易标的**缺当日数据→FAIL(基准指数缺→仅WARN,指数常晚一日)
    ②非新股涨跌幅>板块限幅+1%→WARN ③adj_factor 回退→FAIL。FAIL 抛 DataCheckError 阻断主流程。"""
    date = util.to_date_str(date)
    own = conn is None
    if own:
        conn = get_conn()
    warnings = []
    try:
        if not cal.is_trade_day(date):
            log.info("check: %s 非交易日,跳过", date)
            return {"ok": True, "warnings": [], "note": "非交易日"}

        default_path = tradable_codes is None    # 仅默认路径(run_daily)才追加持仓标的校验
        if tradable_codes is None:
            cfg = conf.load_config(); reg = conf.load_registry()
            tradable_codes = sorted(core_etf_codes(cfg, reg))
        if index_codes is None:
            reg = conf.load_registry()
            index_codes = sorted(benchmark_codes(reg) - {c for c in tradable_codes})

        # ① 开市日缺数据:可交易标的 FAIL,基准指数仅 WARN
        for code in tradable_codes:
            r = conn.execute("SELECT close FROM daily_bar WHERE code=? AND trade_date=?",
                             (code, date)).fetchone()
            if r is None:
                raise DataCheckError(f"开市日 {date} 可交易标的 {code} 缺日线数据(FAIL)")
        for code in index_codes:
            r = conn.execute("SELECT close FROM daily_bar WHERE code=? AND trade_date=?",
                             (code, date)).fetchone()
            if r is None:
                warnings.append(f"基准指数 {code} 缺 {date} 数据(WARN,指数常晚一日)")

        # ①b 持仓标的当日缺数据(卡B):真数据缺口→FAIL;近5日曾有bar→疑似停牌→WARN(不误停跟单)
        if default_path:
            for code in sorted(held_codes_from_state() - set(tradable_codes)):
                r = conn.execute("SELECT close FROM daily_bar WHERE code=? AND trade_date=?",
                                 (code, date)).fetchone()
                if r is not None:
                    continue
                recent = conn.execute(
                    "SELECT count(*) FROM daily_bar WHERE code=? AND trade_date<? "
                    "AND trade_date>=?", (code, date, cal.prev_trade_day(date, 5))).fetchone()[0]
                if recent > 0:
                    warnings.append(f"持仓 {code} 缺 {date} 数据(WARN,疑似停牌)")
                else:
                    raise DataCheckError(f"持仓 {code} 连续无日线数据(FAIL,疑数据缺口)")

        # ②③ 逐标的检查涨跌幅与复权因子(仅可交易标的)
        for code in tradable_codes:
            rows = conn.execute(
                "SELECT trade_date, close, adj_factor FROM daily_bar WHERE code=? "
                "AND trade_date<=? ORDER BY trade_date DESC LIMIT 40",
                (code, date)).fetchall()
            if len(rows) < 2:
                continue
            closes = [(r[0], r[1], r[2]) for r in rows]  # desc
            # ② 当日涨跌幅
            td, c0, _ = closes[0]
            _, c1, _ = closes[1]
            if c1 and c0:
                pct = c0 / c1 - 1
                lim = util.limit_pct(code) + 0.01
                if abs(pct) > lim:
                    warnings.append(f"{code} {td} 涨跌幅 {pct:.1%} 超 {lim:.0%}(WARN)")
            # ③ adj_factor 回退(时间升序应非降)
            afs = [(r[0], r[2]) for r in reversed(closes)]  # asc
            for (d_prev, a_prev), (d_cur, a_cur) in zip(afs, afs[1:]):
                # 后复权因子应非降;容忍 0.5% 源噪声,只在实质回退(疑似源换基)时 FAIL
                if a_prev and a_cur and a_cur < a_prev * 0.995:
                    raise DataCheckError(
                        f"{code} adj_factor 回退 {d_prev}:{a_prev} -> {d_cur}:{a_cur}(FAIL)")
        for w in warnings:
            log.warning("check WARN: %s", w)
        return {"ok": True, "warnings": warnings}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    r = update_all()
    print(r)
    print(check(util.today_str()))
