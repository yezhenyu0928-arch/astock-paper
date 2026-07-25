# SPEC 附录:消息面情绪层(News Layer)v6

> 新增第③.5层:位于策略层与风控层之间。**设计铁律(写死,实现模型不得违背):**
> 1. 消息**永远不直接产生买入信号**——追消息买入是散户主要亏损来源,本层只做三件事:降低敞口、冻结开仓、持仓个股黑天鹅预警/强卖;
> 2. "实时"的诚实定义:免费架构下 = 每日盘后必扫 + 盘中三次(10:00/13:30/14:45)增量扫(Actions 定时,分钟级误差可接受,因为输出的是提醒而非抢单);真·秒级实时属于二期(需常驻服务器),一期不做;
> 3. 消息分析结论必须**可解释、可回溯**:每个信号落库存证,看板可查"当天为什么降仓"。

---

## N1 数据源(news_adapter.py,新增文件,与 data_adapter 同级)

【函数规格】
```python
def fetch_flash(since_ts) -> pd.DataFrame     # 快讯流:ts,title,content,source
def fetch_stock_news(code, days=3) -> pd.DataFrame   # 个股新闻
def fetch_announcements(codes, date) -> pd.DataFrame # 交易所公告:code,ts,title,type
def fetch_macro_calendar(date) -> pd.DataFrame       # 当日宏观事件:议息/CPI/PMI等
```
AkShare 候选接口(实现时按当前版本核对,失效换源并告警):
| 需求 | 候选接口 |
|------|---------|
| 财联社电报快讯 | stock_info_global_cls |
| 全球/央视要闻 | stock_info_global_em / news_cctv |
| 个股新闻 | stock_news_em |
| 巨潮公告 | stock_notice_report / stock_zh_a_disclosure_report_cninfo |
| 宏观日历 | macro_china_* 系列 / tool_* 经济日历 |
落库表:
```sql
CREATE TABLE news_raw (
  id TEXT PRIMARY KEY,            -- hash(source+ts+title) 去重
  ts TEXT, source TEXT, code TEXT, -- code 可空=宏观
  title TEXT, content TEXT);
CREATE TABLE news_signal (
  signal_date TEXT, scope TEXT,    -- 'market' 或个股code
  score REAL,                      -- 市场:-2..+2;个股:0=正常 -1=警示 -2=黑天鹅
  level TEXT,                      -- L0/L1 产生
  evidence TEXT,                   -- 触发的标题列表(JSON),可解释性
  PRIMARY KEY(signal_date, scope));
```

## N2 分析引擎(news_engine.py)双档,免费可用、有钱更强

**L0 规则档(默认,零成本)**:关键词加权打分。
- 市场级负面词表(权重-1~-2):`印花税上调|注册制暂停→重大政策` `地缘冲突升级|制裁` `流动性收紧|超预期加息` `汇率破位|大幅贬值` `熔断|千股跌停`;正面词表(+1~+2):`降准|降息|超预期宽松` `平准基金|汇金增持` `重大利好政策落地`。
- 个股级黑天鹅词表(score=-2):`立案调查|留置|失联` `财务造假|无法表示意见审计` `债务违约|资金占用` `退市风险警示`;警示词表(-1):`大股东减持计划|质押平仓风险|业绩预亏|商誉减值`。
- 当日市场分 = clip(Σ命中权重, -2, +2);evidence 记录命中标题。

**L1 大模型档(可选,config 开关 + ANTHROPIC_API_KEY Secret)**:把当日快讯标题池(截断至~200条)交给低成本模型,产出严格 JSON:`{"market_score": -2..2, "top_risks": [...], "top_positives": [...], "holdings_flags": [{"code":..., "score":-1|-2, "why":...}]}`。Prompt 模板写在 prompts/news_daily.txt,要求"只评估已发生事实,不预测,不荐股"。L1 结果与 L0 取**更保守者**(市场分取更低,个股分取更低)。⚠ JSON 解析失败自动回退 L0 并告警,绝不让消息层故障阻断主流程。

## N3 消息如何调整策略(唯一合法的三条通道)

```
市场分 → 敞口系数 exposure_mult:
  score <= -2 : 0.0   (全市场冻结开仓,等同大盘熔断)
  score == -1 : 0.5   (所有 buy 单 weight 减半)
  score >=  0 : 1.0   (正常;正分不加仓——利好不追)
接入点:risk.post_check 在原有规则之后增加第6步:buy.weight *= exposure_mult

持仓黑天鹅:
  个股 score==-2 → 生成强制 sell(weight=0, reason='黑天鹅:{evidence摘要}') + 即时🔴推送
  个股 score==-1 → 不强卖,即时🟡推送预警,由你决定是否手动干预(并记决策日志)

宏观事件日:macro_calendar 命中重大事件(议息/关键数据)当日
  → 推送提示"今晚有{事件},明日波动可能加大",不改变任何信号(纯提醒)
```
⚠ 再次强调:没有第四条通道。任何"利好→买入"的实现都是违规。

## N4 盘中增量扫(run_intraday.py,新增轻量入口)

Actions 增加 cron:北京 10:00 / 13:30 / 14:45(UTC 02:00/05:30/06:45,工作日)。
流程:fetch_flash(自上次扫描) → 只跑 L0 个股黑天鹅匹配(限**当前持仓**标的,快) → 命中即推送;市场级重大负面(score≤-2 词条命中)也即时推送并写 news_signal,当日 17:40 主流程读到后 exposure_mult 生效。盘中扫**只推送不落单**——A股 T+1 且你手动跟单,盘中强卖单意义有限,给你留人工决断空间;若你盘中据此手动卖出,记入决策日志,系统次日按你回填对齐。
运行预算:每次<2分钟,Actions 免费额度内。

## N5 看板新增页:消息面

- 今日市场分仪表 + 近30日分数走势;
- 生效中的敞口系数、由哪些标题触发(evidence 展开);
- 持仓预警流水(黑天鹅/警示历史);
- 明日宏观事件提醒。

## N6 验证与防自欺

- **消息层也要回测**:用 news_raw 历史(能取到多久算多久)重放,对比"有/无消息层"的 S2、S3 净值——验收标准不是提高收益,而是**最大回撤下降且收益降幅可接受**(Calmar 提升)。若消息层让 Calmar 变差,默认关闭它(config: news_layer: false),这层是保险不是引擎;
- 词表/Prompt 属于参数,进 registry(news_layer@v1),同样冻结、改版本;
- 每月看板自动统计:黑天鹅预警的命中率(预警后20日该股相对基准超额收益,应显著为负才算有效)。

## N7 新增任务卡(接在原 P10 之后)

- **P11**:news_adapter.py + 两张表 + 增量去重。验收:重复拉取不重复入库;三源取数各出样例。
- **P12**:news_engine.py L0 + risk 接入 exposure_mult + 通知模板(🔴黑天鹅/🟡警示/事件提醒)。验收:构造含"立案调查"的假新闻→持仓强卖单+推送;市场分-2→当日 buy 全拦。
- **P13**:run_intraday.py + Actions 三个盘中 cron。验收:手动触发一次全流程<2分钟。
- **P14(可选)**:L1 大模型档 + prompts/news_daily.txt + JSON失败回退测试 + "有/无消息层"对比回测报告 reports/news_layer@v1.md。

配置新增(config.example.yaml 追加):
```yaml
news_layer:
  enabled: true
  llm: false                 # true 需配 ANTHROPIC_API_KEY Secret
  intraday_scan: true
  exposure_map: {-2: 0.0, -1: 0.5, 0: 1.0, 1: 1.0, 2: 1.0}
```
