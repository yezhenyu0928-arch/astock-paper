# -*- coding: utf-8 -*-
"""静态HTML看板生成(国内可达方案·V2)。零外部依赖:数据内嵌、内联SVG画曲线、手机自适应。
产出 docs/index.html + docs/trades.html —— GitHub Pages 托管即可,或本地双击打开(无需翻墙/CDN)。
被 run_daily 收尾调用,也可单独:python report_html.py

V2 要点(见 docs/OPTIMIZE_V2.md 卡A):
- 盈红亏绿(A股习惯):涨/盈/买=红 --up,跌/亏/卖=绿 --down。
- 今日操作聚合置顶 + 一键复制 + 实时价渐进增强(腾讯行情,失败静默回昨收)。
- 实盘赛马总览(2026-07-31 起算)+ 各策略卡(介绍/因子权重表/可展开实盘曲线/最新持仓表)。
- 数据新鲜度横幅(按交易日落后数,红/黄两档)。
"""
import json
import glob
import os
import re
import csv
import html
from datetime import datetime, timedelta

import conf
import util
import backtest as bt
import trade_calendar as cal

# ---------------- 常量 ----------------
LIVE_START = "2026-07-31"          # 实盘赛马起算日(2026-07-31 重置:从今天起看前向收益,非回测;此前 nav 作为归一基准)
BENCH = "sh000300"                 # 实盘曲线基准:沪深300(库内 daily_bar 或指数)
BUY_BAND = (0.99, 1.02)            # 买入跟单价格带:参考价×[0.99, 1.02]

# 策略介绍字典(卡A A5;S4 如实更名;新策略在此追加,未登记 sid 用兜底文案)
STRAT_META = {
    # ===== 2026-07-28 当前参赛阵容(6只, config.yaml strategies 均置 true) =====
    "s26_microcap@v1": {
        "name": "微盘规模因子", "risk": "★★★★☆ 中高", "fit": "≥10万",
        "tagline": "复现规模因子（微盘溢价）：以市值规模(cap)为主导(0.50)+动量增强(0.20)+低波(0.05)+ROE(0.10)，月频10只，小盘段分散持仓吃规模溢价。主板硬约束。",
        "factors": [("规模因子(市值, cap)", "50%"), ("动量(12-1月)", "20%"), ("ROE质量排名", "10%"),
                    ("低波动排名(250日)", "5%"), ("估值分位(valuation)", "10%"), ("深度价值倾斜(value)", "5%"),
                    ("持仓控制", "月频10只·单行业≤3·低波后50%·regime防御关闭"),
                    ("风险过滤", "跟踪止损25%")],
        "rebalance": "每月最后交易日(月频)调仓 · 持10只 · 池=主板小盘段"},
    "s27_dividend_lowvol@v1": {
        "name": "红利低波", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "复现红利低波：股息率(0.40)+低波动(0.25)主导+ROE质量(0.15)+估值(0.10)+深度价值(0.10)，大盘段10只，纯防御型。主板硬约束。",
        "factors": [("股息率排名", "40%"), ("低波动排名(250日)", "25%"), ("ROE质量排名", "15%"),
                    ("估值分位(valuation)", "10%"), ("深度价值倾斜(value)", "10%"),
                    ("入选门槛", "股息率≥3% + 连续3年分红 + 连续3年ROE>8%"),
                    ("持仓控制", "月频10只·单行业≤3·低波后40%"),
                    ("风险过滤", "跟踪止损15%")],
        "rebalance": "每月最后交易日(月频)调仓 · 持10只 · 池=主板大盘段"},
    "s29_smallcap_select@v1": {
        "name": "国信小盘精选", "risk": "★★★★☆ 中高", "fit": "≥10万",
        "tagline": "复现国信《聚焦小盘股》：小市值池+复合因子(规模cap 0.45主导+估值0.20+反转/价值0.15+动量0.15+低波0.05)，月频10只。主板硬约束。",
        "factors": [("规模因子(市值, cap)", "45%"), ("估值分位(valuation)", "20%"), ("深度价值倾斜(value)", "15%"),
                    ("动量(12-1月)", "15%"), ("低波动排名(250日)", "5%"),
                    ("持仓控制", "月频10只·单行业≤3·低波后75%·regime防御关闭"),
                    ("风险过滤", "跟踪止损20%")],
        "rebalance": "每月最后交易日(月频)调仓 · 持10只 · 池=主板小盘段"},
    "s32_roe_quality@v1": {
        "name": "国信ROE质量", "risk": "★★★★☆ 中高", "fit": "≥10万",
        "tagline": "复现国信《基于ROE高质量选股》：ROE质量(0.50)主导+估值(0.15)+深度价值(0.15)+动量(0.15)+低波(0.05)，全市场段10只，质量因子溢价。主板硬约束。",
        "factors": [("ROE质量排名", "50%"), ("估值分位(valuation)", "15%"), ("深度价值倾斜(value)", "15%"),
                    ("动量(12-1月)", "15%"), ("低波动排名(250日)", "5%"),
                    ("入选门槛", "ROE≥8%"),
                    ("持仓控制", "月频10只·单行业≤3·低波后75%·regime防御关闭"),
                    ("风险过滤", "跟踪止损20%")],
        "rebalance": "每月最后交易日(月频)调仓 · 持10只 · 池=主板全段"},
    "s37_earnings_accel@v1": {
        "name": "国信盈利加速", "risk": "★★★★☆ 中高", "fit": "≥10万",
        "tagline": "复现国信《超预期投资全攻略》：净利润同比加速度(growth/accel≈盈余惊喜,0.30)主导+动量(0.15)+ROE质量(0.15)+估值(0.15)+规模(0.10)+价值(0.10)+低波(0.05)，小盘段8只。主板硬约束。",
        "factors": [("盈利加速度(growth accel)", "30%"), ("动量(12-1月)", "15%"), ("ROE质量排名", "15%"),
                    ("估值分位(valuation)", "15%"), ("规模因子(cap)", "10%"), ("深度价值倾斜(value)", "10%"),
                    ("低波动排名(250日)", "5%"), ("入选门槛", "连续3年ROE>8%"),
                    ("持仓控制", "月频8只·单行业≤2·regime防御关闭"),
                    ("风险过滤", "跟踪止损20%")],
        "rebalance": "每月最后交易日(月频)调仓 · 持8只 · 池=主板小盘段"},
    "s42_sue_enriched@v1": {
        "name": "国信SUE+52周新高强化", "risk": "★★★★☆ 中高", "fit": "≥10万",
        "tagline": "冲刺20%达标骨架：在国信小盘精选(s29)上叠加 SUE标准化预期外盈利(0.10)+52周新高距离(high52,0.10)，保留规模cap(0.30)主导+估值(0.15)+价值(0.15)+动量(0.20)+低波(0.05)。月频10只。主板硬约束。全窗口年化16.6%（2024+样本外23.2%）。",
        "factors": [("规模因子(cap)", "30%"), ("动量(12-1月)", "20%"), ("估值分位(valuation)", "15%"),
                    ("深度价值倾斜(value)", "15%"), ("SUE标准化预期外盈利(sue)", "10%"), ("52周新高距离(high52)", "10%"),
                    ("低波动排名(250日)", "5%"), ("ROE质量排名", "5%"),
                    ("持仓控制", "月频10只·单行业≤3·低波后75%·regime防御关闭"),
                    ("风险过滤", "跟踪止损20%")],
        "rebalance": "每月最后交易日(月频)调仓 · 持10只 · 池=主板小盘段"},
    # ===== 2026-07-28 全A股冲刺(含科创/创业/北交) 候选 =====
    "s53_all_a_momentum_smallcap@v1": {
        "name": "全A动量小盘(含科创/创业/北交)", "risk": "★★★★★ 高", "fit": "≥10万",
        "tagline": "全A股(主板+科创+创业+北交)吃规模溢价与52周新高突破alpha:52周新高(high52,0.25)+动量(0.35)+小盘规模(cap,0.20)主导,月频12只。不依赖缺失的SUE(仅主板600有profit_q)。",
        "factors": [("52周新高距离(high52)", "25%"), ("动量(12-1月)", "35%"), ("规模因子(cap,小盘)", "20%"),
                    ("低波动排名", "5%"), ("估值分位", "5%"), ("ROE质量排名", "5%"), ("深度价值倾斜", "5%"),
                    ("持仓控制", "月频12只·单行业≤2·低波后55%·温和regime降仓"),
                    ("风险过滤", "跟踪止损18%")],
        "rebalance": "每月最后交易日(月频)调仓 · 持12只 · 池=全A小盘段"},
    "s54_all_a_industry_mom@v1": {
        "name": "全A行业动量(含科创/创业/北交)", "risk": "★★★★★ 高", "fit": "≥10万",
        "tagline": "全A股行业动量轮动:行业内个股动量中位数(行业相对强度)0.30+个股动量0.25+小盘规模0.10,月频12只。行业分散(单行业≤3)吃板块轮动。",
        "factors": [("行业动量(ind_mom)", "30%"), ("动量(12-1月)", "25%"), ("规模因子(cap,小盘)", "10%"),
                    ("52周新高距离(high52)", "10%"), ("低波动排名", "5%"), ("ROE质量排名", "5%"), ("估值分位", "5%"),
                    ("持仓控制", "月频12只·单行业≤3·低波后55%·温和regime降仓"),
                    ("风险过滤", "跟踪止损18%")],
        "rebalance": "每月最后交易日(月频)调仓 · 持12只 · 池=全A小盘段"},
    "s55_all_a_value_quality@v1": {
        "name": "全A价值质量(含科创/创业/北交)", "risk": "★★★★☆ 中高", "fit": "≥10万",
        "tagline": "全A股价值质量防御型:深度价值(0.20)+ROE质量(0.15)+动量(0.30)+低波(0.10)+小盘规模(0.10),月频15只。更抗回撤。",
        "factors": [("深度价值倾斜(value)", "20%"), ("ROE质量排名", "15%"), ("动量(12-1月)", "30%"),
                    ("低波动排名", "10%"), ("规模因子(cap,小盘)", "10%"), ("估值分位(valuation)", "15%"),
                    ("52周新高距离(high52)", "5%"), ("持仓控制", "月频15只·单行业≤2·低波后55%·温和regime降仓"),
                    ("风险过滤", "跟踪止损18%")],
        "rebalance": "每月最后交易日(月频)调仓 · 持15只 · 池=全A小盘段"},
    "s2_etf@v1": {
        "name": "ETF动量轮动", "risk": "★★☆☆☆ 中低", "fit": "≥1万",
        "tagline": "每周持有近期最强的一只宽基/商品ETF，市场整体走弱时自动切入国债ETF避险。macro_score调节仓位：紧缩期60%、扩张期满仓。",
        "factors": [("20日收益率排名", "50%"), ("60日收益率排名", "50%"),
                    ("绝对动量门槛", "最强者20日收益<0 → 全仓切国债ETF"),
                    ("宏观调节(macro_score)", "紧缩降仓60%/扩张满仓+M2数据附操作理由")],
        "rebalance": "每周最后交易日 · 持1只 · 池=沪深300/中证500/红利低波/黄金/纳指/国债 6只ETF"},
    "s2_etf@v3": {
        "name": "ETF动量轮动v3【10日动量·周频】", "risk": "★★★☆☆ 中", "fit": "≥1万",
        "tagline": "每周买入近10日涨幅最强的1只ETF(宽基/商品/跨境池)，最强者10日收益<0则全仓切国债ETF避险。回测(2022→2026-07)真实年化仅+6.4%/回撤12.9%(此前声明41.5%为虚高，系不同代码路径所致)，未达用户'年化>25%保留'线，已于2026-07-27下线。",
        "factors": [("10日收益率排名", "100%"),
                    ("绝对动量门槛", "最强者10日收益<0 → 全仓国债ETF避险"),
                    ("宏观调节(macro_score)", "紧缩降仓/扩张满仓")],
        "rebalance": "每周最后交易日(周频) · 持1只 · 池=宽基/商品/跨境ETF"},
    "s2_etf@v4": {
        "name": "ETF动量轮动v4【10+20日双动量·周频】", "risk": "★★★☆☆ 中", "fit": "≥1万",
        "tagline": "10日与20日动量各半加权选最强1只ETF，双周期确认降低单周期噪音；最强者动量<0切国债ETF。回测(2022→2026-07)真实年化仅+6.5%/回撤14.7%(此前声明36.6%为虚高)，未达用户'年化>25%保留'线，已于2026-07-27下线。",
        "factors": [("10日收益率排名", "50%"), ("20日收益率排名", "50%"),
                    ("绝对动量门槛", "最强者动量<0 → 全仓国债ETF避险"),
                    ("宏观调节(macro_score)", "紧缩降仓/扩张满仓")],
        "rebalance": "每周最后交易日(周频) · 持1只 · 池=宽基/商品/跨境ETF"},
    # ==== 2026-07-26 参考文章因子重建 s20-s23(国信金工/开源金工/中信建投公众号因子) ====
    "s20_steady_quality@v1": {
        "name": "稳健质量精选·大盘【月频】", "risk": "★★☆☆☆ 中低", "fit": "≥10万",
        "tagline": "复刻国信金工'稳健精选'思路：沪深300大盘段(市值前33%)用低波动+高股息+高ROE质量构建稳健票池，叠加动量增强，持8只高集中度，12-1月动量为正才买(趋势闸)。",
        "factors": [("股息率排名", "20%"), ("低波动排名(250日)", "20%"), ("ROE质量排名", "16%"),
                    ("动量(12-1月)", "30%"), ("估值分位", "8%"), ("深度价值倾斜", "6%"),
                    ("入选门槛", "股息率≥3% + 连续3年分红 + 连续3年ROE>10% + 低波后45% + 动量≥0"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75集中度"),
                    ("风险过滤", "跟踪止损12% + 单行业≤2只")],
        "rebalance": "每月最后交易日(月频)调仓 · 风控每日 · 持8只 · 池=沪深300大盘段(市值前33%)",
        "data_source": "因子来源:国信金工公众号《稳健精选组合》; 行情/估值/分红/ROE:baostock+腾讯快照"},
    "s21_smallcap_compound@v1": {
        "name": "小盘复合精选【月频】", "risk": "★★★★☆ 中高", "fit": "≥10万",
        "tagline": "复刻国信金工'小盘精选'思路：沪深300内中小盘段(市值后45%)吃规模溢价，小市值倾斜+价值+动量复合打分，持8只，日频调仓捕捉轮动。",
        "factors": [("小市值规模溢价(cap)", "22%"), ("动量(12-1月)", "35%"), ("股息率排名", "12%"),
                    ("ROE质量排名", "12%"), ("估值分位", "8%"), ("低波动排名", "6%"), ("深度价值倾斜", "5%"),
                    ("入选门槛", "股息率≥2.5% + 连续3年分红 + 连续3年ROE>8% + 动量≥-5%(不深跌)"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75集中度"),
                    ("风险过滤", "跟踪止损14% + 单行业≤3只")],
        "rebalance": "每月最后交易日(月频)调仓 · 持8只 · 池=沪深300中小盘段(市值后45%)",
        "data_source": "因子来源:国信金工公众号《小盘精选组合》; 行情/估值/ROE:baostock+腾讯快照"},
    "s22_earnings_surprise@v1": {
        "name": "超预期精选【月频】", "risk": "★★★☆☆ 中", "fit": "≥10万",
        "tagline": "复刻国信金工'超预期精选'思路：以盈利同比高增(成长因子)为核心代理'业绩超预期'，要求12-1月动量为正(趋势确认)，行业龙头加分；实盘另叠加研报/公告超预期扫描，回测降级为成长因子。",
        "factors": [("成长(盈利同比,超预期代理)", "28%"), ("动量(12-1月)", "35%"), ("ROE质量排名", "14%"),
                    ("低波动排名", "12%"), ("个股行业地位(龙头)", "6%"), ("估值分位", "5%"),
                    ("入选门槛", "股息率≥2% + 连续2年分红 + 连续2年ROE>8% + 动量≥0"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75集中度"),
                    ("风险过滤", "跟踪止损14% + 单行业≤2只")],
        "rebalance": "每月最后交易日(月频)调仓 · 风控每日 · 持8只 · 池=沪深300",
        "data_source": "因子来源:国信金工公众号《超预期精选组合》; 财报同比:profit表; 实盘叠加:news_engine超预期扫描"},
    "s23_industry_momentum@v1": {
        "name": "行业动量龙头【月频】", "risk": "★★★☆☆ 中", "fit": "≥10万",
        "tagline": "复刻开源金工/中信建投'联合动量'思路：个股12-1月动量与行业地位(龙头)共振——只买强动量且在行业内市值/ROE靠前的票，行业分散(单行业≤2只)+个股集中(8只)，日频调仓。",
        "factors": [("动量(12-1月)", "40%"), ("个股行业地位(龙头)", "22%"), ("ROE质量排名", "12%"),
                    ("低波动排名", "10%"), ("估值分位", "10%"), ("成长(盈利同比)", "6%"),
                    ("入选门槛", "股息率≥2% + 连续2年分红 + 连续3年ROE>8% + 动量≥0(上行趋势)"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75集中度"),
                    ("风险过滤", "跟踪止损14% + 单行业≤2只")],
        "rebalance": "每月最后交易日(月频)调仓 · 持8只 · 池=沪深300",
        "data_source": "因子来源:开源金工《联合动量》/中信建投量化公众号; 行业:industry表; 行情/ROE:baostock+腾讯快照"},
    "s1_dividend@v1": {
        "name": "红利低波", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "买入高股息且股价波动小的大盘股并长期持有，靠分红+低回撤积累收益（同类指数近6年年化约13%）。",
        "factors": [("股息率排名", "50%"), ("低波动排名(250日)", "50%"),
                    ("入选门槛", "股息率≥4% + 连续3年现金分红 + 波动率位于池内最低30%")],
        "rebalance": "每月最后交易日 · 等权约6-10只 · 池=沪深300"},
    # ==== 2026-07-23 主板扩池+差异化 @v3 阵容(6策略各取不同市值段/分红门槛/因子权重/调仓频率)====
    "s1_dividend@v3": {
        "name": "红利价值·主板大盘【周频】", "risk": "★☆☆☆☆ 低", "fit": "≥10万",
        "tagline": "主板全市场(除创/科/北)大盘段(市值前33%)红利蓝筹:高股息+连续分红3年+ROE质量+低波+估值,动量确认上行,周频调仓。定位防御/收入型底仓。",
        "factors": [("股息率排名", "28%"), ("低波动排名(250日)", "18%"), ("ROE质量排名", "16%"),
                    ("估值分位(EP/BP)", "12%"), ("动量(12-1月,确认上行)", "15%"),
                    ("个股行业地位(industry)", "5%"), ("新闻语义分", "6%(新闻库空,暂为0)"),
                    ("入选门槛", "股息率≥3.5% + 连续3年分红 + 连续3年ROE>8% + 低波后50%"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75仓 + 账户回撤分层降险(4%/6%/8%→8/6/4成)"),
                    ("风险过滤", "跟踪止损12% + 单行业≤3只")],
        "rebalance": "每周最后交易日(周频) · 约12只 · 池=主板全市场·大盘段(市值前33%) · 连续分红3年",
        "data_source": "行情/估值/分红/ROE:baostock+腾讯快照; 成分股:index_members(mainboard主板池); 宏观regime:macro_indicator; 新闻:news_raw/news_signal"},
    "s15_core_allocation@v3": {
        "name": "核心配置·主板大盘【周频】", "risk": "★☆☆☆☆ 低", "fit": "≥10万",
        "tagline": "主板大盘段(市值前33%)质量核心配置:连续分红2年(比红利版松,纳入更多质量成长)+高ROE质量+动量,周频调仓,作为组合稳健核心仓位。",
        "factors": [("ROE质量排名", "22%"), ("动量(12-1月)", "22%"), ("股息率排名", "16%"),
                    ("低波动排名", "14%"), ("估值分位", "10%"), ("个股行业地位(industry)", "10%"),
                    ("新闻语义分", "6%(新闻库空,暂为0)"),
                    ("入选门槛", "股息率≥2% + 连续2年分红 + 连续3年ROE>8%"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75仓 + 账户回撤分层降险"),
                    ("风险过滤", "跟踪止损10% + 单行业≤2只")],
        "rebalance": "每周最后交易日(周频) · 约12只 · 池=主板大盘段(市值前33%) · 连续分红2年 · 质量+动量核心",
        "data_source": "行情/估值/分红/ROE:baostock+腾讯快照; 成分股:index_members(mainboard); 宏观regime:macro_indicator; 新闻:news_raw/news_signal"},
    "s8_checklist@v3": {
        "name": "全市场低波价值·主板【周频】", "risk": "★☆☆☆☆ 低", "fit": "≥10万",
        "tagline": "主板全市场(不限市值段)低波价值:不设分红门槛,以低波动+深度价值(低PE/PB)为主,ROE基本质量安全网,分散持15只压回撤,周频调仓。定位最低回撤防御档。",
        "factors": [("低波动排名(250日)", "38%"), ("估值分位(EP/BP)", "18%"), ("深度价值倾斜(value_tilt)", "12%"),
                    ("ROE质量排名", "12%"), ("动量(12-1月)", "10%"), ("个股行业地位(industry)", "5%"),
                    ("新闻语义分", "5%(新闻库空,暂为0)"),
                    ("入选门槛", "不设分红门槛 + 连续3年ROE>5%(安全网) + 低波后35%(收紧压回撤)"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75仓 + 账户回撤分层降险"),
                    ("风险过滤", "跟踪止损8%(最严) + 单行业≤3只 + 分散15只")],
        "rebalance": "每周最后交易日(周频) · 约15只 · 池=主板全市场(不限市值段) · 不设分红门槛 · 低波价值",
        "data_source": "行情/估值/分红/ROE:baostock+腾讯快照; 成分股:index_members(mainboard); 宏观regime:macro_indicator; 新闻:news_raw/news_signal"},
    "s4_smallcap@v3": {
        "name": "中小盘成长·主板【日频】", "risk": "★★★★☆ 中高", "fit": "≥10万",
        "tagline": "主板中小盘段(市值后45%)成长弹性:不设分红门槛、放开ROE硬门槛(仅作排名),偏小市值博规模溢价(cap_tilt)+半年动量轮动+成长因子,日频调仓捕捉小盘快轮动。收益弹性最高。",
        "factors": [("动量(半年,小盘快轮动)", "25%"), ("小市值规模溢价(cap_tilt)", "24%"), ("成长(盈利同比)", "15%"),
                    ("ROE质量排名", "12%"), ("低波动排名", "10%"), ("估值分位", "8%"),
                    ("新闻语义分", "6%(新闻库空,暂为0)"),
                    ("入选门槛", "不设分红门槛 + ROE不设硬门槛 + 半年动量≥-5%(不深跌)"),
                    ("宏观自适应", "regime good/mid/bad=1.0/0.85/0.6仓(弱市更谨慎) + 账户回撤分层降险"),
                    ("风险过滤", "跟踪止损15% + 单行业≤3只 + 分散15只")],
        "rebalance": "每日检查/调仓(日频) · 约15只 · 池=主板中小盘段(市值后45%) · 不设分红门槛 · 小盘成长弹性",
        "data_source": "行情/估值/ROE:baostock+腾讯快照; 成分股:index_members(mainboard); 宏观regime:macro_indicator; 新闻:news_raw/news_signal"},
    "s13_growth_quality_rotation@v3": {
        "name": "景气成长·主板中盘【日频】", "risk": "★★★☆☆ 中", "fit": "≥10万",
        "tagline": "主板中盘段(市值33%~70%)景气成长:不设分红门槛但保留ROE质量门槛,以动量+成长(盈利同比)+ROE质量为主,容忍较高波动,日频调仓吃景气趋势。中盘成长主战场。",
        "factors": [("动量(12-1月,趋势)", "30%"), ("成长(盈利同比)", "22%"), ("ROE质量排名", "16%"),
                    ("个股行业地位(industry)", "12%"), ("低波动排名", "8%"), ("估值分位", "6%"),
                    ("新闻语义分", "6%(新闻库空,暂为0)"),
                    ("入选门槛", "不设分红门槛 + 连续3年ROE>8%(成长要质量) + 动量≥0(上行趋势)"),
                    ("宏观自适应", "regime good/mid/bad=1.0/0.9/0.65仓 + 账户回撤分层降险"),
                    ("风险过滤", "跟踪止损13% + 单行业≤3只")],
        "rebalance": "每日检查/调仓(日频) · 约12只 · 池=主板中盘段(市值33%~70%) · 不设分红门槛 · 景气成长",
        "data_source": "行情/估值/ROE:baostock+腾讯快照; 成分股:index_members(mainboard); 宏观regime:macro_indicator; 新闻:news_raw/news_signal"},
    "s14_value_reversal_rotation@v3": {
        "name": "价值反转·主板中小盘【日频】", "risk": "★★★☆☆ 中", "fit": "≥10万",
        "tagline": "主板中小盘段(市值后70%)低估反转:不设分红门槛、ROE从宽(捡便宜),深度价值(低PE/PB)倾斜+逆动量(偏好近期超跌票),日频调仓捕捉反转,与成长版岔开。",
        "factors": [("深度价值倾斜(value_tilt)", "24%"), ("估值分位(EP/BP)", "22%"), ("低波动排名", "10%"),
                    ("ROE质量排名(弱)", "8%"), ("新闻语义分", "6%(新闻库空,暂为0)"),
                    ("逆动量(负权重,偏好超跌)", "-12%"),
                    ("入选门槛", "不设分红门槛 + ROE从宽 + 不设动量下限(允许买入近期走弱的超跌票)"),
                    ("宏观自适应", "regime good/mid/bad=1.0/0.9/0.65仓 + 账户回撤分层降险"),
                    ("风险过滤", "跟踪止损14% + 单行业≤3只 + 分散15只")],
        "rebalance": "每日检查/调仓(日频) · 约15只 · 池=主板中小盘段(市值后70%) · 深度价值+逆动量反转",
        "data_source": "行情/估值/ROE:baostock+腾讯快照; 成分股:index_members(mainboard); 宏观regime:macro_indicator; 新闻:news_raw/news_signal"},
    "s4_smallcap@v2": {
        "name": "红利质量多因子·小盘倾斜", "risk": "★★★★☆ 中", "fit": "≥5万",
        "tagline": "红利质量多因子底座(mf_core)叠加小市值规模溢价(cap_tilt):高股息+连续分红+ROE质量+低波+估值+新闻,动量确认上行趋势,偏配小市值股博规模溢价。",
        "factors": [("股息率排名", "16%"), ("低波动排名", "7%"), ("ROE质量排名", "15%"),
                    ("估值分位(EP/BP)", "9%"), ("新闻语义分", "7%(新闻库空,暂为0)"),
                    ("小市值规模溢价(cap_tilt)", "11%"), ("动量(12-1月,确认上行)", "35%"),
                    ("入选门槛", "股息率≥2.5% + 连续3年分红 + 连续3年ROE>8% + 动量不深跌"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75仓,市场弱仍留75%仓"),
                    ("风险过滤", "止损14% + 单行业≤3只")],
        "rebalance": "每月最后交易日 · 等权8只 · 池=沪深300成分",
        "data_source": "行情/估值/分红/ROE:baostock(daily_bar/stock_annual/dividend); 成分股:index_members(沪深300); 宏观regime:macro_indicator(PMI/社融/北向/融资余额); 新闻:news_raw/news_signal(表已建,历史回测区间待回灌,当前权重恒0)"},
    "s1_dividend@v2": {
        "name": "红利质量多因子·低波红利", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "红利质量多因子底座(mf_core)的稳健红利版:高股息+连续分红+ROE质量+低波+估值+行业地位+新闻,动量确认上行,低波过滤压回撤,适合稳健底仓。",
        "factors": [("股息率排名", "22%"), ("低波动排名", "10%"), ("ROE质量排名", "15%"),
                    ("估值分位", "10%"), ("新闻语义分", "10%(新闻库空,暂为0)"),
                    ("个股行业地位(industry)", "8%"), ("动量(12-1月,确认上行)", "35%"),
                    ("入选门槛", "股息率≥3.5% + 连续3年分红 + 连续3年ROE>8% + 低波后55%"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75仓"),
                    ("风险过滤", "止损12% + 单行业≤3只")],
        "rebalance": "每月最后交易日 · 等权10只 · 池=沪深300成分",
        "data_source": "行情/估值/分红/ROE:baostock(daily_bar/stock_annual/dividend); 成分股:index_members(沪深300); 宏观regime:macro_indicator; 新闻:news_raw/news_signal(历史回测区间待回灌,当前权重恒0)"},
    "s3_ma_trend@v1": {
        "name": "双均线趋势", "risk": "★★★☆☆ 中", "fit": "≥3万",
        "tagline": "20日均线上穿60日均线且放量时买入，跌破20日均线立即卖出。macro_score调节放量阈值：紧缩1.5x防假突破、扩张0.7x积极入场。",
        "factors": [("入场规则", "MA20上穿MA60 + 当日成交量>20日均量×阈值"),
                    ("出场规则", "收盘跌破MA20 清仓该票"), ("排序", "站上MA60幅度(强度)"),
                    ("宏观调节(macro_score)", "紧缩vol_mult×1.5/扩张×0.7+M2数据附买入理由")],
        "rebalance": "每日检查 · 最多持约6只(按资金自适应) · 池=沪深300成分"},
    "s4_smallcap@v1": {
        "name": "沪深300价值精选(小市值演示档)", "risk": "★★★★☆ 中高", "fit": "≥5万",
        "tagline": "在沪深300内选市值偏小、估值偏低、近期不追高的股票。注：受免费数据限制，当前池为沪深300，非真·小盘。",
        "factors": [("总市值(小优先)", "50%"), ("市净率PB(低优先)", "30%"), ("20日动量", "20%")],
        "rebalance": "每月最后交易日 · 等权约6只 · 池=沪深300(过滤后市值最小400只再打分)"},
    "s5_grid@v1": {
        "name": "大盘估值网格", "risk": "★☆☆☆☆ 低", "fit": "≥1万",
        "tagline": "只做沪深300ETF：估值便宜时越跌越买，贵时越涨越卖。macro_score调节步长(紧缩放宽/扩张收窄)+动态档数(±2档)。",
        "factors": [("PE十年分位择时", "<30%只买不卖 / >70%只卖不买"),
                    ("网格步长", "±2%基准 · macro_score调节±50%"),
                    ("总档数", "5档基准 · macro_score调节±2档"),
                    ("宏观调节(macro_score)", "步长+档数+M2数据")],
        "rebalance": "每日检查 · 标的=沪深300ETF(510300)"},
    "s6_sector@v1": {
        "name": "行业ETF轮动", "risk": "★★★☆☆ 中", "fit": "≥1万",
        "tagline": "每月持有近3-6月最强的行业ETF。macro_score调节仓位(紧缩50%/扩张满仓)+避险阈值(紧缩0收益就切/扩张-3%才切)。",
        "factors": [("60日动量排名", "40%"), ("120日动量排名", "40%"), ("60日低波动排名", "20%"),
                    ("绝对动量门槛", "最强者60日收益<阈值 → 全仓切国债ETF"),
                    ("宏观调节(macro_score)", "仓位调节+避险阈值+M2数据")],
        "rebalance": "每月最后交易日 · 持1只 · 池=券商/半导体/医药/消费/军工/新能源/酒/光伏/银行/国债"},
    "s7_track@v1": {
        "name": "赛道旗舰", "risk": "★★★★☆ 中高", "fit": "≥3万",
        "tagline": "选中1-2个有政策/景气主线的行业赛道，集中持有。综合市场regime判断仓位，风险市果断切国债避险。",
        "factors": [("60日行业动量", "50%"), ("120日行业动量", "加权"),
                    ("GLM产业/政策信号", "50% · 国家级利好+2/+1分，利空-1/-2分"),
                    ("市场regime判断", "强势98%仓/震荡80%仓/转弱50%仓/风险0%全切国债"),
                    ("绝对动量过滤", "短窗收益<0的赛道剔除，顺势不逆势"),
                    ("入选池", "券商/半导体/医药/消费/军工/新能源/酒/光伏/银行/国债")],
        "rebalance": "每月最后交易日 · 持1-2只 · 赛道集中"},
    "s8_checklist@v1": {
        "name": "红利质量多因子·低回撤", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "红利质量多因子底座(mf_core)的低回撤版(原R1-R9清单类已弃用):高股息+ROE质量+低波为主,动量适度,深度价值倾斜(value_tilt),分散持10只压回撤,定位稳健防御底仓。",
        "factors": [("股息率排名", "16%"), ("低波动排名", "20%"), ("ROE质量排名", "15%"),
                    ("估值分位", "10%"), ("新闻语义分", "5%(新闻库空,暂为0)"),
                    ("个股行业地位(industry)", "4%"), ("动量(12-1月,确认上行)", "30%"),
                    ("入选门槛", "股息率≥3% + 连续3年分红 + 连续3年ROE>8% + 深度价值倾斜"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75仓"),
                    ("风险过滤", "止损10% + 单行业≤3只 + 分散10只")],
        "rebalance": "每月最后交易日 · 等权10只 · 池=沪深300成分",
        "data_source": "行情/估值/分红/ROE:baostock(daily_bar/stock_annual/dividend); 成分股:index_members(沪深300); 宏观regime:macro_indicator; 新闻:news_raw/news_signal(历史回测区间待回灌,当前权重恒0)"},
    "s13_growth_quality_rotation@v2": {
        "name": "红利质量多因子·成长质量", "risk": "★★★☆☆ 中", "fit": "≥5万",
        "tagline": "红利质量多因子底座(mf_core)的成长质量版:在红利+ROE质量+低波+估值基础上叠加深度价值倾斜与动量确认,原 growth 因子因单日暴跌选高beta票、回撤超线已移除,现以质量+价值+动量实现稳健成长风格。",
        "factors": [("股息率排名", "18%"), ("低波动排名", "10%"), ("ROE质量排名", "18%"),
                    ("估值分位", "7%"), ("新闻语义分", "6%(新闻库空,暂为0)"),
                    ("价值倾斜(value_tilt)", "11%"), ("动量(12-1月,确认上行)", "35%"),
                    ("入选门槛", "股息率≥3% + 连续3年分红 + 连续3年ROE>8% + 动量不走弱"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75仓"),
                    ("风险过滤", "止损12% + 单行业≤3只")],
        "rebalance": "每月最后交易日 · 等权8只 · 池=沪深300成分",
        "data_source": "行情/估值/分红/ROE:baostock(daily_bar/stock_annual/dividend); 成分股:index_members(沪深300); 宏观regime:macro_indicator; 新闻:news_raw/news_signal(历史回测区间待回灌,当前权重恒0)"},
    "s14_value_reversal_rotation@v2": {
        "name": "红利质量多因子·价值反转", "risk": "★★★☆☆ 中", "fit": "≥5万",
        "tagline": "红利质量多因子底座(mf_core)的价值反转版:高股息+ROE质量+低波+深度价值倾斜,叠加个股行业地位(industry)与动量确认,偏配被错杀的优质价值股,收益弹性最高。",
        "factors": [("股息率排名", "18%"), ("低波动排名", "10%"), ("ROE质量排名", "16%"),
                    ("估值分位", "10%"), ("新闻语义分", "10%(新闻库空,暂为0)"),
                    ("个股行业地位(industry)", "8%"), ("价值倾斜(value_tilt)", "8%"), ("动量(12-1月,确认上行)", "35%"),
                    ("入选门槛", "股息率≥3% + 连续3年分红 + 连续3年ROE>8% + 深度价值倾斜"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75仓"),
                    ("风险过滤", "止损12% + 单行业≤3只")],
        "rebalance": "每月最后交易日 · 等权8只 · 池=沪深300成分",
        "data_source": "行情/估值/分红/ROE:baostock(daily_bar/stock_annual/dividend); 成分股:index_members(沪深300); 宏观regime:macro_indicator; 新闻:news_raw/news_signal(历史回测区间待回灌,当前权重恒0)"},
    "s15_core_allocation@v2": {
        "name": "红利质量多因子·核心配置", "risk": "★☆☆☆☆ 低", "fit": "≥3万",
        "tagline": "红利质量多因子底座(mf_core)的核心配置版:与 s8 同防御骨架(高股息+ROE质量+低波为主、动量适度),分散持10只,作为组合稳健核心仓位。",
        "factors": [("股息率排名", "16%"), ("低波动排名", "20%"), ("ROE质量排名", "15%"),
                    ("估值分位", "10%"), ("新闻语义分", "5%(新闻库空,暂为0)"),
                    ("个股行业地位(industry)", "4%"), ("动量(12-1月,确认上行)", "30%"),
                    ("入选门槛", "股息率≥3% + 连续3年分红 + 连续3年ROE>8% + 深度价值倾斜"),
                    ("宏观自适应", "regime good/mid/bad=1.0/1.0/0.75仓"),
                    ("风险过滤", "止损10% + 单行业≤3只 + 分散10只")],
        "rebalance": "每月最后交易日 · 等权10只 · 池=沪深300成分",
        "data_source": "行情/估值/分红/ROE:baostock(daily_bar/stock_annual/dividend); 成分股:index_members(沪深300); 宏观regime:macro_indicator; 新闻:news_raw/news_signal(历史回测区间待回灌,当前权重恒0)"},
}


_FREQ_LABEL = {"daily": "日频", "weekly": "周频", "monthly": "月频"}
_FREQ_FULL = {"daily": "日频 · 每个交易日检查/调仓",
              "weekly": "周频 · 每周最后交易日调仓",
              "monthly": "月频 · 每月最后交易日调仓"}
_REG_CACHE = {}


def _strategy_freq(sid):
    """从 registry 读取该策略 params.rebalance(或顶层 rebalance)频率, 返回 (short, full, raw)。
    看板据此渲染显式"频率"徽章, 保证与实际调仓频率一致(不依赖人工文案)。"""
    if not _REG_CACHE:
        try:
            _REG_CACHE["reg"] = conf.load_registry() or {}
        except Exception:
            _REG_CACHE["reg"] = {}
    reg = _REG_CACHE.get("reg", {})
    entry = reg.get(sid, {}) or {}
    params = entry.get("params", {}) or {}
    raw = params.get("rebalance") or entry.get("rebalance") or "monthly"
    return _FREQ_LABEL.get(raw, raw), _FREQ_FULL.get(raw, raw), raw


def _meta(sid):
    base = STRAT_META.get(sid, {
        "name": sid, "risk": "—", "fit": "—",
        "tagline": "（该策略暂无介绍文案）",
        "factors": [], "rebalance": "—",
        "data_source": "（未登记）"})
    # 注入 registry 派生的调仓频率(短标签+全描述), 供策略卡显式展示"频率"徽章
    short, full, raw = _strategy_freq(sid)
    return {**base, "_freq_short": short, "_freq_full": full, "_freq_raw": raw}


def _cn(sid):
    return _meta(sid)["name"]


# ---------------- 数据装载 ----------------
def _load_accounts():
    """加载 state/*.json 中的策略账户，仅保留 config.yaml strategies 当前置 true 的（已下线/归档策略
    的历史文件仍留在磁盘供回测/存档，只是不在看板赛马总览/策略卡/操作计划里展示）。
    同时补入 registry 中已注册但尚无 state 文件的新策略（占位，净值1.0）。"""
    try:
        enabled = {sid for sid, on in (conf.load_config().get("strategies") or {}).items() if on}
    except Exception:
        enabled = None   # config 读取异常时不过滤，保留原有全量展示(降级安全)
    out = {}
    for f in glob.glob(str(conf.STATE_DIR / "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
            sid = d.get("strategy_id")
            if sid and (enabled is None or sid in enabled):
                out[sid] = d
        except Exception:
            pass
    # 补入 registry 中已注册但无 state 的新策略（占位展示）
    try:
        reg = conf.load_registry()
        for sid, entry in reg.items():
            if enabled is not None and sid not in enabled:
                continue
            if sid not in out:
                out[sid] = {
                    "strategy_id": sid, "cash": 100000, "nav": 1.0, "nav_history": [],
                    "positions": {}, "pending": [], "frozen": False,
                    "init_capital": 100000, "_placeholder": True,
                }
    except Exception:
        pass
    return out


def _load_trade_log():
    """实盘成交流水 state/trade_log.csv(2026-07-31 起)。返回按 (sid,code) 分组的买入记录 + 全量行。"""
    path = conf.STATE_DIR / "trade_log.csv"
    rows = []
    if path.exists():
        try:
            with open(path, encoding="utf-8", newline="") as f:
                rows = [r for r in csv.DictReader(f)]
        except Exception:
            rows = []
    return rows


def _buy_info(sid, code, log_rows, fallback_date=""):
    """某策略某票最近一笔成交买入的 (日期, 理由)。无流水则回退持仓 buy_date + '—'。"""
    best = None
    for r in log_rows:
        if (r.get("strategy_id") == sid and r.get("code") == code
                and r.get("side") == "buy" and r.get("status") in ("filled", "cut_liquidity")):
            if best is None or r.get("trade_date", "") >= best.get("trade_date", ""):
                best = r
    if best:
        return best.get("trade_date", fallback_date), (best.get("reason", "") or "—")
    return fallback_date, "—"


def _backtest_summary(sid):
    """从 reports/ 读主回测(2022-今)的年化/回撤,按用户硬约束(年化≥5% 且 回撤≤5%)打达标/不达标标。
    兼容主回测报告({slug}.md)和五段回测报告({slug}_v3.md)。
    返回 (bt_line, verdict):bt_line 为纯文本(含达标徽章 emoji),看板两处渲染(一处 html.escape)。
    """
    slug = sid.replace("@", "_at_")
    bt_line, verdict = "", ""
    # 优先读主回测报告, 不存在则读五段回测报告(_v3后缀)
    for rp_name in (f"{slug}.md", f"{slug}_v3.md"):
        rp = conf.REPORTS_DIR / rp_name
        if rp.exists():
            text = rp.read_text(encoding="utf-8")
            # 主回测行: "- 累计25.7% 年化5.4% 回撤4.8% Calmar1.13 ..."
            m = re.search(r"主回测\([^)]*\)[^\n]*\n\s*-\s*累计[^\n]*?年化\s*([+-]?[\d.]+%)[^回]*回撤\s*([+-]?[\d.]+%)", text)
            if m:
                ann, dd = m.group(1), m.group(2)
                try:
                    ok = float(ann.rstrip("%")) >= 5.0 and float(dd.rstrip("%")) <= 5.0
                except Exception:
                    ok = False
                badge = "✅达标" if ok else "⚠️未达收益/回撤目标线"
                bt_line = f"年化{ann}·回撤{dd} {badge}"
                break
            # 兜底:老格式只有 年化收益均值 / 年化 行(无主回测段)
            m2 = re.search(r"年化收益均值:\s*([+\-\d.]+%)", text)
            if not m2:
                m2 = re.search(r"年化\s*([+\-\d.]+%)", text)
            if m2:
                bt_line = f"年化{m2.group(1)} (主回测段缺失)"
                break
    vp = conf.REPORTS_DIR / f"{slug}_validate.md"
    if vp.exists():
        m = re.search(r"## 结论:\*\*(.+?)\*\*", vp.read_text(encoding="utf-8"))
        if m:
            verdict = m.group(1).strip()
    return bt_line, verdict


def _verdict_badge(verdict):
    """验证徽章(卡P/V4):validate 蒙特卡洛判定 → 策略卡头徽章。
    入池/观察取自 reports/<sid>_validate.md;无报告=未验证。用中性色(蓝/琥珀/灰),
    不用盈红亏绿(徽章表状态非盈亏)。"""
    v = (verdict or "").strip()
    base = ("display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;"
            "font-weight:700;margin-left:6px;vertical-align:middle;")
    if "入池" in v:
        return f"<span style='{base}background:#e0edff;color:#1d4ed8' title='蒙特卡洛双压5%分位达标'>✅入池</span>"
    if "观察" in v:
        return f"<span style='{base}background:#fef3c7;color:#b45309' title='蒙卡下界未达标,仅观察'>👀观察</span>"
    return f"<span style='{base}background:#f1f1f4;color:#6b7280' title='尚无 validate 蒙卡报告'>⚠️未验证</span>"


def _load_factor_exposures():
    """读取 state/factor_exposure.json。不存在或异常返回 None。"""
    path = conf.STATE_DIR / "factor_exposure.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _exposure_html(sid, exp_data):
    """为某个策略生成因子暴露区 HTML（CSS条形图+表格+Chart.js画布）。
    etf_only 策略显示说明；数据缺失时返回空字符串。"""
    if exp_data is None:
        return ""
    strats = exp_data.get("strategies", {})
    if sid not in strats:
        return ""
    se = strats[sid]
    if se.get("etf_only"):
        return ('<details class="factor-exposure"><summary>⚖️ 风格暴露（ETF策略）</summary>'
                '<div class="exp-note">ETF策略持仓不映射个股风格因子，无法计算风格暴露。</div></details>')
    exposures = se.get("exposures", {})
    pred_vol = se.get("pred_vol")
    factors_order = ["size", "beta", "momentum", "resvol", "liquidity", "btop"]
    factor_labels = {
        "size": "市值(Size)", "beta": "贝塔(Beta)", "momentum": "动量(Mom)",
        "resvol": "残差波动(ResVol)", "liquidity": "流动性(Liq)", "btop": "账面市值比(BTOP)"
    }
    bars = ""
    for f in factors_order:
        val = exposures.get(f)
        if val is None:
            continue
        clamped = max(-2.0, min(2.0, val))
        pct = abs(clamped) / 4.0 * 100  # 全幅 [-2,2] → 100% 条宽
        if clamped >= 0:
            bar = f'<div class="exp-fill exp-pos" style="width:{pct}%;left:50%"></div>'
        else:
            bar = f'<div class="exp-fill exp-neg" style="width:{pct}%;left:{50-pct}%"></div>'
        bars += (f'<div class="exp-bar-row">'
                f'<span class="exp-label">{factor_labels.get(f, f)}</span>'
                f'<div class="exp-bar-wrap">{bar}<div class="exp-zero-line"></div></div>'
                f'<span class="exp-val">{val:+.2f}</span></div>')
    vol_str = f"（年化 {pred_vol*100:.1f}%）" if pred_vol is not None else ""
    vol_line = f'<div class="exp-vol">预测年化波动：{pred_vol*100:.1f}%</div>' if pred_vol is not None else ""
    chart_id = f"exposureChart_{sid.replace('@','_').replace('.','_')}"
    tbl_rows = "".join(
        f"<tr><td>{factor_labels.get(f, f)}</td><td>{exposures.get(f, 0):+.2f}</td></tr>"
        for f in factors_order if f in exposures)
    return (f'<details class="factor-exposure">'
            f'<summary>⚖️ 风格暴露与预测波动{vol_str}</summary>'
            f'{vol_line}'
            f'<div class="exp-chart-container">'
            f'<canvas id="{chart_id}" width="400" height="200"></canvas>'
            f'<div class="exp-fallback" hidden>'
            f'<div class="exp-fallback-note">📊 图表库(Chart.js)未加载，因子暴露图已降级为纯文本/条形呈现（见下方“风格条形”与“暴露值表格”，数据完整）。</div>'
            f'</div></div>'
            f'<div class="exp-bars">{bars}</div>'
            f'<table class="exposure-table"><thead><tr><th>因子</th><th>暴露值(z分)</th></tr></thead>'
            f'<tbody>{tbl_rows}</tbody></table>'
            f'<div class="exp-note"><a href="methodology.html#risk-model">暴露值如何解读？→</a> · '
            f'策略暴露数据将于下次策略运行时更新</div>'
            f'</details>')


def _exposure_chart_js(exp_data):
    """生成 Chart.js 渲染脚本（内嵌暴露数据）。Chart.js CDN 未加载时静默跳过。"""
    if exp_data is None:
        return ""
    strats = exp_data.get("strategies", {}) if exp_data else {}
    factors_order = exp_data.get("factors", ["size", "beta", "momentum", "resvol", "liquidity", "btop"])
    chart_entries = []
    for sid, se in strats.items():
        if se.get("etf_only"):
            continue
        exposures = se.get("exposures", {})
        if not exposures:
            continue
        chart_id = f"exposureChart_{sid.replace('@','_').replace('.','_')}"
        labels = [f[:4] for f in factors_order if f in exposures]
        values = [exposures.get(f, 0) for f in factors_order if f in exposures]
        colors = ["#3b82f6" if v >= 0 else "#ef4444" for v in values]
        cfg = {
            "type": "bar",
            "data": {
                "labels": labels,
                "datasets": [{"data": values, "backgroundColor": colors}],
            },
            "options": {
                "responsive": True,
                "maintainAspectRatio": False,
                "plugins": {
                    "legend": {"display": False},
                    "title": {
                        "display": True,
                        "text": _cn(sid) + " 风格暴露",
                        "font": {"size": 13},
                    },
                },
                "scales": {
                    "y": {
                        "title": {"display": True, "text": "z分"},
                        "min": -2.0,
                        "max": 2.0,
                    }
                },
            },
        }
        chart_entries.append(
            "(function(){var c=document.getElementById('%s');if(!c)return;"
            "try{new Chart(c,%s);}catch(e){}})();"
            % (chart_id, json.dumps(cfg, ensure_ascii=False))
        )
    if not chart_entries:
        return ""
    # 降级逻辑:Chart.js 未加载(typeof Chart==='undefined')时,隐藏空白 canvas,
    # 显示纯 HTML 兜底(.exp-fallback)与顶部横幅;否则照常渲染 Chart.js。
    return ("<script>"
            "(function(){"
            "if(typeof Chart==='undefined'){"
            "document.querySelectorAll('.exp-chart-container').forEach(function(c){"
            "var cv=c.querySelector('canvas');if(cv)cv.style.display='none';"
            "var fb=c.querySelector('.exp-fallback');if(fb)fb.hidden=false;});"
            "var bn=document.getElementById('chartFallbackBanner');if(bn)bn.hidden=false;"
            "return;}"
            + "".join(chart_entries)
            + "})();</script>")


def _grab(line, pat):
    m = re.search(pat, line or "")
    return m.group(1) if m else "—"


# ---------------- 价格/账户 ----------------
def _latest_close(conn, code):
    try:
        r = conn.execute("SELECT close FROM daily_bar WHERE code=? ORDER BY trade_date DESC LIMIT 1",
                         (code,)).fetchone()
        return float(r[0]) if r else 0.0
    except Exception:
        return 0.0


def ctx_name(conn, code):
    try:
        r = conn.execute("SELECT name FROM security WHERE code=?", (code,)).fetchone()
        return r[0] if r and r[0] else util.bare(code)
    except Exception:
        return util.bare(code)


def _acct_total(conn, a):
    total = a.get("cash", 0)
    for code, p in a.get("positions", {}).items():
        total += p.get("shares", 0) * _latest_close(conn, code)
    return total


# ---------------- 大盘指数（东方财富卡片风）---------------
MARKET_INDEX_CACHE = conf.STATE_DIR / "market_index.json"
MARKET_INDICES = {
    "sh.000001": {"label": "上证指数", "code_short": "SH"},
    "sz.399001": {"label": "深证成指", "code_short": "SZ"},
    "sz.399006": {"label": "创业板指", "code_short": "CYB"},
}


def _load_market_index(force_refresh=False):
    """加载上证/深证/创业板指日线。优先读缓存 JSON，不存在或 force_refresh 时通过 baostock 拉取。
    返回 {code: [(date, close), ...], ...} 或空 dict。"""
    if not force_refresh and MARKET_INDEX_CACHE.exists():
        try:
            raw = json.loads(MARKET_INDEX_CACHE.read_text(encoding="utf-8"))
            out = {}
            for k, v in raw.items():
                if k in MARKET_INDICES:
                    out[k] = [(d, float(c)) for d, c in v]
            if len(out) == len(MARKET_INDICES):
                return out
        except Exception:
            pass
    # 尝试 baostock
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            bs.logout()
            return {}
        out = {}
        for code, meta in MARKET_INDICES.items():
            rs = bs.query_history_k_data_plus(code, "date,close",
                                              start_date="2025-01-01",
                                              end_date=util.today_str(),
                                              frequency="d")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                out[code] = [(r[0], float(r[1])) for r in rows if r[1]]
        bs.logout()
        if out:
            cache = {}
            for k, v in out.items():
                cache[k] = [(d, round(c, 2)) for d, c in v]
            try:
                MARKET_INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
                MARKET_INDEX_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return out
    except Exception:
        return {}


def _market_index_cards(index_data):
    """生成东方财富风格的三大指数卡片：当前点位、涨跌额、涨跌幅、红绿颜色。
    初始用baostock日线收盘渲染，页面加载后JS从qt.gtimg.cn拉取实时行情覆盖。"""
    if not index_data:
        return '<div class="pos-empty">指数数据暂不可用（baostock 离线或网络不通），下次生成看板时将自动重试。</div>'

    cards_html = ""
    for code, meta in MARKET_INDICES.items():
        rows = index_data.get(code, [])
        if not rows or len(rows) < 2:
            continue
        last_date, last_close = rows[-1]
        prev_date, prev_close = rows[-2]
        chg = last_close - prev_close
        chg_pct = (chg / prev_close * 100) if prev_close > 0 else 0
        color_class = "up" if chg >= 0 else "down"
        color = "var(--up)" if chg >= 0 else "var(--down)"
        sign = "+" if chg >= 0 else ""

        cards_html += (
            f'<div class="idx-card {color_class}" id="idx_{code.replace(".","_")}">'
            f'<div class="idx-name">{meta["label"]}<span class="idx-code">{meta["code_short"]}</span></div>'
            f'<div class="idx-price" id="idx_price_{code.replace(".","_")}">{last_close:,.2f}</div>'
            f'<div class="idx-chg" id="idx_chg_{code.replace(".","_")}">'
            f'<span class="idx-chg-val" id="idx_chg_val_{code.replace(".","_")}">{sign}{chg:,.2f}</span>'
            f'<span class="idx-chg-pct" id="idx_chg_pct_{code.replace(".","_")}">{sign}{chg_pct:.2f}%</span>'
            f'</div>'
            f'</div>')

    latest_date = ""
    for code, rows in index_data.items():
        if rows:
            latest_date = rows[-1][0]
            break

    return (
        f'<div class="idx-cards" id="idx_cards">'
        f'{cards_html}'
        f'<div class="idx-date" id="idx_date">数据更新至 {latest_date}（打开页面后自动获取实时行情）</div>'
        f'</div>')


# ---------------- 颜色/格式(盈红亏绿) ----------------
def _col(x):
    """收益/涨跌上色:>0 红(--up),<0 绿(--down),=0或空 灰(--mut)(A股习惯,零值中性)。"""
    if x is None or abs(x) < 1e-9:
        return "var(--mut)"
    return "var(--up)" if x > 0 else "var(--down)"


def _pct(x, plus=True):
    if x is None:
        return "—"
    return (f"{x:+.1%}" if plus else f"{x:.1%}")


def _pct_span(x):
    return f"<span style='color:{_col(x)}'>{_pct(x)}</span>" if x is not None else "—"


# ---------------- 数据新鲜度 ----------------
def _freshness(conn):
    """按交易日落后数给横幅。返回 (last_date, banner_html)。"""
    try:
        last = conn.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
    except Exception:
        last = None
    if not last:
        return "—", "<div class='banner red'>🛑 数据库为空，请先运行 backfill 工作流，今日暂停跟单。</div>"
    try:
        days = cal._ensure(conn)
    except Exception:
        days = []
    now = util.now_cn()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    past = [d for d in days if d < today]
    if today in days and hhmm >= "15:00":
        expected_last = today
    else:
        expected_last = past[-1] if past else (days[-1] if days else last)
    delayed = len([d for d in days if last < d <= expected_last])
    if delayed <= 0:
        banner = ""
    elif delayed == 1:
        banner = "<div class='banner yellow'>⚠ 数据延迟1个交易日，请留意系统是否正常，谨慎跟单。</div>"
    else:
        banner = (f"<div class='banner red'>🛑 数据已过期{delayed}个交易日，今日暂停跟单，"
                  f"等待系统恢复（检查 Actions / backfill）。</div>")
    return last, banner


# ---------------- 实盘曲线(2026-07-31 起算) ----------------
def _live_series(a):
    """返回 (dates, pcts):自 LIVE_START 起相对基准净值的累计收益率序列(前置 0% 起点)。"""
    hist = a.get("nav_history", [])
    if not hist:
        return [], []
    base = None
    for d, nav in hist:
        if d < LIVE_START:
            base = nav
    if base is None or base <= 0:
        base = hist[0][1] or 1.0
    live = [(d, nav) for d, nav in hist if d >= LIVE_START]
    # 起点:LIVE_START 前最后一个有净值的交易日,收益率 0%
    start_anchor = None
    for d, nav in hist:
        if d < LIVE_START:
            start_anchor = d
    dates = [start_anchor] if start_anchor else []
    pcts = [0.0] if start_anchor else []
    for d, nav in live:
        dates.append(d)
        pcts.append(nav / base - 1)
    if not dates and live:                 # 无 pre-start 锚点的兜底
        d0, n0 = live[0]
        dates, pcts = [d0], [0.0]
        for d, nav in live[1:]:
            dates.append(d); pcts.append(nav / (n0 or 1.0) - 1)
    return dates, pcts


def _bench_series(conn, dmin, dmax):
    """沪深300 在 [dmin,dmax] 的归一化累计收益率(对齐交易日)。"""
    if not dmin:
        return {}, []
    try:
        rows = conn.execute(
            "SELECT trade_date, close FROM daily_bar WHERE code=? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date", (BENCH, dmin, dmax)).fetchall()
    except Exception:
        rows = []
    if not rows:
        return {}, []
    base = rows[0][1] or 1.0
    d2v = {r[0]: (r[1] / base - 1) for r in rows}
    return d2v, [r[0] for r in rows]


def _live_stats(a):
    """实盘累计收益/最大回撤/是否已起步。"""
    dates, pcts = _live_series(a)
    if not pcts:
        return {"total": None, "max_dd": None, "started": False}
    navs = [1 + p for p in pcts]
    if len(navs) < 2:
        return {"total": pcts[-1], "max_dd": 0.0, "started": len(pcts) > 1 or pcts[-1] != 0}
    m = bt.compute_metrics(navs)
    return {"total": navs[-1] / navs[0] - 1, "max_dd": m["max_dd"], "started": True}


def _chart_svg(dates, pcts, bench_d2v, up_color, w=720, h=260):
    """实盘收益率大图:策略线(终值定红/绿) + 沪深300灰虚线 + 坐标轴/网格。单点退化为点+标签。"""
    padL, padR, padT, padB = 46, 16, 16, 28
    bench_dates = [d for d in dates if d in bench_d2v]      # 只在策略有数据的交易日取基准
    bvals = [bench_d2v[d] for d in bench_dates]
    ys = list(pcts) + list(bvals) + [0.0]
    lo, hi = (min(ys), max(ys)) if ys else (-0.01, 0.01)
    if hi - lo < 0.002:
        lo -= 0.01; hi += 0.01
    span = hi - lo
    lo -= span * 0.10; hi += span * 0.10
    all_dates = sorted(set(dates) | set(bench_dates))
    idx = {d: i for i, d in enumerate(all_dates)}
    n = len(all_dates)

    def xof(d):
        return padL + (idx[d] / (n - 1) if n > 1 else 0.5) * (w - padL - padR)

    def yof(v):
        return padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB)

    # y 网格 + 刻度
    grid = ""
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = yof(v)
        emph = "stroke='#cbd5e1'" if abs(v) < (hi - lo) / 200 else "stroke='#eef1f4'"
        grid += f"<line x1='{padL}' y1='{y:.1f}' x2='{w-padR}' y2='{y:.1f}' {emph} stroke-width='1'/>"
        grid += (f"<text x='{padL-6}' y='{y+3:.1f}' text-anchor='end' font-size='10' "
                 f"fill='#94a3b8'>{v*100:+.0f}%</text>")
    # 0% 轴加深
    if lo <= 0 <= hi:
        y0 = yof(0)
        grid += f"<line x1='{padL}' y1='{y0:.1f}' x2='{w-padR}' y2='{y0:.1f}' stroke='#94a3b8' stroke-width='1'/>"

    def polyline(ds, vs, color, dash=""):
        if not ds:
            return ""
        pts = " ".join(f"{xof(d):.1f},{yof(v):.1f}" for d, v in zip(ds, vs))
        line = (f"<polyline fill='none' stroke='{color}' stroke-width='2' "
                f"{'stroke-dasharray=4' if dash else ''} points='{pts}'/>") if len(ds) > 1 else ""
        # 末点圆点(单点时也可见)
        dot = f"<circle cx='{xof(ds[-1]):.1f}' cy='{yof(vs[-1]):.1f}' r='3' fill='{color}'/>"
        return line + dot

    strat_line = polyline(dates, pcts, up_color)
    bench_line = polyline(bench_dates, bvals, "#9aa5b1", dash=True)
    # 末点数值标签(策略)
    label = ""
    if dates:
        lx, ly = xof(dates[-1]), yof(pcts[-1])
        anchor = "end" if lx > w - 60 else "start"
        dx = -6 if anchor == "end" else 6
        label = (f"<text x='{lx+dx:.1f}' y='{ly-6:.1f}' text-anchor='{anchor}' font-size='11' "
                 f"font-weight='700' fill='{up_color}'>{pcts[-1]*100:+.1f}%</text>")
    # x 轴首末日期
    xlab = ""
    if all_dates:
        xlab += (f"<text x='{padL}' y='{h-8}' font-size='10' fill='#94a3b8'>{all_dates[0][5:]}</text>")
        if n > 1:
            xlab += (f"<text x='{w-padR}' y='{h-8}' text-anchor='end' font-size='10' "
                     f"fill='#94a3b8'>{all_dates[-1][5:]}</text>")
    # 图例
    legend = (f"<circle cx='{padL+2}' cy='10' r='3' fill='{up_color}'/>"
              f"<text x='{padL+10}' y='13' font-size='10' fill='#64748b'>本策略</text>"
              f"<line x1='{padL+58}' y1='10' x2='{padL+74}' y2='10' stroke='#9aa5b1' stroke-width='2' stroke-dasharray='4'/>"
              f"<text x='{padL+80}' y='13' font-size='10' fill='#64748b'>沪深300</text>")
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' preserveAspectRatio='xMidYMid meet' "
            f"style='background:#fff;border-radius:8px'>{grid}{bench_line}{strat_line}{label}{xlab}{legend}</svg>")


def _mini_spark(pcts, up_color, w=300, h=40):
    """策略卡 summary 行的迷你走势(无坐标)。"""
    if len(pcts) < 2:
        return ""
    lo, hi = min(pcts + [0.0]), max(pcts + [0.0])
    rng = (hi - lo) or 1
    pts = " ".join(f"{i/(len(pcts)-1)*w:.1f},{h-(v-lo)/rng*(h-6)-3:.1f}" for i, v in enumerate(pcts))
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' height='{h}' preserveAspectRatio='none'>"
            f"<polyline fill='none' stroke='{up_color}' stroke-width='2' points='{pts}'/></svg>")


# ---------------- 今日操作(聚合) ----------------
def _op_calc(conn, a, o):
    """返回 (qty_desc, ref_price, target_amount)。买:约x%≈y股;卖:全部x股。"""
    code = o["code"]
    ref = _latest_close(conn, code)
    if o["side"] == "sell" or o.get("weight", 0) == 0:
        held = a.get("positions", {}).get(code, {}).get("shares", 0)
        return f"全部{held}股", ref, 0.0
    total = _acct_total(conn, a)
    amt = total * o.get("weight", 0)
    est = util.floor100(amt / ref) if ref else 0
    return f"约{o['weight']*100:.0f}%≈{est}股", ref, amt


def _factor_block(meta):
    rows = "".join(f"<tr><td>{html.escape(str(n))}</td><td>{html.escape(str(wt))}</td></tr>"
                   for n, wt in meta.get("factors", []))
    tbl = (f"<table class='fx'><tr><th>选股因子 / 规则</th><th>权重 / 说明</th></tr>{rows}</table>"
           if rows else "")
    ds = meta.get("data_source")
    ds_html = (f"<div class='ds'>📡 数据来源：{html.escape(ds)}</div>" if ds else "")
    # 显式"调仓频率"徽章(registry 派生, 保证准确): 日频=绿 / 周频=蓝 / 月频=灰
    fshort = meta.get("_freq_short", "—")
    ffull = meta.get("_freq_full", "—")
    fcolor = {"日频": "#16a34a", "周频": "#2563eb", "月频": "#6b7280"}.get(fshort, "#6b7280")
    freq_html = (f"<div class='freq-badge' style='display:inline-block;margin:2px 0 6px;"
                 f"padding:2px 10px;border-radius:12px;font-weight:700;font-size:12px;"
                 f"color:#fff;background:{fcolor}'>⏱ 调仓频率：{html.escape(fshort)}</div>"
                 f"<span class='freq-full' style='margin-left:8px;color:#888;font-size:12px'>"
                 f"{html.escape(ffull)}</span>")
    return (f"{freq_html}"
            f"<div class='tagline'>{html.escape(meta['tagline'])}</div>{tbl}"
            f"<div class='rb'>调仓：{html.escape(meta['rebalance'])} · 适合资金：{html.escape(meta['fit'])}</div>"
            f"{ds_html}")


def _positions_table(conn, a, sid, log_rows):
    pos = a.get("positions", {})
    cash = a.get("cash", 0)
    total = _acct_total(conn, a)
    init = a.get("init_capital", cash) or cash
    if not pos:
        return (f"<div class='pos-empty'>当前空仓（现金 100%，约 {cash:,.0f} 元）</div>")
    body = ""
    today = util.today_str()
    for code, p in pos.items():
        shares = p.get("shares", 0)
        avg = p.get("avg_cost", 0)
        last = _latest_close(conn, code)
        prev = conn.execute("SELECT close FROM daily_bar WHERE code=? AND trade_date<? ORDER BY trade_date DESC LIMIT 1",
                            (code, today)).fetchone()
        prev_close = float(prev[0]) if prev else last
        mv = shares * last
        pnl = (last / avg - 1) if avg else None
        posp = (mv / total) if total else 0
        nm = ctx_name(conn, code)
        bdate, reason = _buy_info(sid, code, log_rows, fallback_date=p.get("buy_date", ""))
        hold = _hold_days(bdate, today)
        body += (
            f"<tr data-code='{code}' data-avg='{avg}' data-shares='{shares}' data-prev='{prev_close}'>"
            f"<td class='l'>{util.bare(code)} {html.escape(nm)}</td>"
            f"<td>{shares}</td><td>{util.r2(avg)}</td><td class='cur'>{util.r2(last)}</td>"
            f"<td class='mv'>{mv:,.0f}</td>"
            f"<td class='cpnl' style='color:{_col(pnl)}'>{_pct(pnl)}</td>"
            f"<td class='dpnl' style='color:var(--mut)'>—</td>"
            f"<td>{posp*100:.0f}%</td></tr>"
            f"<tr class='why'><td colspan='8'>买入 {bdate or '—'} · 持有{hold}天 · "
            f"理由：{html.escape(reason)}</td></tr>")
    tot_pnl = (total / init - 1) if init else None
    cash_p = (cash / total) if total else 0
    body += (f"<tr class='sum'><td class='l'>现金</td><td colspan='3'></td>"
             f"<td class='mv'>{cash:,.0f}</td><td colspan='2'></td>"
             f"<td>{cash_p*100:.0f}%</td></tr>"
             f"<tr class='sum'><td class='l'>合计总资产</td><td colspan='3'></td>"
             f"<td class='mv'>{total:,.0f}</td>"
             f"<td style='color:{_col(tot_pnl)}'>{_pct(tot_pnl)}</td><td colspan='2'></td></tr>")
    return ("<table class='pos'><tr><th>标的</th><th>股数</th><th>成本</th><th>最新</th>"
            "<th>市值</th><th>累计盈亏</th><th>当日</th><th>仓位</th></tr>" + body + "</table>")


def _hold_days(bdate, today):
    try:
        a = datetime.strptime(bdate[:10], "%Y-%m-%d")
        b = datetime.strptime(today[:10], "%Y-%m-%d")
        return max(0, (b - a).days)
    except Exception:
        return 0


_WD = "一二三四五六日"


def _exec_date(pendings):
    """待执行订单的开盘执行日 = 其信号日的下一个交易日(与撮合口径一致)。返回 (date_str, 周X, 是否=今天)。"""
    sig = max((o.get("signal_date", "") for o in pendings), default="")
    if not sig:
        return None, "", False
    try:
        d = cal.next_trade_day(sig)
        wd = _WD[datetime.strptime(d, "%Y-%m-%d").weekday()]
    except Exception:
        return None, "", False
    return d, wd, (d == util.today_str())


# ---------------- 市场信号(regime + 利好板块,移植自 K线机 marketRegime) ----------------
def _market_regime_section(conn):
    """市场信号卡:牛熊 regime + 0-100 分 + 关键指标 + 基准迷你行 + 近期利好行业板块排行。"""
    if not conn:
        return ""
    try:
        import macro
        today = util.today_str()
        reg = macro.compute_market_regime(today, conn=conn)
        if not reg or reg.get("regime") == "数据不足":
            return ""
        regime = reg["regime"]
        score = reg.get("score", 50)
        # regime 配色(A股习惯:强势偏多=红,风险偏空=绿;转弱=橙警示,震荡=灰中性)
        rc = {"强势": "var(--up)", "风险": "var(--down)", "转弱": "#f59e0b", "震荡": "var(--mut)"}
        color = rc.get(regime, "var(--mut)")
        chips = []
        if reg.get("ret_1m") is not None:
            chips.append(f"近1月 {reg['ret_1m']:+.1f}%")
        if reg.get("breadth") is not None:
            chips.append(f"强势广度 {reg['breadth']}%")
        ma = []
        for k, lbl in (("aboveMa20", "MA20"), ("aboveMa50", "MA50"), ("aboveMa200", "MA200")):
            v = reg.get(k)
            if v is not None:
                ma.append(lbl + ("↑" if v else "↓"))
        if ma:
            chips.append(" ".join(ma))
        chips_html = " · ".join(html.escape(c) for c in chips)
        # 基准迷你行
        bench_parts = []
        for b in reg.get("benchmarks", []):
            r1 = b.get("ret_1m")
            bcol = _col(r1 / 100 if r1 is not None else None)
            rtxt = f"{r1:+.1f}%" if r1 is not None else "—"
            bench_parts.append(f"<span class='rg-bench'>{html.escape(b['name'])} "
                               f"<b style='color:{bcol}'>{rtxt}</b></span>")
        bench_html = ("<div class='rg-benches'>" + " ".join(bench_parts) + "</div>") if bench_parts else ""
        # 近期利好行业板块
        try:
            sectors, _ = macro.top_bullish_sectors(today, conn=conn, top=6)
        except Exception:
            sectors = []
        sec_parts = []
        for s in sectors:
            mp = s.get("momentum_pct")
            scol = _col(mp / 100 if mp is not None else None)
            sec_parts.append(f"<span class='rg-sec'>{html.escape(str(s['name']))} "
                             f"<b style='color:{scol}'>{mp:+.1f}%</b></span>")
        sec_html = ("<div class='rg-sectors'><span class='rg-lbl'>近60日利好行业</span>"
                    + " ".join(sec_parts) + "</div>") if sec_parts else ""
        return (
            f"<div class='sec'>🧭 市场信号</div>"
            f"<div class='rg-card'>"
            f"<div class='rg-head'><span class='rg-badge' style='background:{color}'>{html.escape(regime)} · {score}</span>"
            f"<span class='rg-metrics'>{chips_html}</span></div>"
            f"{bench_html}{sec_html}"
            f"<div class='rg-note'>{html.escape(reg.get('summary', ''))}</div>"
            f"</div>")
    except Exception:
        return ""


# ---------------- 新闻/产业信号展示 ----------------
def _news_industry_section(conn):
    """生成新闻/产业信号展示区域。"""
    if not conn:
        return ""
    today = util.today_str()

    def _load_signals(d):
        r = conn.execute("SELECT score, evidence FROM news_signal WHERE signal_date=? AND scope='market'",
                         (d,)).fetchone()
        m = float(r[0]) if r else None
        ev = r[1] if r else ""
        sec = []
        for scope, score, e in conn.execute(
                "SELECT scope, score, evidence FROM news_signal WHERE signal_date=? AND scope LIKE 'sector:%'",
                (d,)).fetchall():
            if float(score) != 0:
                sec.append((scope.replace("sector:", ""), float(score), e))
        stk = []
        for scope, score, e in conn.execute(
                "SELECT scope, score, evidence FROM news_signal WHERE signal_date=? AND scope LIKE 'stock:%'",
                (d,)).fetchall():
            if float(score) != 0:
                stk.append((scope.replace("stock:", ""), float(score), e))
        return m, ev, sec, stk

    try:
        market_score, market_ev, sector_signals, stock_signals = _load_signals(today)
        note = ""
        if market_score is None and not sector_signals and not stock_signals:
            # 兜底:今日无信号时回退到最近一次有信号的快照,避免板块整块消失
            latest = conn.execute(
                "SELECT signal_date FROM news_signal "
                "WHERE scope='market' OR scope LIKE 'sector:%' OR scope LIKE 'stock:%' "
                "ORDER BY signal_date DESC LIMIT 1").fetchone()
            if latest:
                market_score, market_ev, sector_signals, stock_signals = _load_signals(latest[0])
                note = f"<div class='rg-note'>（展示最近一次信号快照 {latest[0]}，今日尚未生成新信号）</div>"

        # 构建HTML
        parts = []

        # 市场面
        if market_score is not None:
            color = "var(--up)" if market_score > 0.5 else "var(--down)" if market_score < -0.5 else "var(--mut)"
            label = "利好" if market_score > 0.5 else "利空" if market_score < -0.5 else "中性"
            parts.append(f"<span class='news-tag' style='background:{color}'>市场面 {label}({market_score:+.1f})</span>")

        # 行业面
        for etf_code, score, ev in sector_signals[:5]:
            color = "var(--up)" if score > 0 else "var(--down)"
            nm = _ETF_NAMES.get(etf_code, etf_code)
            parts.append(f"<span class='news-tag' style='background:{color}'>{nm} {score:+.1f}</span>")

        # 个股面
        for code, score, ev in stock_signals[:3]:
            color = "var(--up)" if score > 0 else "var(--down)"
            nm = ctx_name(conn, code) if conn else util.bare(code)
            parts.append(f"<span class='news-tag' style='background:{color}'>{nm} {score:+.1f}</span>")

        tags = " ".join(parts)
        if not tags:
            # 今日与历史均无任何信号:仍然渲染区块(空状态提示),避免板块消失
            return (f"<div class='sec'>📰 新闻/产业信号</div>"
                    f"<div class='rg-note'>今日暂无新增产业/市场信号（消息面扫描未产生有效信号）。</div>")
        return (f"<div class='sec'>📰 新闻/产业信号</div>"
                f"<div class='news-bar'>{tags}</div>{note}")

    except Exception:
        return ""


# 快讯来源展示名/色(与 news_adapter.SOURCE_META 对齐)
_SRC_NAME = {"sina_roll": "新浪", "em_global": "东财", "news_cctv": "央视"}
_SRC_COLOR = {"sina_roll": "#e6162d", "em_global": "#d92b2b", "news_cctv": "#c0392b"}


# ---------------- 新闻含金量过滤 ----------------
# 低价值(与 A 股投资决策无关)→ 直接剔除:娱乐八卦/天气/体育/人名轶事/社会花边。
_NEWS_JUNK_RE = re.compile(
    r"(张雪峰|热浪|演唱会|离婚|结婚|出轨|综艺|春晚|世界杯|奥运|欧洲杯|亚运|电影票房|电视剧|"
    r"明星|网红|微博热搜|天气|降温|升温|暴雨|暴雨预警|台风|沙尘暴|大雾|结冰|彩票|"
    r"相亲|选秀|粉丝|代言|绯闻|去世|病逝|悼念|讣告|婚礼|满月|寿宴|度假|旅游攻略|"
    r"演唱会门票|票房|收视率|综艺节目|脱口秀|相声|小品)")

# 市场强信号(命中则重要性拉满):政策/监管/央行/财报/并购/风险事件等真正影响 A 股的东西。
_NEWS_BOOST_RE = re.compile(
    r"(涨停|跌停|央行|降准|加息|MLF|逆回购|GDP|CPI|PPI|PMI|财报|季报|年报|业绩|"
    r"营收|净利润|亏损|盈利|收购|重组|借壳|退市|ST|减持|增持|回购|分红|派息|配股|"
    r"证监会|央行|银保监会|国务院|政治局|国常会|发改委|工信部|商务部|财政部|"
    r"关税|制裁|贸易战|反倾销|出口管制|实体清单|美联储|非农|加息|缩表|"
    r"IPO|新股|申购|北向资金|外资|净流入|净流出|融资余额|融券|"
    r"利好|利空|暴雷|违约|债务|商誉减值|立案|问询函|风险提示|"
    r"半导体|芯片|新能源|光伏|锂电|医药|创新药|军工|券商|银行|地产|白酒|消费|"
    r"黄金|原油|期货|大宗商品|汇率|人民币|美元|指数|沪指|深成指|创业板|科创板|"
    r"放量|缩量|成交额|破万亿|历史新高|新低)")

# 信源基础分(权威源更具参考价值):央视(S0)>东财全球(S3权威量大)>新浪滚动(S3)。
_SRC_BASE = {"news_cctv": 3, "em_global": 3, "em_global_cjzc": 3, "sina_roll": 2,
             "sina_global": 2, "cls_global": 2, "em_global_np": 2}


def _news_importance(title, source):
    """新闻重要性评分(越高越重要)。基础分(信源) + 市场强信号命中加权。"""
    score = _SRC_BASE.get(source, 1)
    t = title or ""
    if _NEWS_BOOST_RE.search(t):
        score += 5
    # 较长标题通常信息更实质(短讯如"X涨超3%"也保留,但不过度加权)
    if len(t) >= 24:
        score += 1
    return score


def _news_flash_section(conn):
    """今日重点快讯:提升含金量——剔除娱乐/天气等低价值新闻,按重要性排序,
    默认仅展示最重要 5 条,其余折叠(点'展开全部'展开)。"""
    if not conn:
        return ""
    today = util.today_str()
    cutoff = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        rows = conn.execute(
            "SELECT ts, title, source FROM news_raw "
            "WHERE (ts LIKE ? OR ts >= ?) AND length(title)>0 "
            "ORDER BY ts DESC LIMIT 60",
            (today + "%", cutoff)).fetchall()
        if not rows:
            # 兜底:云端抓取偶发失败时,回退展示最近7天快讯,避免整块新闻区消失
            cutoff7 = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT ts, title, source FROM news_raw "
                "WHERE ts >= ? AND length(title)>0 "
                "ORDER BY ts DESC LIMIT 60", (cutoff7,)).fetchall()
        if not rows:
            return ""
        seen, items = set(), []
        for ts, title, source in rows:
            if title in seen:
                continue
            seen.add(title)
            items.append((str(ts), str(title), str(source)))
        # 过滤低价值新闻(娱乐/天气/八卦等),保留对 A 股决策有用的内容
        items = [it for it in items if not _NEWS_JUNK_RE.search(it[1])]
        if not items:
            return ""
        # 按重要性降序(同分按时间新→旧),保证"最重要"的排在前面
        items.sort(key=lambda it: (_news_importance(it[1], it[2]), it[0]), reverse=True)

        def _fmt(ts):
            if ts.isdigit() and len(ts) >= 10:
                try:
                    return datetime.fromtimestamp(int(ts[:10])).strftime("%m-%d %H:%M")
                except Exception:
                    return ""
            return ts[:16]

        def _render(it):
            ts, title, source = it
            color = _SRC_COLOR.get(source, "var(--mut)")
            return (f"<div class='flash-item'>"
                    f"<span class='flash-time'>{_fmt(ts)}</span>"
                    f"<span class='flash-src' style='color:{color}'>{_SRC_NAME.get(source, source)}</span>"
                    f"<span class='flash-title'>{html.escape(title)}</span>"
                    f"</div>")

        top5 = items[:5]
        rest = items[5:]
        parts = [f"<div class='flash-list'>{''.join(_render(it) for it in top5)}</div>"]
        if rest:
            rest_html = "".join(_render(it) for it in rest)
            parts.append(
                f"<details class='flash-more'><summary>展开全部 {len(items)} 条</summary>"
                f"<div class='flash-list'>{rest_html}</div></details>")
        return (f"<div class='sec'>📰 今日重点快讯 <span class='sec-sub'>（已过滤娱乐/天气等低价值，按重要性排序）</span></div>"
                f"{''.join(parts)}")
    except Exception:
        return ""


# ETF名称映射(与data_adapter同步)
_ETF_NAMES = {
    "sh510300": "沪深300ETF", "sh510500": "中证500ETF", "sh512890": "红利低波ETF",
    "sh518880": "黄金ETF", "sh513100": "纳指ETF", "sh511010": "国债ETF",
    "sh512000": "券商ETF", "sh512480": "半导体ETF", "sh512010": "医药ETF",
    "sz159928": "消费ETF", "sh512660": "军工ETF", "sh516160": "新能源ETF",
    "sh512690": "酒ETF", "sh515790": "光伏ETF", "sh512800": "银行ETF",
}


# ---------------- 主生成 ----------------
def _ensure_vendor_chart():
    """尽量把 Chart.js 落地到 docs/vendor/(摆脱境外 CDN)。缺失或下载失败均静默,页面其余正常。"""
    try:
        import urllib.request
        vend = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "vendor")
        os.makedirs(vend, exist_ok=True)
        dst = os.path.join(vend, "chart.umd.min.js")
        if os.path.exists(dst) and os.path.getsize(dst) > 10000:
            return
        for url in ("https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js",
                   "https://cdn.bootcdn.net/ajax/libs/Chart.js/4.4.7/chart.umd.min.js",
                   "https://unpkg.com/chart.js@4.4.7/dist/chart.umd.min.js"):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
                open(dst, "wb").write(data)
                if os.path.getsize(dst) > 10000:
                    return
            except Exception:
                continue
    except Exception:
        pass


def generate(out_path=None):
    _ensure_vendor_chart()
    accts = _load_accounts()
    log_rows = _load_trade_log()
    today = util.today_str()
    from db import get_conn
    try:
        conn = get_conn()
    except Exception:
        conn = None
    last, banner = _freshness(conn) if conn else ("—", "")

    # ===== 操作计划聚合(按执行日,而非"今日";每条明确标注所属策略) =====
    all_pending = [o for a in accts.values() for o in a.get("pending", [])]
    exec_d, exec_wd, is_today = _exec_date(all_pending)
    if all_pending and exec_d:
        ops_title = ("今日操作" if is_today else f"操作计划（{exec_d[5:]} 周{exec_wd} 开盘跟单）")
        head_txt = (f"【操作计划】将于 {exec_d} 周{exec_wd} 开盘按价格带手动跟单"
                    if not is_today else "【今日操作】按开盘价附近手动跟单")
    else:
        ops_title = "操作计划"
        head_txt = ""

    op_rows_html = []
    copy_lines = []
    for sid, a in sorted(accts.items()):
        for o in a.get("pending", []):
            qty, ref, amt = _op_calc(conn, a, o) if conn else ("", 0, 0)
            nm = ctx_name(conn, o["code"]) if conn else util.bare(o["code"])
            is_sell = o["side"] == "sell" or o.get("weight", 0) == 0
            side_cn = "卖出" if is_sell else "买入"
            cls = "sell" if is_sell else "buy"
            band = ""
            if not is_sell and ref:
                band = (f"<div class='band'>跟单价格带：{util.r2(ref*BUY_BAND[0])} ~ {util.r2(ref*BUY_BAND[1])}"
                        f"（高于上带建议减半或放弃，勿追高）</div>")
            op_rows_html.append(
                f"<div class='op {cls}' data-code='{o['code']}' data-side='{o['side']}' "
                f"data-amount='{amt:.0f}' data-ref='{util.r2(ref)}'>"
                f"<div class='op-hd'><span class='chip'>{_cn(sid)}</span>"
                f"<b>{side_cn} {util.bare(o['code'])} {html.escape(nm)}</b></div>"
                f"<span class='q'>{qty} · 参考价 {util.r2(ref)}</span>"
                f"<span class='reason'>{html.escape(o.get('reason', ''))}</span>{band}</div>")
            copy_lines.append(f"【{_cn(sid)}】{side_cn} {util.bare(o['code'])} {nm} {qty} 参考价{util.r2(ref)}")
    if op_rows_html:
        copy_js = json.dumps((f"操作计划 {exec_d}(周{exec_wd})开盘跟单：\n" if exec_d else "") + "\n".join(copy_lines),
                             ensure_ascii=False)
        ops_section = (
            f"<div class='ops-head'><span>{head_txt}</span>"
            f"<button class='copybtn' onclick='copyOps()'>📋 复制指令</button></div>"
            + "".join(op_rows_html)
            + "<div class='op-note'>每条操作左侧标签为所属策略；页面会尝试用实时价校准股数与金额（失败则显示“昨收参考”）。</div>"
            + f"<script>var OPS_TEXT={copy_js};function copyOps(){{"
              "if(navigator.clipboard){navigator.clipboard.writeText(OPS_TEXT).then(function(){alert('已复制操作指令');},"
              "function(){alert('复制失败，请手动选择');});}else{alert('浏览器不支持一键复制，请手动选择');}}</script>")
    else:
        ops_section = (f"<div class='op none'>暂无待执行操作（各策略空仓或未到调仓日）。"
                       f"赛马自 {LIVE_START} 起跑，有操作时此处按策略列出。</div>")

    # ===== 大盘指数（东方财富风卡片）=====
    market_data = _load_market_index()
    market_section = ""
    if market_data:
        market_section = (
            f"<div class='sec'>📈 大盘指数</div>"
            f"{_market_index_cards(market_data)}")
    else:
        market_section = (
            f"<div class='sec'>📈 大盘指数</div>"
            f"<div class='pos-empty'>指数数据暂不可用（baostock 离线或网络不通），"
            f"下次生成看板时将自动重试。</div>")

    # ===== 实盘赛马总览 =====
    ov = ""
    for sid, a in sorted(accts.items()):
        ls = _live_stats(a)
        st = "🔴熔断" if a.get("frozen") else "🟢正常"
        bt_line, verdict = _backtest_summary(sid)
        npos = len(a.get("positions", {}))
        total_col = _col(ls["total"]) if ls["total"] is not None else "var(--mut)"
        total_txt = _pct(ls["total"]) if ls["started"] else "今日起步"
        ddtxt = _pct(ls["max_dd"], plus=False) if ls["max_dd"] is not None else "—"
        ov += (f"<tr><td class='l'>{_cn(sid)}</td>"
               f"<td style='color:{total_col};font-weight:700'>{total_txt}</td>"
               f"<td>{ddtxt}</td><td>{a.get('nav',1):.3f}</td><td>{npos}</td><td>{st}</td>"
               f"<td class='ref'>{bt_line}{' · '+verdict if verdict else ''}</td></tr>")
    overview = ("<table class='ov'><tr><th>策略</th><th>实盘累计</th><th>最大回撤</th><th>净值</th>"
                "<th>持仓</th><th>状态</th><th>回测参考(2022→今)</th></tr>" + ov + "</table>") if accts else ""

    # ===== 各策略卡 =====
    exp_data = _load_factor_exposures()
    cards = ""
    for sid, a in sorted(accts.items()):
        meta = _meta(sid)
        ls = _live_stats(a)
        st = "🔴熔断" if a.get("frozen") else "🟢正常"
        dates, pcts = _live_series(a)
        _last = pcts[-1] if pcts else None
        up_color = "#6b7280" if (_last is None or abs(_last) < 1e-9) else ("#d92b2b" if _last > 0 else "#0a9e6b")
        bench_d2v, _ = _bench_series(conn, dates[0], dates[-1]) if (conn and dates) else ({}, [])
        chart = _chart_svg(dates, pcts, bench_d2v, up_color) if dates else f"<div class='pos-empty'>曲线将于 {LIVE_START} 起累积</div>"
        cur_txt = _pct(ls["total"]) if ls["started"] else "今日起步"
        bt_line, verdict = _backtest_summary(sid)
        bt_html = f"<div class='bt'>📈 回测(2022→今)：{html.escape(bt_line)}</div>" if bt_line else ""
        # 该策略今日操作
        op_items = []
        for o in a.get("pending", []):
            qty, ref, _amt = _op_calc(conn, a, o) if conn else ("", 0, 0)
            nm = ctx_name(conn, o["code"]) if conn else util.bare(o["code"])
            is_sell = o["side"] == "sell" or o.get("weight", 0) == 0
            op_items.append(
                f"<div class='op {'sell' if is_sell else 'buy'}'>"
                f"<b>{'卖出' if is_sell else '买入'} {util.bare(o['code'])} {html.escape(nm)}</b>"
                f"<span class='q'>{qty} · 参考价 {util.r2(ref)}</span>"
                f"<span class='reason'>{html.escape(o.get('reason', ''))}</span></div>")
        ops = "".join(op_items) or "<div class='op none'>无待执行操作</div>"
        # 策略逻辑折叠 + 方法论链接
        logic_block = (
            f"<details class='strategy-logic'><summary>📐 策略逻辑说明（点击展开）</summary>"
            f"{_factor_block(meta)}"
            f"<a class='logic-link' href='methodology.html#{sid}'>完整方法论 →</a>"
            f"</details>")
        # 因子暴露区域(放在持仓表之后)
        exposure_html = _exposure_html(sid, exp_data)
        cards += (
            f"<div class='card'>"
            f"<div class='card-h'><b>{meta['name']}</b>{_scope_badge(sid)}<span class='risk'>{meta['risk']}</span>"
            f"{_verdict_badge(verdict)}"
            f"<span class='stat'>{st}</span></div>"
            f"{logic_block}"
            f"<details><summary>📈 实盘收益率曲线（{LIVE_START[5:]} 起）当前 "
            f"<span style='color:{up_color};font-weight:700'>{cur_txt}</span></summary>{chart}{bt_html}</details>"
            f"<div class='sub2'>最新持仓</div>{_positions_table(conn, a, sid, log_rows)}"
            f"<div class='sub2'>操作计划</div>{ops}"
            f"{exposure_html}"
            f"</div>")
    if not accts:
        cards = "<p class='empty'>暂无策略状态。请先运行 run_daily.py 或回测生成 state/。</p>"

    # 顶部导航
    nav = ('<nav><a href="index.html">📊 策略看板</a>'
           '<a href="methodology.html">📐 策略方法论</a>'
           '<a href="methodology.html#risk-model">📈 因子风险模型</a>'
           '</nav>')
    chart_fb_banner = ("<div id='chartFallbackBanner' class='chart-fallback-banner' hidden>"
                        "⚠️ 图表库(Chart.js)未加载，因子暴露图已降级为纯文本/条形展示（数据完整，"
                        "见各策略卡内“风格暴露”的条形图与暴露值表格）。</div>")
    scope_banner = ""  # 已移除:原"现有策略…s54/s55 仍测试中"说明段(用户要求不再展示)
    body = (
        f"{nav}<h1>📊 A股模拟跟单看板</h1>"
        f"{chart_fb_banner}"
        f"<div class='sub'>生成 {today} · 数据最新 {last} · 实盘赛马自 {LIVE_START} 起（所有策略 10 万元起跑，看未来收益） · 模拟/历史不代表未来，非投资建议，人工跟单</div>"
        f"{banner}"
        f"{scope_banner}"
        f"{market_section}"
        f"{_market_regime_section(conn)}"
        f"{_news_flash_section(conn)}"
        f"{_news_industry_section(conn)}"
        f"<div class='sec'>{ops_title}</div>{ops_section}"
        f"<div class='sec'>🏆 实盘赛马总览（{LIVE_START} 起算 · 前向真实模拟收益，非回测）</div>{overview}"
        f"<div class='sec'>各策略详情</div>{cards}"
        f"<div class='sec'><a href='trades.html'>📜 查看全部历史交易记录 →</a></div>"
        f"{_FOOTER}")
    chart_js = _exposure_chart_js(exp_data)
    html_doc = f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>" \
               f"<meta name='viewport' content='width=device-width, initial-scale=1'>" \
               f"<title>A股模拟跟单看板</title>" \
               f"<script src='./vendor/chart.umd.min.js'>" \
               f"</script>{_STYLE}</head><body><div class='wrap'>{body}</div>{chart_js}{_LIVE_JS}</body></html>"

    out_path = out_path or (conf.ROOT / "docs" / "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    try:
        generate_trades(conn)
    except Exception:
        pass
    try:
        generate_methodology(out_path=conf.ROOT / "docs" / "methodology.html")
    except Exception:
        pass
    if conn:
        conn.close()
    return str(out_path)


# ---------------- 方法论页 ----------------
_METHODOLOGY_STYLE = """<style>
:root{--bg:#f6f7f9;--fg:#1f2937;--mut:#6b7280;--card:#fff;--line:#e5e7eb;--up:#d92b2b;--down:#0a9e6b}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--fg);font-size:15px;line-height:1.6}
.wrap{max-width:800px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0}h2{font-size:18px;margin:24px 0 10px;border-bottom:2px solid var(--line);padding-bottom:6px}
h3{font-size:15.5px;margin:16px 0 8px;color:#334155}
p,li{font-size:14px}ul{padding-left:20px}
a{color:#2563eb;text-decoration:none}
/* 导航 */
nav{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
nav a{display:inline-block;padding:6px 14px;background:#2563eb;color:#fff;border-radius:8px;
text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
nav a:hover{background:#1d4ed8}
/* 目录 */
.toc{background:var(--card);border-radius:10px;padding:14px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.toc a{display:block;padding:3px 0;font-size:13.5px}
/* 策略块 */
.strat-block{background:var(--card);border-radius:10px;padding:14px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.strat-block summary{cursor:pointer;font-weight:600;font-size:15px;padding:4px 0;color:#1f2937}
.strat-block details{margin:0}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;font-size:13px;margin:8px 0}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{background:#f0f2f5;color:var(--mut);font-weight:600;font-size:12px}
.note{color:var(--mut);font-size:12px;margin:4px 0}
.foot{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}
.diff{background:#fef9c3;padding:8px 12px;border-radius:6px;font-size:12.5px;margin:6px 0}
.risk-badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;
background:#fef3c7;color:#92400e;margin-left:6px}
.scope-badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;margin-left:6px;font-weight:600;vertical-align:middle}
.scope-mb{background:#dbeafe;color:#1d4ed8}
.scope-alla{background:#ffedd5;color:#c2410c}
</style>"""


def _enabled_strategy_ids():
    """当前 config.yaml strategies 置 true 的策略集合；读取异常返回 None(调用方应视为"不过滤")。"""
    try:
        return {sid for sid, on in (conf.load_config().get("strategies") or {}).items() if on}
    except Exception:
        return None


# ---------------- 策略覆盖范围（主板 / 全A股） ----------------
# 现有参赛策略仅交易 A股主板（引擎层 main_board_universe 硬约束）；
# s53/s54/s55 为全A股候选（pool_index: all_a，覆盖主板+科创+创业+北交），测试中。
CANDIDATE_SIDS = [
    "s53_all_a_momentum_smallcap@v1",
    "s54_all_a_industry_mom@v1",
    "s55_all_a_value_quality@v1",
]


def _scope_badge(sid):
    """策略覆盖范围徽标：主板 vs 全A股（含科创/创业/北交）。"""
    if "all_a" in sid:
        return '<span class="scope-badge scope-alla">🟧 全A股·含科创/创业/北交</span>'
    return '<span class="scope-badge scope-mb">🟦 A股主板</span>'


def _methodology_candidate_block(sid):
    """全A股候选策略卡：s53 已验证入池，s54/s55 测试中。带范围徽标与说明。"""
    import pathlib
    meta = _meta(sid)
    factors = meta.get("factors", [])
    factor_rows = ""
    if factors:
        factor_rows = "".join(
            f"<tr><td>{html.escape(str(n))}</td><td>{html.escape(str(wt))}</td></tr>"
            for n, wt in factors)
    # 是否已验证入池：存在 reports/<sid前缀>*.md 即视为云端已跑出五关报告
    rep = list(pathlib.Path(conf.ROOT / "reports").glob(f"{sid.split('@')[0]}*.md"))
    if rep:
        cand_badge = '<span class="risk-badge" style="background:#dcfce7;color:#15803d">✅ 已验证入池</span>'
        verified_note = ('<p class="note"><b>验证结果（云端五关 + 蒙特卡洛，报告日 2026-07-30）</b>：'
                         '主回测 年化16.0% / 回撤15.2% / 夏普1.03 / Calmar1.05；'
                         '样本外(2024-今) 年化15.3% / 回撤12.4% / Calmar1.23（外优于内）；'
                         '最坏5%情形总收益仍 +8.4%，已入池。满足「回撤≤年化」铁律，为当前全A股唯一过约束策略。'
                         'config 已启用，实盘起跑后进入赛马总览（看板主卡仅统计实盘序列）。</p>')
    else:
        cand_badge = '<span class="risk-badge" style="background:#ffedd5;color:#c2410c">🧪 测试中·待测达标</span>'
        verified_note = ('<p class="note"><b>覆盖范围</b>：全A股（主板 + 科创板688 + 创业板300·301 + 北交所），不卡主板前缀；'
                         f'与现有主板策略（主板硬约束）形成两档。当前回测未达「年化&gt;10% 且 回撤≤年化」铁律，'
                         f'达标后从注册日起入赛马，不追溯。</p>')
    return (f'<div class="strat-block" id="{sid}">'
            f'<details><summary>{meta["name"]}{_scope_badge(sid)}{cand_badge}</summary>'
            f'<p class="tagline">{html.escape(meta["tagline"])}</p>'
            f'<h3>因子构成</h3>'
            f'<table><thead><tr><th>因子 / 规则</th><th>权重 / 说明</th></tr></thead>'
            f'<tbody>{factor_rows}</tbody></table>'
            f'<p class="note">调仓：{html.escape(meta["rebalance"])} · 适合资金：{html.escape(meta["fit"])}</p>'
            f'{verified_note}'
            f'</details></div>')


def _methodology_candidates_section():
    blocks = "".join(_methodology_candidate_block(s) for s in CANDIDATE_SIDS)
    return (f'<h2 id="candidates-alla">全A股候选策略</h2>'
            f'<div class="banner yellow">🟧 <b>全A股覆盖范围</b>：以下候选策略覆盖 <b>主板 + 科创板(688) + 创业板(300·301) + 北交所</b>，'
            f'是看板准备新增的「全A股策略」。现有 5 只参赛策略（s26/s29/s32/s37/s42）仅交易 <b>A股主板</b>（主板硬约束）。'
            f'<b>s53 已验证入池</b>（云端五关 + 蒙特卡洛通过，年化16.0%/回撤15.2%/夏普1.03）；s54/s55 仍在测试中。</div>'
            f'{blocks}')


def _methodology_toc():
    """目录锚点导航：仅展示当前 config.yaml 启用（参赛）策略，已下线/归档策略不展示。"""
    enabled = _enabled_strategy_ids()
    items = ""
    for sid, meta in sorted(STRAT_META.items()):
        if enabled is not None and sid not in enabled:
            continue  # 已下线/归档策略不在看板展示
        items += f'<a href="#{sid}">{meta["name"]}</a>\n'
    items += '<a href="#risk-model">因子与风险模型</a>\n'
    items += '<a href="#candidates-alla">全A股候选策略</a>\n'
    return f'<div class="toc"><b>📑 目录</b>\n{items}</div>'


def _methodology_strat_block(sid):
    """单个策略的方法论区块（含完整投资逻辑、因子表、适用环境、风险提示）。"""
    meta = _meta(sid)
    factors = meta.get("factors", [])
    factor_rows = ""
    if factors:
        factor_rows = "".join(
            f"<tr><td>{html.escape(str(n))}</td><td>{html.escape(str(wt))}</td></tr>"
            for n, wt in factors)
    # 适用环境与风险提示（按策略类型分）
    env_risk = {
        "s2_etf@v1": ("<b>适用环境</b>：趋势明确的市场（牛市/熊市均可），震荡市表现一般。"
                       "当市场连续下跌时国债ETF提供避险保护。<br>"
                       "<b>风险提示</b>：单品种集中持仓，轮动时点决定收益差距；"
                       "动量策略在趋势反转拐点可能滞后切换。"),
        "s1_dividend@v1": ("<b>适用环境</b>：震荡市或慢牛市中表现突出，高股息股在利率下行期有防御价值。"
                          "<br><b>风险提示</b>：高股息陷阱——部分股票因股价暴跌导致股息率虚高；"
                          "利率上行周期高股息股相对吸引力下降。"),
        "s1_dividend@v2": ("<b>适用环境</b>：与v1相同，额外过滤了盈利质量不足的高股息股，减少股息陷阱风险。"
                          "<br><b>风险提示</b>：ROE筛选可能剔除周期底部的高股息机会；"
                          "A股多数公司ROE波动大，连续3年门槛可能使候选池过小。"),
        "s3_ma_trend@v1": ("<b>适用环境</b>：趋势明确的中期行情（牛熊均可，急涨急跌最好）。"
                          "<br><b>风险提示</b>：震荡市频繁假突破·假跌破，磨损成本高；"
                          "均线信号滞后于价格，顶部区域可能在跌破均线前已回吐大量利润。"),
        "s4_smallcap@v1": ("<b>适用环境</b>：风险偏好较高的市场环境，小市值因子溢价周期。"
                          "当前池为沪深300(非真小盘)，因子暴露偏向'大盘内选中小'。<br>"
                          "<b>风险提示</b>：小市值天然波动大；流动性风险——极端行情可能无法按预期价格成交；"
                          "免费数据限制使池仅为沪深300，非真正的小盘精选。"),
        "s5_grid@v1": ("<b>适用环境</b>：震荡/慢牛市场，PE估值在合理区间（十年20-70%分位）时效果最佳。"
                      "<br><b>风险提示</b>：极端单边行情（如2007/2015大牛）过早卖出导致踏空；"
                      "PE分位依赖历史数据，估值中枢可能永久性变化（如市场制度改革）。"),
        "s6_sector@v1": ("<b>适用环境</b>：有明确产业主线的市场（政策驱动、景气周期），"
                        "行业轮动规律明显时表现好。<br>"
                        "<b>风险提示</b>：行业集中度高、单品种持仓；"
                        "政策变化或景气拐点可能引发剧烈回撤；弱市切国债提供部分保护但非保本。"),
        "s7_track@v1": ("<b>适用环境</b>：产业主线明确、政策与景气共振的结构性行情（AI/半导体/设备更新等），"
                        "或宏观趋势清晰的阶段。<br>"
                        "<b>风险提示</b>：集中持仓1-2只，赛道错误时回撤大；"
                        "GLM政策信号只在实盘可得，回测退化为动量骨架；"
                        "建议作为观察级策略，实盘验证后再给真金白银。"),
        "s26_microcap@v1": ("<b>适用环境</b>：规模因子溢价周期（小盘整体占优、流动性充裕的震荡/慢牛市）。"
                            "<br><b>风险提示</b>：小市值天然波动大、流动性风险高；微盘在极端行情可能连续跌停无法按价成交；"
                            "免费数据池为沪深300内中小盘段，非真正微盘精选，因子暴露偏向'大盘内选中小'。"),
        "s27_dividend_lowvol@v1": ("<b>适用环境</b>：利率下行期或防御市（市场偏弱、资金避险），高股息+低波动提供下行保护。"
                                   "<br><b>风险提示</b>：高股息陷阱——股价暴跌导致股息率虚高；利率上行期高股息股相对吸引力下降；"
                                   "大盘段弹性较低，牛市跑输成长策略。"),
        "s29_smallcap_select@v1": ("<b>适用环境</b>：风险偏好回升、小市值因子溢价的结构性行情。"
                                   "<br><b>风险提示</b>：小盘天然波动大、流动性风险；规模因子在风格切换（如2021核心资产行情）时回撤显著；"
                                   "需配合止损与行业分散。"),
        "s32_roe_quality@v1": ("<b>适用环境</b>：盈利质量被市场定价的慢牛/价值回归行情，高ROE公司溢价持续。"
                               "<br><b>风险提示</b>：质量因子在题材炒作市（偏好高弹性差公司）时跑输；ROE高企可能处周期顶点，反转风险；"
                               "全市场段含小盘，需关注个股流动性。"),
        "s37_earnings_accel@v1": ("<b>适用环境</b>：业绩披露季前后、盈利上修预期升温的行情，成长加速度因子有效。"
                                  "<br><b>风险提示</b>：盈利加速度为同比代理'盈余惊喜'，非真实超预期事件；"
                                  "若基期异常（去年低基数）会失真；成长股估值消化期回撤大。"),
        "s42_sue_enriched@v1": ("<b>适用环境</b>：小盘+动量+52周新高因子共振的强势市场（如2024下半年起的行情），当前样本外表现最佳。"
                               "<br><b>风险提示</b>：SUE因profit_q仅覆盖约376只主板，对实际选股贡献有限；"
                               "52周新高因子在趋势反转初期易追高；全窗口16.6%含2022-23弱势段拖累，近期样本外23%+不保证延续。"),
    }
    er = env_risk.get(sid, "")
    v3_diff = ""
    if sid == "s1_dividend@v3":
        v3_diff = ('<div class="diff"><b>P0升级(2026-07-06)</b>：4因子→7因子(BETA反向/EARNINGS_YIELD/LEVERAGE反向)+macro regime自适应权重。'
                   '扩张期提QUALITY/EARNINGS_YIELD；收缩期自动提高LOW_VOL/BETA/LEVERAGE负向权重增强防御。'
                   'BETA负向=偏好低Beta防御股, LEVERAGE负向=偏好低杠杆公司。'
                   '与v2核心差异：排名法→去极值+标准化+正交化复合(ResVol⊥Beta⊥Size)；取消低波后30%硬截断。</div>')
    elif sid == "s4_smallcap@v2":
        v3_diff = ('<div class="diff"><b>P0升级(2026-07-06)</b>：4因子→7因子(+BETA/EARNINGS_YIELD/QUALITY)+macro regime自适+行业动量倾斜。'
                   '扩张期自动提高MOMENTUM/BETA弹性权重；收缩期提高VALUE/EARNINGS_YIELD/QUALITY防御权重。'
                   '所属行业近60日涨幅前30%获行业动量加分。'
                   '与v1核心差异：20日动量→RSTR 12-1月动量；PB排名→BTOP z分并加ETOP/ROE/QUALITY；新增残差波动帽。</div>')
    enabled = _enabled_strategy_ids()
    retired_badge = ('<span class="risk-badge" style="background:#e5e7eb;color:#6b7280">已下线/归档</span>'
                     if (enabled is not None and sid not in enabled) else "")
    return (f'<div class="strat-block" id="{sid}">'
            f'<details><summary>{meta["name"]}{_scope_badge(sid)}<span class="risk-badge">{meta["risk"]}</span>{retired_badge}</summary>'
            f'<p class="tagline">{html.escape(meta["tagline"])}</p>'
            f'<h3>因子构成</h3>'
            f'<table><thead><tr><th>因子 / 规则</th><th>权重 / 说明</th></tr></thead>'
            f'<tbody>{factor_rows}</tbody></table>'
            f'<p class="note">调仓：{html.escape(meta["rebalance"])} · 适合资金：{html.escape(meta["fit"])}</p>'
            f'{v3_diff}'
            f'<p class="note">{er}</p>'
            f'</details></div>')


def _methodology_risk_model():
    """因子与风险模型章节（id=risk-model）。"""
    return '''<h2 id="risk-model">因子与风险模型</h2>

<h3>因子体系总览</h3>
<p>本项目参考 MSCI Barra 中国A股模型（CNE5/CNE6）与 Axioma Robust Risk Model，
按免费数据现实裁剪，实现 10 个风格因子。每个因子由 1-3 个描述符加权复合。
当前赛马阵容共 6 只纯个股策略（s26/s27/s29/s32/s37/s42），全部在<b>主板硬约束</b>下运行
（引擎层 main_board_universe 强制过滤：主板前缀+非ST/停牌/北交所/科创创业+上市≥2年+市值≥80亿+日均成交≥8000万），
回测与实盘同源、口径一致。各策略因子权重见下方"各策略详解"。
此外另有 3 只<b>全A股候选策略</b>（s53/s54/s55，覆盖主板+科创688+创业300·301+北交）正在回测验证，达标后入赛马，见页底"全A股候选策略（测试中）"。</p>
<table>
<thead><tr><th>因子</th><th>描述符</th><th>主要使用策略 / 方向</th><th>说明</th></tr></thead>
<tbody>
<tr><td>Size（市值 / cap）</td><td>ln(总市值)</td><td>s26/s29/s42 正向主导</td><td>规模因子溢价，小盘段选股核心（s26权重0.50、s29 0.45、s42 0.30）</td></tr>
<tr><td>Momentum（动量）</td><td>12-1月剔除近21日</td><td>全部策略正向</td><td>动量延续，s42权重最高0.20</td></tr>
<tr><td>Value（价值 / value）</td><td>深度价值倾斜</td><td>s27/s29/s32/s42 正向</td><td>低估值加分，红利与小盘策略共有</td></tr>
<tr><td>Valuation（估值分位）</td><td>PE/PB 历史分位</td><td>全部策略正向</td><td>绝对估值廉价度</td></tr>
<tr><td>Low Vol（低波动）</td><td>250日波动率倒序</td><td>全部策略正向过滤</td><td>降波动，s27权重0.25最高</td></tr>
<tr><td>ROE Quality（质量）</td><td>ROE 排名</td><td>s27/s32/s37 正向</td><td>盈利质量，s32权重0.50主导</td></tr>
<tr><td>Dividend（股息）</td><td>股息率排名</td><td>s27 正向0.40</td><td>红利防御，仅大盘段</td></tr>
<tr><td>Growth / Accel（成长加速度）</td><td>净利润同比加速度</td><td>s37 正向0.30</td><td>盈余惊喜代理（国信《超预期投资全攻略》）</td></tr>
<tr><td>SUE（标准化预期外盈利）</td><td>(实际EPS-预期)/标准差</td><td>s42 正向0.10</td><td>国信《基于PEAD效应的超预期因子》；受profit_q仅覆盖~376只主板限制，对实际选股贡献有限</td></tr>
<tr><td>High52（52周新高距离）</td><td>今收/前252日最高-1</td><td>s42 正向0.10</td><td>国信《由创新高个股看市场投资热点》；衡量突破强度</td></tr>
</tbody></table>

<h3>数据处理管线</h3>
<ol>
<li><b>去极值（MAD Winsorize）</b>：对每个因子截面，用中位数绝对偏差（MAD）设定上下界，
越界值截断到边界。公式：bound = median(x) +/- 5 x 1.4826 x MAD(x)。防止个别的极端数值扭曲整体评估。</li>
<li><b>标准化（Z-score）</b>：z = (x - mu_w) / sigma_eq，其中 mu_w 为市值加权均值，sigma_eq 为等权标准差。
处理后，因子分布以市值加权组合为中心(暴露约=0)，等权标准差约=1。</li>
<li><b>正交化（Gram-Schmidt）</b>：按固定顺序(BETA→SIZE→VALUE→...→EARNINGS_YIELD)依次对前一因子做WLS回归取残差。
消除因子间的共线性——例如残差波动与Beta天然相关，正交化后残差波动不再包含Beta已解释的部分。</li>
<li><b>缺失处理</b>：缺失的描述符在复合时按可得权重重归一，标准化后NaN填0（池中性）。</li>
</ol>

<h3>风险模型（Barra 横截面法）</h3>
<p>结构模型：<b>r = Xf + u</b>（个股收益 = 因子暴露 x 因子收益 + 特异收益）。</p>
<ol>
<li><b>暴露矩阵 X</b>：N只股票 x 6个风险因子(size, beta, momentum, resvol, liquidity, btop)的当天暴露值。</li>
<li><b>因子收益估计</b>：对每个交易日，用t-1日暴露对t日个股收益做WLS（权=sqrt(市值)）横截面回归，得因子收益 f_t。</li>
<li><b>因子协方差 F</b>：f_t 的EWMA协方差（半衰期90日），x252年化。</li>
<li><b>特异波动 σ_i</b>：残差 u_i 的EWMA标准差（半衰期42日），xsqrt(252)年化。</li>
<li><b>组合预测波动</b>：σ_p = sqrt( h\'X F X\'h + Σ h_i^2 σ_i^2 )，h=各持仓市值权重。</li>
<li><b>组合暴露</b>：X_p = Σ h_i · z_i。因标准化以市值加权均值为中心，X_p 本身即为主动暴露（相对于市值加权基准）。</li>
</ol>

<h3>对称性说明（暴露怎么看）</h3>
<p>暴露值为正 → 组合在该因子上比市值加权基准偏多（如正Beta = 比市场Beta更高）。<br>
暴露值为负 → 组合在该因子上比基准偏少。<br>
暴露值在[-0.5, 0.5] → 基本中性，无明显偏离。<br>
暴露值>|1| → 显著偏离，需要注意该维度的集中风险。</p>

<h3>投资纪律：单策略回撤 ≤ 年化（铁律）</h3>
<p>用户设定：单策略最大回撤不得超过其年化收益率。风控层 <code>risk.py</code> 对每只策略设 <code>max_dd</code> 上限，并配合跟踪止损（stop_pct）与大盘冻结硬熔断（market_freeze）实现。
当前 6 只策略均满足该约束，例如 s42（回撤14.6% ≤ 年化16.6%）、s27（回撤8.9% ≤ 11.1%）。任何回测违反此律的变体（如 s44/45/46 集中度变体、回撤&gt;年化）一律淘汰、不在看板展示。</p>

<h3>仅主板硬约束</h3>
<p>引擎层 <code>common.main_board_universe()</code> 对所有策略候选池<b>强制过滤</b>：主板前缀（60/000/001/002/003）+ 非ST/停牌/退市/北交所/科创创业 + 上市≥2年 + 市值≥80亿 + 日均成交≥8000万。
用户仅交易A股主板，该约束在回测与实盘<b>结构性一致</b>满足，无需逐策略重复声明。</p>
<p><b>全A股候选策略的例外</b>：s53/s54/s55 使用 <code>pool_index: all_a</code>，经 <code>common.all_a_universe()</code>（仅排停牌/ST/退市/上市&lt;60日 + 流动性门槛）过滤，<b>不卡主板前缀</b>，从而纳入科创板(688)、创业板(300·301)、北交所。这是看板准备新增的「全A股策略」覆盖范围，与现有主板策略形成两档。</p>

<h3>与 Barra CNE6 / Axioma 的差异声明</h3>
<ul>
<li><b>Beta</b>：使用60日窗口(vs CNE6的504日/252日)，因本项目数据覆盖较短(2018年起)，声明短窗差异。</li>
<li><b>动量(RSTR)</b>：不做CNE6的11日滞后平均处理，直接使用252日剔除最近21日的指数衰减累积超额收益。</li>
<li><b>残差波动</b>：无CMRA(月累计收益范围)描述符，仅用日收益标准差DASTD与ATR近似。</li>
<li><b>流动性</b>：仅有STOM(月换手率对数)，无STOQ(季度)/STOA(年度)三个档位。</li>
<li><b>质量</b>：无季频ATO(资产周转率)/GPM(毛利率)数据，仅用年报ROE加杠杆代理。</li>
<li><b>成长</b>：仅净利润增长率，无营收增长率数据。</li>
<li><b>杠杆</b>：无资产负债表数据，用1-1/PB近似资产负债率，属于粗粒度代理。</li>
</ul>

<h3>免费数据局限</h3>
<ul>
<li><b>无分析师预期数据</b>：无法构建Analyst Sentiment、预期EP、预期股息因子；国信《超预期精选组合》"研报标题超预期+分析师上调净利润"事件池在回测中由 event_pool（近期盈余公告且SUE&gt;0）代理，真实文本信号待实盘接 news_engine 验证（故 s52 仅作观察级）。</li>
<li><b>SUE 覆盖受限</b>：标准化预期外盈利依赖 profit_q 表，当前仅覆盖约376只主板，对纯小盘策略实际选股贡献有限（s42 已计入但权重仅0.10）。</li>
<li><b>无季频资产负债表与现金流</b>：无法严格构建Leverage、Investment Quality、Earnings Quality因子。</li>
<li><b>无真实流通股本</b>：换手率为amount x 100 / market_cap反推近似，市值加权也只能用总市值而非流通市值。</li>
<li><b>市场代理为sh510300 ETF</b>：库内无sh000300指数日线，用沪深300ETF后复权收益替代。</li>
<li><b>无真实宏观数据</b>：宏观因子(利率变化、PMI意外)在本模型中占位为0；已通过 macro.py detect_regime() 用PE分位+MA方向做regime自适应补偿。</li>
</ul>

<h3>宏观 Regime 检测</h3>
<p><b>macro.py detect_regime()</b>：基于沪深300 PE十年分位 + MA20/MA60均线方向判断市场状态，用于风控层的<b>大盘冻结硬熔断</b>（market_freeze）——大跌日全策略统一触发、暂停开仓，属安全网而非选股信号。</p>
<ul>
<li><b>扩张(expansion)</b>：PE分位≤50% 且 MA20>MA60（估值合理+趋势向上）→ 正常开仓。</li>
<li><b>收缩(contraction)</b>：PE分位>70% 或 MA20<MA60且PE>50%（高估或下行）→ 触发大盘冻结防御，降低新开仓。</li>
<li><b>中性(neutral)</b>：其余情况 → 使用默认因子权重。</li>
</ul>
<p>注：当前 6 只参赛策略均设 <code>regime_downsize: false</code>（不启用"弱势减仓"软防御），避免重蹈 s43 因防御过度被压到11%的覆辙；集中防御仅保留 risk 层大盘冻结硬熔断。</p>

<h3>行业动量倾斜</h3>
<p><b>macro.py industry_momentum()</b>：计算申万31行业近60日等权涨幅排名。该信号曾用于早期策略的行业景气度倾斜；当前 6 只策略以个股多因子排名为主，行业动量作为 mf_core 的 ind_mom 候选因子接入（s50 行业动量轮动实测未达阈值，作观察级）。</p>
'''


def generate_methodology(out_path=None):
    """生成独立方法论页 docs/methodology.html。"""
    nav = ('<nav><a href="index.html">📊 策略看板</a>'
           '<a href="methodology.html">📐 策略方法论</a>'
           '<a href="methodology.html#risk-model">📈 因子风险模型</a>'
           '</nav>')
    toc = _methodology_toc()
    enabled = _enabled_strategy_ids()
    all_sids = sorted(STRAT_META.keys())
    ordered_sids = [s for s in all_sids if enabled is None or s in enabled]  # 仅展示启用策略,已下线不展示
    strat_blocks = ""
    for sid in ordered_sids:
        strat_blocks += _methodology_strat_block(sid)
    # v3 的策略(如果 registry 有但 STRAT_META 还没有，加占位)
    risk = _methodology_risk_model()
    today = util.today_str()
    body = (f"{nav}<h1>📐 策略方法论</h1>"
            f'<p class="note">生成 {today} · 文档随策略版本同步更新 · 所有分析基于免费公开数据</p>'
            f"{toc}"
            f'<h2>各策略详解</h2>{strat_blocks}'
            f"{risk}"
            f'{_methodology_candidates_section()}'
            f'<div class="foot"><b>免责</b>：本页由 report_html.py 自动生成；'
            f'模拟/历史表现不代表未来，不构成投资建议，请仅用可承受损失的资金。因子模型参考 MSCI Barra CNE5/CNE6 公开文献与'
            f'Axioma V4 Handbook，按本项目免费数据现实裁剪。</div>')
    doc = (f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>策略方法论 - A股模拟跟单</title>{_METHODOLOGY_STYLE}</head>"
           f"<body><div class='wrap'>{body}</div></body></html>")
    out_path = out_path or (conf.ROOT / "docs" / "methodology.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return str(out_path)


def generate_trades(conn, out_path=None, cap=800):
    """历史交易页:实盘成交(2026-07-31 起)置顶展开 + 各策略回测成交折叠靠后。买红卖绿。"""
    sids = ["s26_microcap@v1", "s29_smallcap_select@v1", "s32_roe_quality@v1",
            "s37_earnings_accel@v1", "s42_sue_enriched@v1", "s53_all_a_momentum_smallcap@v1"]
    live_rows = []
    live_csv = conf.STATE_DIR / "trade_log.csv"
    if live_csv.exists():
        with open(live_csv, encoding="utf-8") as f:
            live_rows = [r for r in csv.DictReader(f) if r.get("status") in ("filled", "cut_liquidity")]

    def table(rows, truncate=True):
        body = ""
        for r in rows[:cap]:
            is_sell = r.get("side") == "sell"
            side = "卖出" if is_sell else "买入"
            cls = "sell" if is_sell else "buy"
            nm = ctx_name(conn, r.get("code", "")) if conn else util.bare(r.get("code", ""))
            real = r.get("real_price") or ""
            reason = r.get("reason", "") or ""
            reason = html.escape(reason[:44]) if truncate else html.escape(reason)
            body += (f"<tr class='{cls}'><td>{r.get('trade_date','')}</td><td>{side}</td>"
                     f"<td>{util.bare(r.get('code',''))} {nm}</td><td>{r.get('shares','')}</td>"
                     f"<td>{r.get('sim_price','')}</td><td>{real}</td>"
                     f"<td class='rs'>{reason}</td></tr>")
        head = ("<table class='t'><tr><th>日期</th><th>方向</th><th>标的</th><th>股数</th>"
                "<th>模拟价</th><th>实盘价</th><th>理由</th></tr>")
        return head + body + "</table>"

    sections = ""
    if live_rows:
        live_rows.sort(key=lambda r: r.get("trade_date", ""), reverse=True)
        live_start = min((r.get("trade_date", "") for r in live_rows), default=LIVE_START)
        sections += (f"<details open><summary>🔴 实盘模拟成交 · 全部（{live_start} 起，共{len(live_rows)}笔）</summary>"
                     f"{table(live_rows, truncate=False)}</details>")
        # 按策略筛选(纯 HTML 分组,卡E):每个有成交的策略一个可折叠子块
        by_sid = {}
        for r in live_rows:
            by_sid.setdefault(r.get("strategy_id", "?"), []).append(r)
        if len(by_sid) > 1:
            sections += "<div class='subhead'>按策略筛选</div>"
            for sid in sorted(by_sid):
                rows = by_sid[sid]
                sections += (f"<details><summary>{_cn(sid)}（{len(rows)}笔）</summary>"
                             f"{table(rows, truncate=False)}</details>")
    else:
        sections += (f"<details open><summary>🔴 实盘模拟成交（{LIVE_START} 起）</summary>"
                     f"<p class='empty'>实盘模拟期尚未产生成交（首个交易日为 {LIVE_START}）。赛马自 {LIVE_START} 起跑，首次成交将在之后的交易日产生，此处按“全部 + 按策略筛选”分组展示。</p></details>")
    for sid in sids:
        p = conf.REPORTS_DIR / f"{sid.replace('@','_at_')}_trades.csv"
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("status") in ("filled", "cut_liquidity")]
        rows.sort(key=lambda r: r.get("trade_date", ""), reverse=True)
        note = f"共{len(rows)}笔" + (f"，显示最近{cap}笔" if len(rows) > cap else "")
        sections += (f"<details><summary>{_cn(sid)} · 历史回放成交（2022→今，仅供参考）{note}</summary>{table(rows)}</details>")

    doc = (f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>历史交易记录</title>{_TRADES_STYLE}</head><body><div class='wrap'>"
           f"<h1>📜 历史交易记录</h1>"
           f"<div class='sub'>生成 {util.today_str()} · <a href='index.html'>← 返回看板</a> · "
           f"回测按次日开盘价+真实费用滑点模拟成交 · 买入红 / 卖出绿</div>"
           f"<div class='tw'>{sections}</div>"
           f"<div class='foot'>实盘区为 {LIVE_START} 起真实跟踪的模拟成交；历史回放区为 2022 年至今回测(含费用/滑点/T+1)，仅供参考。"
           f"实盘价一列由你在 Streamlit 看板回填。完整明细见仓库 reports/*_trades.csv。</div>"
           f"</div></body></html>")
    out_path = out_path or (conf.ROOT / "docs" / "trades.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return str(out_path)


# ---------------- 样式 / 脚本 ----------------
_STYLE = """<style>
:root{--bg:#f6f7f9;--fg:#1f2937;--mut:#6b7280;--card:#fff;--line:#e5e7eb;--up:#d92b2b;--down:#0a9e6b}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--fg);font-size:15px;line-height:1.5}
.wrap{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}.sub{color:var(--mut);font-size:13px;margin-bottom:12px}
.sec{margin:22px 0 8px;font-size:16px;font-weight:600}.sec a{color:#2563eb;text-decoration:none}
.banner{padding:10px 12px;border-radius:8px;font-size:13.5px;font-weight:600;margin:10px 0}
.banner.yellow{background:#fef9c3;color:#854d0e;border:1px solid #fde68a}
.banner.red{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
.scope-badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;margin-left:6px;font-weight:600;vertical-align:middle}
.scope-mb{background:#dbeafe;color:#1d4ed8}
.scope-alla{background:#ffedd5;color:#c2410c}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;font-size:13.5px}
th,td{padding:8px 7px;text-align:center;border-bottom:1px solid var(--line)}
th{background:#f0f2f5;color:var(--mut);font-weight:600}td.l,th:first-child{text-align:left}
.ov td.ref{color:var(--mut);font-size:12px}
.ops-head{display:flex;justify-content:space-between;align-items:center;margin:6px 0}
.ops-head span{font-size:13.5px;color:var(--mut)}
.copybtn{background:#2563eb;color:#fff;border:none;border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer}
.op{padding:9px 12px;border-radius:9px;margin:6px 0;font-size:14px}
.op.buy{background:#fef2f2;color:#991b1b}.op.sell{background:#ecfdf5;color:#065f46}
.op.none{background:#f3f4f6;color:var(--mut)}
.op .op-hd{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:2px}
.op .chip{display:inline-block;background:#fff;border:1px solid rgba(0,0,0,.18);color:#1f2937;font-size:11.5px;font-weight:700;padding:1px 9px;border-radius:999px;white-space:nowrap}
.op .q{display:block;font-size:13px;color:#374151;margin:3px 0}
.op .reason{display:block;color:var(--mut);font-size:12.5px}
.op .band{font-size:11.5px;color:#9a3412;margin-top:3px}
.op .stale{color:var(--mut);font-size:11.5px}.op .warn{color:#b91c1c;font-weight:600}
.op-note{color:var(--mut);font-size:12px;margin:4px 2px}
.card{background:var(--card);border-radius:12px;padding:14px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:6px}
.card-h b{font-size:15.5px}.card-h .risk{color:#a16207;font-size:12px;margin-left:auto}
.news-bar{display:flex;flex-wrap:wrap;gap:6px;padding:8px 0}
.news-tag{display:inline-block;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;color:#fff}
.news-tag.positive{background:#0a9e6b}.news-tag.negative{background:#d92b2b}
.flash-list{display:flex;flex-direction:column;gap:6px;padding:6px 0;max-height:440px;overflow:auto}
.flash-item{display:flex;gap:8px;align-items:baseline;font-size:13px;line-height:1.5}
.flash-time{color:var(--mut);font-size:11.5px;flex:0 0 auto;width:62px;font-variant-numeric:tabular-nums}
.flash-src{flex:0 0 auto;width:30px;font-size:11.5px;font-weight:700}
.flash-title{color:var(--fg);flex:1 1 auto}
.flash-more{margin-top:2px}
.flash-more>summary{cursor:pointer;color:#2563eb;font-size:12.5px;padding:5px 2px;user-select:none;list-style:none}
.flash-more>summary::-webkit-details-marker{display:none}
.flash-more>summary:hover{text-decoration:underline}
.flash-more .flash-list{max-height:520px}
.sec-sub{font-size:11.5px;font-weight:400;color:var(--mut);margin-left:4px}
.rg-card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:6px 0}
.rg-head{display:flex;align-items:center;flex-wrap:wrap;gap:10px}
.rg-badge{color:#fff;font-weight:700;font-size:14px;padding:4px 12px;border-radius:8px}
.rg-metrics{color:var(--mut);font-size:12.5px}
.rg-benches{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px;font-size:12.5px}
.rg-sectors{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;font-size:12.5px;align-items:center}
.rg-lbl{color:var(--mut);font-weight:600;margin-right:2px}
.rg-sec,.rg-bench{background:#f3f4f6;border-radius:6px;padding:2px 8px}
.rg-note{color:var(--mut);font-size:12px;margin-top:8px}
.card-h .stat{font-size:12px;color:var(--mut)}
.tagline{font-size:13px;color:#374151;margin:4px 0 8px}
.fx{margin:6px 0;font-size:12.5px}.fx th{font-size:12px}
.rb{font-size:12px;color:var(--mut);margin:6px 0}
.ds{font-size:11.5px;color:#2563a8;background:#eef4fb;border-left:3px solid #3b82f6;padding:5px 8px;margin:8px 0;border-radius:4px;line-height:1.5}
details{margin:8px 0}summary{cursor:pointer;font-size:13.5px;color:#334155;padding:4px 0}
.sub2{font-size:13px;font-weight:600;color:#334155;margin:10px 0 4px}
.pos{font-size:12.5px}.pos .why td{text-align:left;color:var(--mut);font-size:11.5px;background:#fafafa;padding:4px 8px}
.pos .sum td{font-weight:600;background:#f8fafc}
.pos-empty{color:var(--mut);font-size:13px;padding:10px;background:#f8fafc;border-radius:8px}
/* 大盘指数卡片（东方财富风） */
.idx-cards{display:flex;gap:10px;margin:8px 0 4px;flex-wrap:wrap}
.idx-card{flex:1;min-width:180px;background:linear-gradient(135deg,#f8fafc 0%,#fff 100%);
border-radius:12px;padding:14px 16px;border:1px solid var(--line);text-align:center}
.idx-card.up{border-left:3px solid var(--up)}.idx-card.down{border-left:3px solid var(--down)}
.idx-card.up .idx-price,.idx-card.up .idx-chg{color:var(--up)}
.idx-card.down .idx-price,.idx-card.down .idx-chg{color:var(--down)}
.idx-name{font-size:13px;color:var(--mut);font-weight:600;margin-bottom:2px}
.idx-code{font-size:10px;color:var(--mut);margin-left:4px;opacity:0.7}
.idx-price{font-size:24px;font-weight:700;margin:4px 0;letter-spacing:-0.5px}
.idx-chg{font-size:13px;display:flex;justify-content:center;gap:8px}
.idx-chg-val{font-weight:600}.idx-chg-pct{font-weight:600}
.idx-date{width:100%;text-align:center;color:var(--mut);font-size:11px;margin-top:2px}
.bt{background:#f0f7ff;color:#1e40af;font-size:12px;padding:6px 10px;border-radius:8px;margin:6px 0}
.foot{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}
.foot b{color:#374151}.empty{color:var(--mut);text-align:center;padding:40px}
/* 顶部导航 */
nav{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
nav a{display:inline-block;padding:6px 14px;background:#2563eb;color:#fff;border-radius:8px;
text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
nav a:hover{background:#1d4ed8}
/* 折叠区域增强 */
details.strategy-logic,details.factor-exposure,details.usage-instructions{margin:8px 0}
details.strategy-logic summary,details.factor-exposure summary,details.usage-instructions summary{cursor:pointer;
padding:8px 12px;background:rgba(0,0,0,0.03);border-radius:4px;user-select:none}
details.strategy-logic summary:hover,details.factor-exposure summary:hover,
details.usage-instructions summary:hover{background:rgba(0,0,0,0.06)}
details[open].strategy-logic summary,details[open].factor-exposure summary,
details[open].usage-instructions summary{margin-bottom:8px;border-bottom:1px solid var(--line)}
/* 策略逻辑链接 */
.logic-link{display:block;margin-top:8px;font-size:12.5px;color:#2563eb;text-decoration:none}
/* 因子暴露 CSS 条形图 */
.exp-vol{font-size:12.5px;color:var(--mut);margin:6px 0}
.exp-chart-container{width:100%;max-width:400px;height:200px;margin:8px auto;position:relative}
.exp-no-data{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:var(--mut);font-size:13px}
.exp-bar-row{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:12px}
.exp-label{width:100px;text-align:right;color:var(--mut);flex-shrink:0;font-size:11.5px}
.exp-bar-wrap{flex:1;height:14px;background:#f0f2f5;border-radius:7px;position:relative;overflow:hidden}
.exp-zero-line{position:absolute;left:50%;top:0;width:1px;height:100%;background:rgba(0,0,0,0.15)}
.exp-fill{position:absolute;top:0;height:100%;border-radius:7px}
.exp-fill.exp-pos{background:#3b82f6}
.exp-fill.exp-neg{background:#ef4444}
.exp-val{width:42px;text-align:left;font-size:11.5px;font-weight:600;flex-shrink:0}
.exp-note{color:var(--mut);font-size:11.5px;margin-top:6px}
.exp-note a{color:#2563eb;text-decoration:none}
.exposure-table{font-size:12px;margin-top:8px}
.exposure-table th,.exposure-table td{padding:4px 8px}
.exp-fallback{margin-top:6px}
.exp-fallback-note{padding:8px 10px;border:1px dashed #e0a3a3;border-radius:8px;background:#fff8f8;color:#b3432f;font-size:12px;line-height:1.6}
.chart-fallback-banner{margin:10px 0;padding:10px 14px;border:1px solid #e0a3a3;border-radius:8px;background:#fff8f8;color:#b3432f;font-weight:600;font-size:13px}
</style>"""

_FOOTER = """<div class="foot">
<details class="usage-instructions"><summary>📖 使用说明（点击展开）</summary>
<b>怎么用</b>：每天 18:00 前后微信收到推送，次日开盘按『操作计划』的价格带手动跟单（每条已标注所属策略）；没收到心跳=系统故障，当天别跟单。<br>
<b>观察期纪律</b>：第0-2周只看不投；满季度后若赛马正常，5万低风险参考配比 = ETF轮动25%+大盘价值25%+小市值多因子20%+行业轮动15%+现金15%（S7赛道旗舰仅观察，待实盘验证）。任何策略熔断→该部分转现金等复核。<br>
<b>数据来源</b>：腾讯/新浪/东财 免费源为主，baostock/yfinance为辅，每交易日17:40自动更新；页面顶部横幅提示数据新鲜度。<br>
<b>免责</b>：本页由 report_html.py 自动生成，零外部依赖可离线打开；模拟/历史表现不代表未来，不构成投资建议，请仅用可承受损失的资金。
</details>
</div>"""

# 实时价渐进增强(腾讯行情 qt.gtimg.cn):<script>跨域取数,失败/超时3s静默回昨收。全程 try/catch,无 Promise 悬挂。
_LIVE_JS = """<script>
(function(){
  try{
    // ── 大盘指数实时行情（腾讯 qt.gtimg.cn）──
    var idxMap=[{qq:'sh000001',id:'sh_000001'},{qq:'sz399001',id:'sz_399001'},{qq:'sz399006',id:'sz_399006'}];
    function idxFmt(v){return (v&&v!='0.000'&&v!='0.00')?parseFloat(v):null;}
    function updateIdx(){
      for(var i=0;i<idxMap.length;i++){
        var v=window['v_'+idxMap[i].qq];
        if(!v) continue;
        var f=v.split('~'); var cur=idxFmt(f[3]); var prev=idxFmt(f[4]);
        if(!cur||!prev) continue;
        var chg=cur-prev; var chgPct=prev>0?(chg/prev*100):0;
        var isUp=chg>=0; var col=isUp?'var(--up)':'var(--down)'; var sign=isUp?'+':'';
        var pr=document.getElementById('idx_price_'+idxMap[i].id);
        var cv=document.getElementById('idx_chg_val_'+idxMap[i].id);
        var cp=document.getElementById('idx_chg_pct_'+idxMap[i].id);
        var ca=document.getElementById('idx_'+idxMap[i].id);
        if(pr){pr.textContent=cur.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});pr.style.color=col;}
        if(cv){cv.textContent=sign+chg.toFixed(2);cv.style.color=col;}
        if(cp){cp.textContent=sign+chgPct.toFixed(2)+'%';cp.style.color=col;}
        if(ca){ca.className=ca.className.replace(/\bup\b|\bdown\b/g,'')+' '+(isUp?'up':'down');}
      }
      var de=document.getElementById('idx_date'); if(de) de.textContent='实时行情（腾讯）';
    }
    function refreshIdx(){
      var s=document.createElement('script');
      s.src='https://qt.gtimg.cn/q='+idxMap.map(function(x){return x.qq;}).join(',');
      s.charset='gbk';
      s.onload=function(){updateIdx();};
      document.head.appendChild(s);
    }
    // 首次加载:3秒超时兜底
    var it=setTimeout(function(){updateIdx();},3000);
    var is=document.createElement('script');
    is.src='https://qt.gtimg.cn/q='+idxMap.map(function(x){return x.qq;}).join(',');
    is.charset='gbk';
    is.onload=function(){clearTimeout(it);updateIdx();};
    is.onerror=function(){clearTimeout(it);};
    document.head.appendChild(is);
    // 每10秒自动刷新指数行情
    setInterval(refreshIdx,10000);
  }catch(e){}
})();
(function(){
  try{
    // ── 个股/ETF 实时价 —— 操作计划区 + 持仓浮盈浮亏 ──
    var ops=document.querySelectorAll('.op[data-code]');
    var posRows=document.querySelectorAll('tr[data-code]');
    var set={}, codes=[];
    ops.forEach(function(el){var c=el.getAttribute('data-code'); if(!set[c]){set[c]=1;codes.push(c);}});
    posRows.forEach(function(el){var c=el.getAttribute('data-code'); if(!set[c]){set[c]=1;codes.push(c);}});
    if(!codes.length) return;
    var done=false;
    function mark(){ops.forEach(function(el){var q=el.querySelector('.q'); if(q&&q.innerHTML.indexOf('昨收参考')<0){q.innerHTML+=" <span class='stale'>(昨收参考)</span>";}});_refreshPositions();}
    function ft(t){return (t&&t.length>=12)?(t.substr(8,2)+':'+t.substr(10,2)):'';}
    function _refreshPositions(){
      var posRows=document.querySelectorAll('tr[data-code]');
      if(!posRows.length) return;
      posRows.forEach(function(tr){try{
        var code=tr.getAttribute('data-code');
        var v=window['v_'+code];
        if(!v) return;
        var f=v.split('~'); var cur=parseFloat(f[3]); var prev=parseFloat(f[4]);
        if(!(cur>0)||!(prev>0)) return;
        var avg=parseFloat(tr.getAttribute('data-avg'))||0;
        var shares=parseFloat(tr.getAttribute('data-shares'))||0;
        var lastPrev=parseFloat(tr.getAttribute('data-prev'))||prev;
        var dailyChg=cur/lastPrev-1;
        var dailyPnl=shares*cur-shares*lastPrev;
        var dailyCol=dailyChg>=0?'var(--up)':'var(--down)';
        var dailySg=dailyChg>=0?'+':'';
        var cumChg=avg>0?cur/avg:1;
        var curTd=tr.querySelector('.cur'); if(curTd){curTd.textContent=cur.toFixed(3);curTd.style.color=dailyCol;}
        var mvTd=tr.querySelector('.mv'); if(mvTd){mvTd.textContent=Math.round(shares*cur).toLocaleString();}
        var dpnlTd=tr.querySelector('.dpnl');
        if(dpnlTd){
          var dTxt=dailySg+(dailyChg*100).toFixed(2)+'%';
          if(dailyPnl!==0){dTxt+=' ('+(dailyPnl>0?'+':'')+Math.round(dailyPnl).toLocaleString()+')';}
          dpnlTd.textContent=dTxt;dpnlTd.style.color=dailyCol;
        }
        var cpnlTd=tr.querySelector('.cpnl');
        if(cpnlTd){
          var cSg=(cumChg-1)>=0?'+':'';
          cpnlTd.textContent=cSg+((cumChg-1)*100).toFixed(1)+'%';
          cpnlTd.style.color=Math.abs(cumChg-1)<0.001?'inherit':dailyCol;
        }
      }catch(e){}});
    }
    function apply(){
      ops.forEach(function(el){try{
        var code=el.getAttribute('data-code'); var v=window['v_'+code];
        var q=el.querySelector('.q'); if(!v){return;}
        var f=v.split('~'); var cur=parseFloat(f[3]); var prev=parseFloat(f[4]);
        if(!(cur>0)){return;}
        var chg=prev>0?(cur/prev-1):0; var col=chg>=0?'var(--up)':'var(--down)'; var sg=chg>=0?'+':'';
        var line="实时价 "+cur.toFixed(3)+" <span style='color:"+col+"'>"+sg+(chg*100).toFixed(2)+"%</span> "+ft(f[30]);
        if(el.getAttribute('data-side')==='buy'){
          var amt=parseFloat(el.getAttribute('data-amount'))||0;
          var ref=parseFloat(el.getAttribute('data-ref'))||prev;
          var sh=Math.floor(amt/cur/100)*100;
          line+=" · 约"+sh+"股 ≈"+Math.round(sh*cur)+"元";
          if(ref>0 && cur>ref*1.02){line+=" <span class='warn'>⚠已超跟单价格带，建议减半或放弃</span>";}
        }
        if(q){q.innerHTML=line;}
      }catch(e){}});
      _refreshPositions();
    }
    var timer=setTimeout(function(){if(!done){done=true;mark();}},3000);
    var s=document.createElement('script');
    s.src='https://qt.gtimg.cn/q='+codes.join(',');
    s.charset='gbk';
    s.onload=function(){if(done)return;done=true;clearTimeout(timer);apply();};
    s.onerror=function(){if(done)return;done=true;clearTimeout(timer);mark();};
    document.head.appendChild(s);
  }catch(e){}
})();
</script>"""

_TRADES_STYLE = """<style>
:root{--bg:#f6f7f9;--fg:#1f2937;--mut:#6b7280;--card:#fff;--line:#e5e7eb;--up:#d92b2b;--down:#0a9e6b}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--fg);font-size:14px;line-height:1.5}
.wrap{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}.sub{color:var(--mut);font-size:13px;margin-bottom:14px}a{color:#2563eb;text-decoration:none}
details{background:var(--card);border-radius:10px;margin:10px 0;padding:6px 12px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
summary{cursor:pointer;font-weight:600;padding:8px 0}
.t{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0}
.t th,.t td{padding:6px 5px;border-bottom:1px solid var(--line);text-align:center}
.t th{background:#f0f2f5;color:var(--mut)}
.t td.rs{white-space:normal;text-align:left;color:var(--mut);min-width:120px}
.t tr.buy td:nth-child(2){color:var(--up);font-weight:600}.t tr.sell td:nth-child(2){color:var(--down);font-weight:600}
.tw{overflow-x:auto}.empty{color:var(--mut);text-align:center;padding:24px}
.subhead{margin:16px 2px 6px;font-size:14px;font-weight:600;color:#334155}
.foot{color:var(--mut);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}
</style>"""


if __name__ == "__main__":
    print("已生成:", generate())
