# -*- coding: utf-8 -*-
"""盘中任务。两种模式(SPEC_NEWS N4 + 卡G):
- calibrate(北京09:35):按实时价校准今日待撮合订单的股数/金额,推送「开盘校准」,让人工跟单更贴近实际成交。
- scan(北京10:00/13:30/14:45):快讯黑天鹅扫描,只推送不落单。
mode=auto 时按当前北京时间自判(9点→calibrate,否则→scan)。运行预算<2分钟。
用法:python run_intraday.py [--mode auto|calibrate|scan] [--date YYYY-MM-DD]"""
import sys
import json
import logging
import argparse

import conf
import util
import notify
import trade_calendar as cal
from db import get_conn
from engine import Engine

log = logging.getLogger("run_intraday")

# 跟单价格带(与看板卡A一致):买入实时价 > 昨收×1.02 视为追高,提示减半/放弃
BUY_BAND_HIGH = 1.02


def run(date=None, mode="auto"):
    cfg = conf.load_config()
    reg = conf.load_registry()
    today = util.to_date_str(date) if date else util.today_str()
    if not cal.is_trade_day(today):
        log.info("非交易日 %s,退出", today)
        return 0
    if mode == "auto":
        mode = "calibrate" if util.now_cn().hour < 10 else "scan"
    if mode == "calibrate":
        return _calibrate(today, cfg, reg)
    return _scan(today, cfg, reg)


# ---------------- 开盘校准(卡G) ----------------
def _calibrate(today, cfg, reg):
    """读各策略今日待撮合 pending,按实时价重算股数/金额并推送。无 pending 静默退出。"""
    import data_adapter as da
    eng = Engine(cfg, reg)
    try:
        plans = []       # (sid, order_dict, account)
        for sid in eng.enabled_strategies():
            acct = eng.load_account(sid)
            for o in eng.state[sid].get("pending", []):
                plans.append((sid, o, acct))
        if not plans:
            log.info("开盘校准:今日无待撮合订单,静默退出")
            return 0

        ctx = eng.ctx(today)
        codes = list(dict.fromkeys(o["code"] for _, o, _ in plans))
        rt = {}
        try:
            rt = da.fetch_realtime(codes)
        except Exception as e:
            log.warning("实时行情整体失败(降级昨收):%s", e)

        items, any_live, record = [], False, {}
        for sid, o, acct in plans:
            code, side, weight = o["code"], o["side"], o.get("weight", 0)
            nm = ctx.name(code)
            prev = eng._prev_close(code, today)
            q = rt.get(code)
            if q and q.get("price", 0) > 0:
                any_live = True
                price, live = q["price"], True
                prev = q.get("prev_close") or prev     # 实时源的官方昨收更权威(与实时价同口径)
            else:
                price, live = (prev or 0), False
            chg = (price / prev - 1) if (prev and price) else 0.0
            is_sell = side == "sell" or weight == 0
            if is_sell:
                held = acct.positions.get(code)
                sh = held.shares if held else 0
                amt = sh * price
                head = f"卖出 {util.bare(code)} {nm} 全部{sh}股"
                warn = ""
            else:
                total = acct.total(eng._price_of(today))
                sh = util.floor100(total * weight / price) if price else 0
                amt = sh * price
                head = f"买入 {util.bare(code)} {nm} 约{sh}股 ≈{amt:.0f}元"
                warn = ("  ⚠已超跟单价格带(>昨收×1.02),建议减半或放弃,勿追高"
                        if (prev and price > prev * BUY_BAND_HIGH) else "")
            ptxt = (f"实时{util.r2(price)}({chg*100:+.1f}%)" if live else f"昨收{util.r2(price)}(实时不可得)")
            items.append(f"{head}  {ptxt}{warn}")
            record[f"{sid}|{code}|{side}"] = {"price": price, "prev": prev, "shares": sh,
                                              "amount": util.r2(amt), "live": live}

        title = f"【⏰开盘校准|今日操作】{today}"
        lines = [title, "(按约09:35实时价校准股数/金额;以开盘价附近、在价格带内跟单)"]
        lines += [f"{notify._circ(i)} {t}" for i, t in enumerate(items, 1)]
        if not any_live:
            lines.append("⚠ 实时价整体不可得,以上为昨收参考价,请开盘后自行按价格带把握")
        lines.append("→ 撮合以次日开盘价为准,本条仅帮助你判断当前跟单可行性")
        notify.push(title, "\n".join(lines), "op", cfg)

        try:                                      # 轻量落盘供次日复盘"校准价 vs 实际成交价"
            (conf.STATE_DIR / f"calibrate_{today}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        log.info("开盘校准完成 %s:%d 单,实时命中=%s", today, len(items), any_live)
    finally:
        eng.close()
    return 0


# ---------------- 盘中黑天鹅扫描(原逻辑) ----------------
def _scan(today, cfg, reg):
    if not (cfg.get("news_layer") or {}).get("enabled") or not (cfg.get("news_layer") or {}).get("intraday_scan"):
        log.info("盘中扫描未启用,退出")
        return 0
    try:
        import news_adapter as na
        import news_engine as ne
    except Exception as e:
        log.warning("消息模块不可用:%s", e)
        return 0

    conn = get_conn()
    try:
        na.store_news(na.fetch_flash(), conn=conn)
        score, ev = ne.scan_market(today, conn=conn)
        if score <= -2:
            t, c = notify.build_alert(f"🔴盘中市场级重大负面(分{score}):{';'.join(ev[:2])},今日主流程将冻结开仓")
            notify.push(t, c, "alert", cfg)
        eng = Engine(cfg, reg, conn=conn)
        accts = {s: eng.load_account(s) for s in eng.enabled_strategies()}
        holdings = set()
        for a in accts.values():
            holdings |= set(a.positions.keys())
        flags = ne.scan_holdings(today, holdings, conn=conn)
        for code, (sc, evd) in flags.items():
            icon = "🔴黑天鹅" if sc == -2 else "🟡警示"
            act = "建议立即人工减仓(盘中不自动落单,系统次日按你回填对齐)" if sc == -2 else "请人工研判"
            t, c = notify.build_alert(f"{icon} 持仓 {util.bare(code)}:{'/'.join(evd[:2])},{act}")
            notify.push(t, c, "alert", cfg)
        log.info("盘中扫描完成 %s:市场分%s 持仓命中%d", today, score, len(flags))
    finally:
        conn.close()
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "calibrate", "scan"], default="auto")
    ap.add_argument("--date", help="指定日期(测试用)YYYY-MM-DD")
    args = ap.parse_args(argv)
    return run(args.date, args.mode)


if __name__ == "__main__":
    sys.exit(main())
