# 云端 DB 体检报告

## 库内全部表
- daily_bar, dividend, fundamental, index_members, macro_indicator, news_raw, news_signal, security, stock_annual, trade_calendar

## 表规模
- daily_bar: 602185 行
- dividend: 5022 行
- fundamental: 5171 行
- index_members: 300 行
- macro_indicator: 5197 行
- news_raw: 273 行
- news_signal: 16 行
- security: 315 行
- stock_annual: 2955 行
- trade_calendar: 2184 行

## daily_bar 覆盖
- 日期 2018-01-02 ~ 2026-07-27,distinct code 315

## fundamental 覆盖(关键!)
- 日期 2005-04-08 ~ 2026-07-21,distinct code 1
- 按年 distinct code:
  - 2005: 1 只
  - 2006: 1 只
  - 2007: 1 只
  - 2008: 1 只
  - 2009: 1 只
  - 2010: 1 只
  - 2011: 1 只
  - 2012: 1 只
  - 2013: 1 只
  - 2014: 1 只
  - 2015: 1 只
  - 2016: 1 只
  - 2017: 1 只
  - 2018: 1 只
  - 2019: 1 只
  - 2020: 1 只
  - 2021: 1 只
  - 2022: 1 只
  - 2023: 1 只
  - 2024: 1 只
  - 2025: 1 只
  - 2026: 1 只
- 最新快照 1 行: pe非空 1, market_cap>0 0, dividend_yield>0 0

## index_members 池
- sh000300: 300 条

## 模拟选股(s26_microcap@v1 @ 2023-01-04 与 2025-06-04)
- 2023-01-04: target 0 只; empty_reason=无满足股息率/分红/ROE门槛标的
- 2025-06-04: target 0 只; empty_reason=无满足股息率/分红/ROE门槛标的

### 选股诊断日志
    s26_microcap@v1 候选筛选: 池226 无基本面226 股息率0 分红年数0 ROE0 数据不足0 → 候选0
    s26_microcap@v1 候选筛选: 池240 无基本面240 股息率0 分红年数0 ROE0 数据不足0 → 候选0
