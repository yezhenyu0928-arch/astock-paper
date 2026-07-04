# -*- coding: utf-8 -*-
"""盘中增量扫描(SPEC_NEWS N4)。北京 10:00/13:30/14:45 三次。
只推送不落单:扫快讯→持仓黑天鹅即时🔴/警示🟡;市场级重大负面写 news_signal(当日17:40主流程读到后降敞口)。
运行预算<2分钟。用法:python run_intraday.py"""
import sys
import logging

import conf
import util
import notify
from db import get_conn
from engine import Engine

log = logging.getLogger("run_intraday")


def run(date=None):
    cfg = conf.load_config()
    reg = conf.load_registry()
    if not (cfg.get("news_layer") or {}).get("enabled") or not (cfg.get("news_layer") or {}).get("intraday_scan"):
        log.info("盘中扫描未启用,退出")
        return 0
    today = util.to_date_str(date) if date else util.today_str()
    import trade_calendar as cal
    if not cal.is_trade_day(today):
        return 0

    try:
        import news_adapter as na
        import news_engine as ne
    except Exception as e:
        log.warning("消息模块不可用:%s", e)
        return 0

    conn = get_conn()
    try:
        # 市场级快讯 → 写信号(供主流程降敞口)
        na.store_news(na.fetch_flash(), conn=conn)
        score, ev = ne.scan_market(today, conn=conn)
        if score <= -2:
            t, c = notify.build_alert(f"🔴盘中市场级重大负面(分{score}):{';'.join(ev[:2])},今日主流程将冻结开仓")
            notify.push(t, c, "alert", cfg)
        # 持仓黑天鹅/警示(仅当前持仓,快)
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sys.exit(run())
