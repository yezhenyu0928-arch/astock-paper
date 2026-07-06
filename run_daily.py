# -*- coding: utf-8 -*-
"""每日主流程(SPEC 模块6)。幂等,可重复执行。
1 非交易日→心跳退出;2 更新数据+质检(FAIL 告警退出1);3 settle 撮合昨日信号;
4 有回报→推送;5 run_strategies 生成明日计划;6 有计划→推送;7 心跳;8 落盘。
用法:python run_daily.py [--date YYYY-MM-DD] [--only s2_etf@v1,...]"""
import sys
import logging
import argparse
import threading

import conf
import util
import data
import trade_calendar as cal
import notify
import data_adapter as da
from db import get_conn
from engine import Engine

log = logging.getLogger("run_daily")

# 海外 Runner 数据更新超时(秒)。超时后降级继续跑引擎,不挂死 30 分钟。
# ETF 数据(~15个)约 30-60 秒能取完;个股(~300个)yfinance 每个 1-2 秒,
# 超时后引擎用缓存DB+已有ETF新数据继续,不影响 ETF动量轮动/网格策略。
_DATA_TIMEOUT = 180  # 3 分钟


class TimeoutError(Exception):
    pass


def _timeout_guard(seconds):
    """数据更新超时守卫:用 threading.Timer + 线程安全标志位。
    不同于 signal.alarm(Unix only),在 Windows 上也工作。"""
    flag = {"expired": False}

    def _on_timeout():
        flag["expired"] = True

    timer = threading.Timer(seconds, _on_timeout)
    timer.start()
    return flag, timer


def _check_timeout(flag):
    if flag["expired"]:
        raise TimeoutError("数据更新超时,降级继续")


def _stock_universe(cfg, reg, conn):
    """启用的个股策略所需的日线更新范围(S3=沪深300成分;S1/S4 全A 由 P9 数据流补,暂取沪深300兜底)。"""
    codes = set()
    enabled = [s for s, on in cfg.get("strategies", {}).items() if on]
    need_members = any(s in enabled for s in ("s3_ma_trend@v1",))
    need_full = any(s in enabled for s in ("s1_dividend@v1", "s4_smallcap@v1"))
    if need_members or need_full:
        rows = conn.execute("SELECT code FROM index_members WHERE index_code='sh000300'").fetchall()
        codes |= {r[0] for r in rows}
    if need_full:
        log.warning("S1/S4 需全A历史,当前以沪深300成分兜底(完整全A补数见 P7/P9)")
    return codes


def _fund_fail_track(failed, cfg):
    """卡B:基本面接口连续失败跟踪。连续≥2日失败→告警一次(S1/S4 将沿用库内旧基本面)。
    用 state/fund_fail_count.txt 持久化计数;成功即清零。"""
    p = conf.STATE_DIR / "fund_fail_count.txt"
    try:
        prev = int((p.read_text(encoding="utf-8").strip() or "0"))
    except Exception:
        prev = 0
    cur = prev + 1 if failed else 0
    try:
        p.write_text(str(cur), encoding="utf-8")
    except Exception:
        pass
    if failed and cur >= 2:
        try:
            t, c = notify.build_alert(
                f"🟡 基本面接口连续 {cur} 日更新失败,S1/S4 将沿用库内旧基本面数据;"
                f"请检查 baostock/akshare 是否漂移。")
            notify.push(t, c, "alert", cfg)
        except Exception:
            pass


def _render_plan_items(eng, ctx, sid, orders):
    """把订单渲染成推送 items(名称/参考价/数量描述)。"""
    acct = eng.load_account(sid)
    total = acct.total(eng._price_of(ctx.date))
    items = []
    for o in orders:
        close = ctx.raw_close(o.code) or 0
        if o.side == "sell" or o.weight == 0:
            held = acct.positions.get(o.code)
            qty = f"全部{held.shares if held else 0}股"
        else:
            est = util.floor100(total * o.weight / close) if close else 0
            qty = f"约{o.weight*100:.0f}%仓位≈{est}股"
        items.append({"side": o.side, "code": o.code, "name": ctx.name(o.code),
                      "qty_desc": qty, "ref_price": util.r2(close), "reason": o.reason})
    return items


def _render_fill_items(eng, ctx, reports):
    items = []
    for r in reports:
        items.append({"side": r["side"], "code": r["code"], "name": ctx.name(r["code"]),
                      "shares": r["shares"], "sim_price": r["sim_price"],
                      "fee": r["fee"], "tax": r["tax"], "status": r["status"]})
    return items


def run(date=None, only=None):
    cfg = conf.load_config()
    reg = conf.load_registry()
    if only:
        cfg["strategies"] = {s: (s in only) for s in reg}
    today = util.to_date_str(date) if date else util.today_str()

    # 1 非交易日
    if not cal.is_trade_day(today):
        t, c = notify.build_heartbeat(today, today, "非交易日,休市")
        notify.push(t, c, "heartbeat", cfg)
        log.info("非交易日 %s,退出", today)
        return 0

    # 1.5 数据库丢失自检(卡B):防 Actions cache 被驱逐后静默空库跑。
    # 首次部署须先跑 backfill 工作流(README 步骤6);此后 daily 每次都应见到完整历史(>>5万行)。
    conn0 = get_conn()
    try:
        try:
            n_bar = conn0.execute("SELECT count(*) FROM daily_bar").fetchone()[0]
        except Exception:
            n_bar = 0
    finally:
        conn0.close()
    if n_bar < 50000:
        t, c = notify.build_alert(
            f"🛑 数据库疑似丢失(daily_bar 仅 {n_bar} 行,Actions cache 可能被驱逐)。"
            f"请手动运行 backfill 工作流重建历史库后再跟单;今日已暂停,未产生任何操作。")
        notify.push(t, c, "alert", cfg)
        log.error("DB自检失败:daily_bar=%d 行(<50000),疑似 cache 驱逐,退出", n_bar)
        return 1

    # 2 更新数据 + 质检
    conn = get_conn()
    try:
        flag, timer = _timeout_guard(_DATA_TIMEOUT)
        try:
            data.update_all(cfg, reg, with_members=True)
            _check_timeout(flag)
            stock_codes = _stock_universe(cfg, reg, conn)
            if stock_codes:
                data.update_daily(sorted(stock_codes), conn=conn, timeout_flag=flag, timeout_check=_check_timeout)
                _check_timeout(flag)
                data.update_security(stock_codes, conn=conn)
                fund_ok = True
                try:
                    import fundamental as F
                    F.update_stock_fundamental(sorted(stock_codes), conn=conn)
                    if cfg.get("strategies", {}).get("s1_dividend@v2") and util.now_cn().month <= 5:
                        F.update_annual_roe(sorted(stock_codes), conn=conn)
                except Exception as e:
                    fund_ok = False
                    log.warning("基本面更新失败(不阻断):%s", e)
                _fund_fail_track(not fund_ok, cfg)
            if cfg.get("strategies", {}).get("s5_grid@v1"):
                try:
                    import fundamental as F
                    F.update_index_pe("sh000300", conn=conn)
                except Exception as e:
                    log.warning("指数PE更新失败:%s", e)
        except TimeoutError:
            log.warning("数据更新超时(%ds),使用缓存DB继续引擎流程", _DATA_TIMEOUT)
        finally:
            timer.cancel()
        # 质检:验证今天是否有新数据入库(update_all静默吞异常,不会因数据断流抛错)
        has_today_data = conn.execute(
            "SELECT count(*) FROM daily_bar WHERE trade_date=?", (today,)
        ).fetchone()[0] > 0
        if has_today_data:
            try:
                chk = data.check(today, conn=conn)
                for w in chk.get("warnings", []):
                    log.warning(w)
            except data.DataCheckError as e:
                t, c = notify.build_alert(f"数据质检失败:{e},今日暂停跟单")
                notify.push(t, c, "alert", cfg)
                log.error("质检FAIL:%s", e)
                return 1
        else:
            log.warning("今天无新数据入库(海外Runner数据源不可达),跳过质检,使用缓存DB继续")
    finally:
        conn.close()
        da.bs_logout()

    # 消息面:盘后先扫市场分(供风控降敞口) + 产业主题扫描(供策略叠加),须在 run_strategies 之前
    news_on = (cfg.get("news_layer") or {}).get("enabled")
    mkt_score = None
    industry_result = None
    if news_on:
        try:
            import news_adapter as na
            import news_engine as ne
            na.store_news(na.fetch_flash())
            mkt_score, _ = ne.scan_market(today)
            log.info("消息面市场分:%s", mkt_score)
            # 产业主题扫描(新增)
            try:
                industry_result = ne.scan_industry_themes(today)
                if industry_result and industry_result.get("themes"):
                    log.info("产业主题:%d个主题,行业信号:%s",
                             len(industry_result["themes"]),
                             {k: v for k, v in industry_result.get("sector_score", {}).items() if v != 0})
            except Exception as e:
                log.warning("产业主题扫描失败(降级):%s", e)
        except Exception as e:
            log.warning("消息面扫描失败(降级,不阻断):%s", e)

    # 3-8 引擎流程
    eng = Engine(cfg, reg)
    try:
        ctx = eng.ctx(today)
        # 3 撮合昨日信号
        reports = eng.settle(today)
        # 4 成交回报
        if reports:
            items = _render_fill_items(eng, ctx, reports)
            t, c = notify.build_fill_message(today, items)
            notify.push(t, c, "op", cfg)
        # 5 生成明日计划(risk 内已按市场分降敞口)
        orders = eng.run_strategies(today)
        # 5.5 持仓黑天鹅:强制清仓单 + 警示
        if news_on:
            try:
                import news_engine as ne
                accts = {s: eng.load_account(s) for s in eng.enabled_strategies()}
                bs_sells, warns = ne.blackswan_sells(today, accts, cfg, conn=eng.conn)
                for o in bs_sells:
                    st = eng.state[o.strategy_id]
                    st["pending"] = [p for p in st["pending"]
                                     if not (p["code"] == o.code and p["signal_date"] == today)]
                    d = o.to_dict(); d["_defer"] = 0
                    st["pending"].append(d)
                    eng.save_account(accts[o.strategy_id])
                    orders.append(o)
                for sid, code, ev in warns:
                    t, c = notify.build_alert(f"🟡持仓预警 {notify.strategy_cn(sid)} {util.bare(code)}:{'/'.join(ev[:2])},请人工研判")
                    notify.push(t, c, "alert", cfg)
            except Exception as e:
                log.warning("黑天鹅扫描失败(降级):%s", e)
        # 6 明日操作(按策略分条)
        by_sid = {}
        for o in orders:
            by_sid.setdefault(o.strategy_id, []).append(o)
        for sid, os_ in by_sid.items():
            items = _render_plan_items(eng, ctx, sid, os_)
            t, c = notify.build_op_message(sid, today, items)
            notify.push(t, c, "op", cfg)
        # 7 心跳(含市场分)
        last = eng.conn.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
        acted = set(by_sid.keys())
        idle = [notify.strategy_cn(s) for s in eng.enabled_strategies() if s not in acted]
        note = ("今日有操作见上条;" if acted else "") + ("无操作策略:" + "、".join(idle) if idle else "全部策略今日有操作")
        if mkt_score is not None and mkt_score < 0:
            note += f" | ⚠消息面市场分{mkt_score}(已降敞口)"
        t, c = notify.build_heartbeat(today, last, note)
        notify.push(t, c, "heartbeat", cfg)
        # 刷新静态看板(国内可达,零依赖)
        try:
            import report_html
            report_html.generate()
        except Exception as e:
            log.warning("静态看板生成失败(不阻断):%s", e)
            try:                                  # 卡B:看板停更也要告警,否则无人知晓
                t, c = notify.build_alert(f"🔴 看板生成失败:{e};Pages 可能停更,请检查 report_html")
                notify.push(t, c, "alert", cfg)
            except Exception:
                pass
        log.info("run_daily 完成 %s:回报%d 计划%d 市场分%s", today, len(reports), len(orders), mkt_score)
    finally:
        eng.close()
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="指定日期(测试用)YYYY-MM-DD")
    ap.add_argument("--only", help="仅运行指定策略,逗号分隔")
    args = ap.parse_args(argv)
    only = args.only.split(",") if args.only else None
    return run(args.date, only)


if __name__ == "__main__":
    sys.exit(main())
