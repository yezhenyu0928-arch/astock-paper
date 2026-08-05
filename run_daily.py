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
# 沪深300成分(~300只)首次回填(从2018起)每只约3秒,全量约15分钟;
# 故放宽到 900s 让单次运行尽量填满个股日线,之后增量更新极快不再触顶。
# 注意:此超时只包裹 update_all/update_daily,引擎/消息面/看板仍在 30min 限额内完成。
_DATA_TIMEOUT = 900  # 15 分钟:个股日线回填(首次从2018起约15分钟),超时仅截断"未更部分"
_FUND_TIMEOUT = 600   # 10 分钟:基本面(300只 sequential)独立超时,超时才截断,绝不跳过


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
    """启用的个股策略所需的日线更新范围——从 registry 动态推导,免手工登记。

    ⚠ 两次踩坑史(务必别再改回白名单写法):
    ① 2026-07 一次坑:写死 @v1 ID 判断,config 切到 v2 后全部 false → 返回空集。
    ② 2026-07 二次坑:改成"策略前缀白名单"仍需人工登记;s26/s27/s29/s32/s37/s42/s53
       一批上线后无人登记 → need_stock 恒为 False → update_daily 永不调用 →
       个股日线停更在最后一次人工回填 → 当前交易日候选池=0 → 全部策略 0 交易、
       微信零推送(即用户反馈的"微信推送一直没有任何策略产生交易")。
    根治:不再维护任何名单,直接读 registry:
      universe == 'dynamic'      → 个股策略,池由 params.pool_index 决定(默认 mainboard)
      universe == 'index:<code>' → 个股策略,池为该指数成分
      universe == [ETF代码列表]   → 纯ETF策略,不需要个股日线
    池取并集并按覆盖面择大:all_a(全A) > mainboard(主板流动性池) > sh000300。
    """
    enabled = {s for s, on in cfg.get("strategies", {}).items() if on}
    R = reg.get("strategies", reg) if isinstance(reg, dict) else {}

    pools = set()          # 需要的 index_members.index_code 集合
    stock_sids = []
    for sid in sorted(enabled):
        d = R.get(sid) or {}
        if not isinstance(d, dict):
            continue
        uni = d.get("universe")
        if isinstance(uni, (list, tuple)):      # 显式 ETF 列表 → 非个股策略
            continue
        uni = str(uni or "").strip().lower()
        if uni == "dynamic":
            params = d.get("params") or {}
            pools.add(str(params.get("pool_index") or "mainboard"))
            stock_sids.append(sid)
        elif uni.startswith("index:"):
            pools.add(uni.split(":", 1)[1])
            stock_sids.append(sid)

    if not pools:
        log.info("无启用的个股策略,跳过个股日线更新")
        return set()

    # 覆盖面择大:只要有策略要全A,就一次性拉全A(它是其余池的超集)
    if "all_a" in pools:
        order = ["all_a"]
    else:
        order = sorted(pools, key=lambda p: 0 if p == "mainboard" else 1)

    codes = set()
    used = []
    for pool in order:
        rows = conn.execute(
            "SELECT code FROM index_members WHERE index_code=?", (pool,)).fetchall()
        if rows:
            codes |= {r[0] for r in rows}
            used.append(f"{pool}({len(rows)})")
    if not codes:      # 兜底:池表未回填时退回沪深300,保证永不返回空集(旧"0交易"坑)
        rows = conn.execute(
            "SELECT code FROM index_members WHERE index_code='sh000300'").fetchall()
        codes |= {r[0] for r in rows}
        used.append(f"sh000300兜底({len(rows)})")

    # 剔除 sh920 段(沪市新股/北交所转板, 2026-08-05): 332 只全A池股票数据源普遍拉不到
    # (腾讯/baostock/yfinance 均无), 且不在任何策略选股池(mainboard/sh000300 主板前缀无 920)。
    # 逐只失败重试3次严重拖慢 daily(是 45 分钟超时的主因之一)。
    codes = {c for c in codes if not (c.startswith("sh920") or c.startswith("sz920"))}

    log.info("个股策略 %d 只(%s),日线更新范围=%d 只,来源池=%s",
             len(stock_sids), ",".join(stock_sids), len(codes), "+".join(used))
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


def _annual_coverage(conn):
    """沪深300成分中 stock_annual 已覆盖比例(0..1),用于判断是否需要补年报ROE。"""
    try:
        total = conn.execute(
            "SELECT count(*) FROM index_members WHERE index_code='sh000300'").fetchone()[0] or 1
        have = conn.execute(
            "SELECT count(DISTINCT code) FROM stock_annual WHERE code IN "
            "(SELECT code FROM index_members WHERE index_code='sh000300')").fetchone()[0] or 0
        return have / total
    except Exception:
        return 0.0


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
        stock_codes = _stock_universe(cfg, reg, conn)
        flag, timer = _timeout_guard(_DATA_TIMEOUT)
        try:
            data.update_all(cfg, reg, with_members=True, _timeout_check=_check_timeout, _timeout_flag=flag)
            _check_timeout(flag)
            # —— 个股日线:先「截面秒补当日」,再「逐只补历史缺口」 ——
            # 顺序很关键(2026-07 修复):策略选股只硬依赖「当日 bar 存在」+「近251日≥200条」,
            # 而逐只 update_daily 拉全A(5000+只)要约2.3小时,在 _DATA_TIMEOUT 内必被截断,
            # 且高并发会触发腾讯限流导致整批失败(历史上就是这样把当日数据拉残的:
            # 07-27 仅4532行、07-28/29 仅2333行,应为5084行)。
            # 现在改为:截面接口约13次请求、20秒补齐当日(决定今天能否出信号),
            # 历史缺口再用逐只增量慢慢补(补不完也不影响当天出单)。
            if stock_codes:
                try:
                    data.update_daily_snapshot(sorted(stock_codes), conn=conn, date=today)
                except Exception as e:
                    log.warning("当日截面补数失败(降级为逐只增量):%s", e)
                try:
                    data.update_daily(sorted(stock_codes), conn=conn, timeout_flag=flag, timeout_check=_check_timeout)
                    _check_timeout(flag)
                except TimeoutError:
                    log.warning("个股历史日线回填超时(当日数据已由截面补齐,不影响出单),继续")
                    flag["expired"] = False   # 解除标记,避免影响后续步骤
        except TimeoutError:
            # _check_timeout(flag) 在 update_all 之后触发(整段超时)
            log.warning("数据更新超时(降级继续):_check_timeout 触发")
            flag["expired"] = False
        except Exception as e:
            # 卡B:任何非预期异常(网络/接口/DB 写入)均降级继续,绝不因单次抓取抖动让整轮任务失败。
            # 原 try/finally 无 except,异常会冒泡使 GitHub Actions 整轮标红、微信只收到泛化"任务失败"。
            log.warning("数据更新异常(降级继续,使用已有缓存数据):%s", e)
            log.exception("数据更新异常详情")
        finally:
            timer.cancel()
        # —— 证券信息 + 基本面(独立于日线回填超时,策略选股硬依赖,必须跑) ——
        # 旧逻辑把这部分放在日线回填的同一超时 try 内:日线回填一超时就连带跳过基本面,
        # 导致策略有股价无基本面(股息率/市值/ROE)→ 候选池空 → 0 交易(本期二次修复)。
        if stock_codes:
            f2, t2 = _timeout_guard(_FUND_TIMEOUT)
            try:
                data.update_security(stock_codes, conn=conn)
                fund_ok = True
                try:
                    import fundamental as F
                    F.update_stock_fundamental(sorted(stock_codes), conn=conn,
                                              _timeout_flag=f2, _timeout_check=_check_timeout)
                    # 年报ROE补齐:覆盖不足80%时随时补(修复"仅month<=5才跑导致7月后永不补、候选池空"),
                    # 或在年报季(1-6月)例行刷新;其余时间跳过以节省API额度
                    if cfg.get("strategies", {}).get("s1_dividend@v2"):
                        cov = _annual_coverage(conn)
                        if cov < 0.8 or (1 <= util.now_cn().month <= 6):
                            F.update_annual_roe(sorted(stock_codes), conn=conn,
                                               _timeout_flag=f2, _timeout_check=_check_timeout)
                except TimeoutError:
                    log.warning("基本面更新超时(已部分更新),引擎用已有基本面继续")
                except Exception as e:
                    fund_ok = False
                    log.warning("基本面更新失败(不阻断):%s", e)
                finally:
                    t2.cancel()
                _fund_fail_track(not fund_ok, cfg)
            except Exception as e:
                log.warning("证券信息/基本面更新异常(不阻断):%s", e)
                _fund_fail_track(True, cfg)
        if cfg.get("strategies", {}).get("s5_grid@v1"):
            try:
                import fundamental as F
                F.update_index_pe("sh000300", conn=conn)
            except Exception as e:
                log.warning("指数PE更新失败:%s", e)
        # 诊断:基本面三表覆盖(海外 baostock 不可达时,fundamental 由腾讯兜底 pe/pb/mcap;
        # dividend/stock_annual 可能为空,策略层对已缺失项优雅降级,不应再全拒)
        try:
            fcnt = conn.execute("SELECT count(*) FROM fundamental").fetchone()[0]
            dcnt = conn.execute("SELECT count(*) FROM dividend").fetchone()[0]
            acnt = conn.execute("SELECT count(*) FROM stock_annual").fetchone()[0]
            log.info("基本面覆盖: fundamental=%d dividend=%d stock_annual=%d (个股策略依赖此三表)",
                     fcnt, dcnt, acnt)
        except Exception:
            pass
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
                # 卡B:质检异常改为降级继续(不再 return 1 让整轮任务失败)。
                # 系统为模拟盘,各策略对缺失数据已优雅降级;整轮静默失败(零推送)危害更大。
                # 仍大声告警,提示今日数据可能不全。
                t, c = notify.build_alert(f"🟡 数据质检异常(降级继续,请注意今日数据可能不全):{e}")
                notify.push(t, c, "alert", cfg)
                log.error("质检FAIL(降级继续):%s", e)
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
        # 5 之前: 持仓个股新闻预扫描(落库 news_signal(stock:{code})),
        # 供各策略 generate_orders 内的 guard_holdings 读取, 与步骤5.5 黑天鹅强卖双保险。
        if news_on:
            try:
                import news_engine as ne
                accts = {s: eng.load_account(s) for s in eng.enabled_strategies()}
                holdings = set()
                for a in accts.values():
                    holdings |= set(a.positions.keys())
                if holdings:
                    ne.scan_holdings(today, holdings, conn=eng.conn)
                    log.info("持仓新闻预扫描完成(供策略guard_holdings): %d 只", len(holdings))
            except Exception as e:
                log.warning("持仓新闻预扫描失败(降级): %s", e)
        # 5 之前补充: 候选池(沪深300)新闻预扫描, 供各策略 guard_candidates 纯读取
        # (消除此前在 generate_orders 内逐个抓外部新闻、75s 超时丢候选的根因)
        if news_on:
            try:
                from strategies import news_guard
                cand = list(ctx.members("sh000300", today))
                if cand:
                    news_guard.pre_scan_candidates(today, cand, conn=eng.conn, cfg=cfg)
            except Exception as e:
                log.warning("候选新闻预扫描失败(降级): %s", e)
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
        # 修复(2026-08-04): 心跳推送失败不应阻断后续看板生成。
        # PushPlus token 失效("服务端验证错误")时, push 抛 RuntimeError 冒泡 → 整轮失败 → 看板不更新。
        # 心跳是通知,看板是核心交付物; 心跳失败只降级记日志, 看板必须生成。
        try:
            notify.push(t, c, "heartbeat", cfg, smtp_fallback=False)
        except Exception as e:
            log.warning("心跳推送失败(降级继续,不影响看板):%s", e)
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
    try:
        return run(args.date, only)
    except Exception:
        # 顶层兜底:任何 run() 未预期崩溃,把真实堆栈推到微信(末25行),
        # 避免只收到 daily.yml 的泛化"任务失败"。NO_PUSH 时不发推送仅打印。
        import traceback as _tb
        tb_text = _tb.format_exc()
        tail = "\n".join(tb_text.strip().splitlines()[-25:])
        msg = "🔴 每日任务异常崩溃(非预期),堆栈末25行如下,请检查代码/数据:\n" + tail
        try:
            notify.push("【告警🔴】", msg, "alert", conf.load_config())
        except Exception:
            pass
        print(tb_text)
        return 1


if __name__ == "__main__":
    sys.exit(main())
