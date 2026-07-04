# A股多策略模拟跟单系统(astock-paper)

**它做什么**:5个不同风险的策略各自用虚拟账户"赛马";每天18:00把次日买卖计划推送到你微信;你手动跟单;网页看板(手机/电脑)对比各策略真实表现;硬性风控层限制最大亏损。
**它不做什么**:不接实盘接口、不保证收益。它通过 分散+硬熔断+真实成本建模+先模拟后实盘 来提高你长期赚钱的概率、控制亏损深度。

---

## 快速开始(一次性,约30分钟)

1. 注册 https://pushplus.plus (微信扫码),复制 token。
2. 准备一个邮箱,开启 SMTP 服务,拿到授权码(推送失败时的备用通知)。
3. 新建 **私有** GitHub 仓库,上传本项目全部文件。
4. 仓库 Settings → Secrets and variables → Actions,添加 Secret:
   `PUSHPLUS_TOKEN` / `SMTP_AUTH_CODE`(必填);`ANTHROPIC_API_KEY`(选填,仅启用大模型消息档时)
5. 复制 `config.example.yaml` 为 `config.yaml`,按需改:
   - `capital`: 你计划将来实盘投入的金额(模拟按此建账,并据此自动压缩持仓数/剔碎单)
   - `smtp.user`: 你的邮箱地址
   - `strategies`: 开哪些策略(资金<3万建议只开 s2/s5,见下表)
6. **先建历史库**:Actions 页 → `backfill` → Run workflow(约15–40分钟,构建 ETF+沪深300 历史)。
   仅跑 S2/S5(纯ETF)可跳过——`daily` 首跑会自动补 ETF 数据。
7. Actions 页 → `daily` → Run workflow 手动跑一次。**微信收到"心跳"消息 = 部署成功。**
8. **看板(二选一,国内首选①)**:
   - ① 静态看板(零依赖、不翻墙):Settings → Pages → Source 选 `main` 分支 `/docs` 目录,
     保存后访问 `https://<用户名>.github.io/<仓库名>/`。每天 daily 跑完自动刷新。手机加书签即可。
   - ② Streamlit(交互强,可回填实盘价,但国内访问可能不稳):https://share.streamlit.io 部署 `dashboard.py`。

## 每天怎么用(约5分钟)

| 时间 | 你做什么 |
|------|---------|
| 18:00 | 微信收到推送。「今日无操作」→ 结束;有操作 → 记下明日计划 |
| 次日开盘后 | 按推送内容,以开盘价附近手动下单(S2策略每周最多调仓1次,多数日子无事) |
| 09:35 | 收到模拟成交回报,顺手在看板"操作流水"页回填你的实盘成交价 |
| 任何一天 | **没收到心跳 = 系统故障,当天不要跟单**,查看板"系统健康"页 |

## 什么时候投钱、投多少(纪律表,建议打印)

| 阶段 | 动作 |
|------|------|
| 第0–2周 | 系统上线,只观察,不投一分钱 |
| 第2–8周 | 赛马淘汰:触发15%熔断者出局;周操作>3次 或 跟单摩擦年化>2% 者出局(不可跟) |
| 满1季度 | 存活者中「跑赢自身基准 且 Calmar≥0.8」取最高者,以 **≤总资产10%** 实盘跟单 |
| 满2季度 | 实盘无熔断、摩擦可控 → 可加至20–30%;**任何策略触发熔断 → 实盘同步清仓,等人工复核** |
| 永远 | 每次是否跟单由你决定;偏离方案时在看板"决策日志"记一笔,月底复盘纪律与直觉谁赚钱 |


## 资金分档建议(先看这张表再开策略)

| 你的资金 | 建议开启 | 原因 |
|---|---|---|
| <3万 | 仅 S2+S5(纯ETF) | 个股策略持仓数太少、单票风险过大;ETF滑点最小 |
| 3–10万 | S2/S5 + 至多1个个股策略(推荐S1或S3) | S4小市值此档流动性冲击最大,谨慎 |
| >10万 | 全部可选 | 仍受流动性截断保护 |

系统会按你 config 里的真实资金自动:压缩个股策略持仓数、跳过低于5000元的碎单、剔除买不起一手的高价股、对大单按成交概率打滑点/部分成交——模拟曲线故意保守,实盘跟单才不会失望。

## 风险可控的五个来源
硬熔断(单策略回撤触15%即全清仓降险+告警)/ 仓位纪律(10%起步)/ 成本真实(费用·滑点·冲击·T+1·部分成交全建模)/ 策略分散(红利·动量·趋势·小市值·网格,低相关)/ 入池五关+蒙特卡洛验证(样本外·前推·参数扰动·成本加压·分牛熊·5%分位下界)。
> 熔断说明:触发后**自动全清仓并告警,次日重置基准继续参赛**(不是永久踢出;是否弃用由你看告警决定)。因此持续熊市中累计回撤可能>15%——这是"持续参赛"与"硬截断"的权衡,报告如实披露。

## 已实现策略(5个,各带 reports/ 五关报告)
| 代码 | 名称 | 频率 | 池 | 备注 |
|---|---|---|---|---|
| s2_etf@v1 | ETF动量轮动 | 周 | 6只ETF | MVP,已冻结入赛 |
| s1_dividend@v1 | 红利低波 | 月 | 沪深300 | 股息率+低波 |
| s3_ma_trend@v1 | 双均线趋势 | 日 | 沪深300 | MA20上穿MA60+放量 |
| s4_smallcap@v1 | 小市值多因子 | 月 | 沪深300* | *数据可行性:沪深300内取最小市值演示;真小盘改 POOL_INDEX 为中证1000并重跑 backfill |
| s5_grid@v1 | 大盘网格 | 日 | 沪深300ETF | PE分位择时+5档网格 |

## 部署自检(照做即可确认每一环)
1. **本地测试全绿**:`python tests/test_m1.py && python tests/test_m2.py && python tests/test_m4.py && python tests/test_risk.py && python tests/test_news.py`
2. **回填成功**:backfill 工作流日志末尾出现"回填完成";或本地 `python backfill.py`。
3. **主流程通**:`daily` 工作流手动跑 → 微信收到心跳。无通道时本地 `python run_daily.py --only s2_etf@v1` 会打印消息。
4. **看板出图**:Pages 网址能打开、有净值曲线;或本地 `python report_html.py` 后打开 `docs/index.html`。
5. **回测/报告**:`python backtest.py report s2_etf@v1`、`python validate.py s2_etf@v1` 生成 reports/*.md。

## 目录说明
```
run_daily.py / run_intraday.py   每日主流程 / 盘中扫描(入口)
engine.py risk.py                撮合引擎 / 风控层
data_adapter.py data.py fundamental.py  数据层(唯一碰 akshare/baostock)
news_adapter.py news_engine.py news_llm.py  消息面(保险层,永不阻断主流程)
strategies/  s1..s5 + base(冻结)+ common
backtest.py validate.py          历史回测五关 / 蒙特卡洛稳健性
notify.py                        PushPlus主+SMTP备 推送
dashboard.py report_html.py      Streamlit看板 / 零依赖静态看板
backfill.py                      历史数据回填(首次/重建)
conf.py models.py(冻结) schema.sql(冻结) registry.yaml config.yaml
.github/workflows/  daily / intraday / backfill
tests/  test_m1 m2 m4 risk news
docs/  SPEC*.md 规格书 + index.html(生成的看板)
```
免责:本系统仅输出模拟信号,不构成投资建议;历史与模拟表现不代表未来;请仅用可承受损失的资金。
