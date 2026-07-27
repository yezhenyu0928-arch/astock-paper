# -*- coding: utf-8 -*-
"""新闻更新专用任务(2026-07-27 新增)。

每天 2 次(含周末)拉取快讯 + 市场/行业情绪扫描入库。
不推送任何操作,只更新消息面状态,供 run_daily 的风控降敞口与策略叠加使用。

用法: python run_news.py [--date YYYY-MM-DD]
"""
import sys
import logging
import argparse

import conf
import util
from db import get_conn

log = logging.getLogger("run_news")


def run(date=None):
    cfg = conf.load_config()
    today = util.to_date_str(date) if date else util.today_str()
    conn = get_conn()
    try:
        # 1 拉取快讯并入库(供持仓/候选新闻预扫描读取)
        try:
            import news_adapter as na
            df = na.fetch_flash()
            n = na.store_news(df, conn=conn) if df is not None else 0
            log.info("快讯入库 %d 条 @ %s", n, today)
        except Exception as e:
            log.warning("快讯拉取失败(降级):%s", e)
        # 2 市场情绪扫描(供风控按分降敞口)
        try:
            import news_engine as ne
            score, _ = ne.scan_market(today, conn=conn)
            log.info("市场情绪分:%s @ %s", score, today)
        except Exception as e:
            log.warning("市场扫描失败(降级):%s", e)
        # 3 产业主题扫描(供策略叠加行业信号)
        try:
            import news_engine as ne
            res = ne.scan_industry_themes(today, conn=conn)
            if res and res.get("themes"):
                log.info("产业主题 %d 个 @ %s", len(res["themes"]), today)
        except Exception as e:
            log.warning("行业主题扫描失败(降级):%s", e)
        log.info("run_news 完成 %s", today)
    finally:
        conn.close()
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="指定日期(测试用)YYYY-MM-DD")
    a = ap.parse_args(argv)
    return run(a.date)


if __name__ == "__main__":
    sys.exit(main())
