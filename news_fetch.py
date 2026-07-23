# -*- coding: utf-8 -*-
"""本地(中国)新闻快照生产者。

实时抓取快讯写 state/news_flash.json,供云端海外 Runner 兜底显示"重点新闻"。
原理:云端 GitHub Actions(海外 Runner)对国内新闻源(新浪feed/东财快讯/央视)常不可达,
与 baostock/东财行情同理;而本地(中国)环境可正常抓取。故让"可达的本地环境"生产新闻,
用 Git 仓库当运输层(随 commit 跨地域送达云端),看板在任何网络下都有新闻。

运行: python news_fetch.py
建议:本地定时任务/WorkBuddy自动化 每日北京时间 16:30 前运行(早于云端 17:40 触发),
      并提交推送 state/news_flash.json,使当日快照先于云端看板生成到位。
"""
import logging

import news_adapter as na

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    na.ensure()
    n = na.refresh_news_cache()
    print(f"[news_fetch] 已刷新新闻快照: {n} 条 -> state/news_flash.json")
