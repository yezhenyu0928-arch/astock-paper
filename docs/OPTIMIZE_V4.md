# 优化蓝图 V4 —— 策略修复与外部借鉴落地（2026-07-10 定稿）

> 本文档由统筹会话（Fable）产出，是执行会话（Sonnet）的**唯一实现依据**。体例沿用 docs/OPTIMIZE_V3.md。
> 外部参考（已完成调研）：ZhuLinsen/daily_stock_analysis、wbh604/UZI-Skill（策略/数据源/看板主要来源）、
> himself65/finance-skills（Minervini SEPA 转译）、muxuuu/serenity-skill（证据分级，备用）、
> ouyang-2019/OpenStock-Enhanced（无可用增量，不采纳）。
> 背景：用户 5 万本金、低风险偏好；实盘模拟期 2026-07-06 已开始；V2/V3 卡全部完成。

---

## 一、现状诊断（2026-07-10 实证，含本轮新确诊故障）

回测五关 + validate 蒙特卡洛判定（reports/*.md）：

| 策略 | 年化 | 最大回撤 | Calmar | 蒙卡判定 | 状态 |
|---|---|---|---|---|---|
| s4_smallcap@v1 | 25.3% | 16.0% | 1.58 | 入池 | live |
| s1_dividend@v2 | 12.1% | 13.8% | 0.88 | 入池 | live |
| s2_etf@v1 | 9.0% | 25.3% | 0.36 | 观察(5%分位-22.3%) | live |
| s6_sector@v1 | 7.5% | 31.5% | 0.24 | 观察(5%分位-30.2%) | live |
| s7_track@v1 | ≈0% | 15.6% | ≈0 | **从未跑过validate** | live观察级 |
| s3_ma_trend@v1 | -0.2% | 24.3% | -0.01 | 观察 | archived |
| s5_grid@v1 | -3.3% | 39.0% | -0.09 | 观察 | archived |
| s1v3 / s4v2(Barra版) | 五段≈0% | — | — | 未跑validate | archived("边际提升有限") |

**本轮确诊的技术故障**（全部有一手证据，修复优先于一切新功能）：

1. **Barra 因子管线故障**：`reports/s1_dividend_at_v3_exposures.json` 里 MOMENTUM/VOLATILITY/QUALITY/
   GROWTH/LIQUIDITY/LEVERAGE/EARNINGS_YIELD 七列**全程 NaN**，仅 BETA/SIZE/VALUE 有值。
   s1_dividend_v3.py L97 与 s4_smallcap_v2.py 的评分直接消费 `factors.compute_factor_exposures()`
   的这些大写列 —— 七因子策略实际最多只有三因子在工作。**"边际提升有限"的归档判决建立在故障管线上，无效，必须修复后重新裁决。**
2. **本地 db/market.sqlite 是残库**：index_members=0 行、daily_bar 仅 2.5 万行（CI 缓存 91 万行）。
   s1v3/s4v2 的近零回测结果与残库高度吻合（个股池查询返回空 → 策略从未建仓）。个股类回测必须先 `python backfill.py` 重建全量库。
3. **backup.yml 缓存键错位**：daily/backfill/intraday/keepalive 全在 `db-*` 血统上，backup.yml 却
   restore `market-db-` 前缀 —— 永远取不到真库，每天备份进 Artifacts 的是残根。备份链路形同虚设。
4. **strategy_optimize.py 死代码**：L194/L210 以 `start_date=/end_date=` 关键字调用
   `backtest.run_backtest(sid, start, end, ...)`，形参名不符，必抛 TypeError；全仓库无调用方无测试。
5. **看板双轨不同步**：dashboard.py `STRAT_CN` 与 gen_reports.py 默认 sid 列表停留在最初 5 个 v1 策略，
   report_html.py 的 STRAT_META 已覆盖 10 个版本。

## 二、外部借鉴采纳清单（已裁剪，只列本轮落地项）

**策略（重点）**：
- UZI-Skill `investor_criteria.py` 的加权规则清单范式（rule_id/权重/布尔check/通过失败文案）→ 新策略 s8。
- UZI 价值派具体规则（ROE连续多年、PE低于自身历史中位数、净利持续为正）→ s8 规则集，全部可用库内数据实现。
- finance-skills `sepa-strategy` Minervini 趋势模板（Stage 2 均线堆叠+RS相对强度+52周位置）→ 新策略 s9，
  结构性替代已死于 whipsaw 的 s3（长期均线堆叠+周频，而非日频短均线交叉）。
- 动量崩溃防护惯例（趋势过滤+波动收缩，daily_stock_analysis 策略卡的 regime 条件思想）→ s2_etf@v2。
- **不采纳**：LLM人格打分/缠论/波浪等不可回测项；龙虎榜（5万资金用不上）；51人格评审（成本高、不可验证）。

**数据源**：
- sh000300 指数日线入库（替代 ETF 代理做展示层基准；akshare 指数接口，CI 可达）。
- 市场情绪数据 spike：两融余额（akshare）+沪深港通成交额（若接口仍在）→ 进 macro 情绪分项+看板情绪卡。
  **北向个股净买入 2024-08 起已停发，不做**；接口探针失败则如实放弃并在方法论页声明。
- 东财 push2 直连日线 fetcher（requests 实现、零新依赖）挂个股兜底链末端（UZI/daily_stock_analysis 共同做法）。
- **不采纳**：efinance/pytdx/finnhub 新依赖；巨潮公告全文（news 层边际小，本轮不做）。

**看板**：
- daily_stock_analysis"决策仪表盘"范式 → 顶部头卡：买X/卖Y/持Z + 风险警报 + 催化因素 + 跟单前检查清单。
- UZI"验证徽章+缺口显式标记" → 策略卡显示 五关/蒙卡判定徽章；数据源健康徽章行。
- UZI 清单式可解释持仓 → s8 持仓表逐股展示"通过了哪些规则"。
- 红线不变：零外部依赖、离线可开、375px 无横滚、盈红亏绿、暴露条中性色。

## 三、设计红线（与 V2/V3 一致，全卡适用）

- 冻结文件不可改：models.py、strategies/base.py、schema.sql、config.example.yaml。
- registry.yaml **只增不改**；旧策略类代码一行不改（bug 修复仅限 factors.py/backtest_v3.py/
  strategy_optimize.py/backup.yml/dashboard.py/gen_reports.py 这些**非策略**文件，且须证明 live 策略行为零变化）。
- 取数只经 data_adapter.py；**不新增 pip 依赖**；Python 3.11（CI）/3.13（本地）双兼容。
- 新策略入赛纪律：先 `python backtest.py report <sid>` 五关 + `python validate.py <sid>` 蒙卡，
  判定**入池→config 挂 live；观察且主回测为正→live 观察级（先例 s7）；主回测为负→不挂载**，报告留档、结论如实。
- 每卡交付含验收证据；改完必须本地全绿：tests/ 原测试 + 本卡新测试 + `python report_html.py`。
- 过程脚本放 `C:\Users\zhenyu\Desktop\测试\过程文件\astock-optimize\`，不入库。

## 四、任务卡

统一开场白（每张卡执行前先读）：
```
背景：A股多策略模拟跟单系统 astock-paper（C:\Users\zhenyu\Desktop\测试\astock-paper），
已上线赛马、实盘模拟期进行中。本次实现依据：docs/OPTIMIZE_V4.md 对应任务卡，逐条落实、
不得自行发明需求；蓝图未定义处沿现有代码惯例并在注释声明。冻结文件与红线见蓝图第三节。
本地 python 用 C:\Users\zhenyu\AppData\Local\Programs\Python\Python313\python.exe；
控制台 GBK，脚本中文输出写 UTF-8 文件。
```

### 卡L（P0·基础修复）：Barra 管线修复 + 备份链路 + 死代码 + 看板同步

1. **factors.py NaN 诊断修复**：用全量库复现 `compute_factor_exposures(pool, date)` 七列 NaN，
   逐因子定位根因（优先怀疑：`_pool_bars` 未返回 volume/amount 列导致 LIQUIDITY 缺输入、
   `_pool_annual` 合并键不匹配导致 QUALITY/GROWTH 全空、MOMENTUM/VOLATILITY 的输入列名与
   `compute_style_factors` 组装处不一致）。修复后：最近交易日全池十列 NaN 率≤15%（缺数据的
   个别新股除外），且 `factors._self_test()` 与 tests/test_factors.py 全绿。
   修复只动 factors.py（及确有必要时 backtest_v3.py 的暴露导出），**不动任何 strategies/*.py**。
2. **backup.yml**：restore-keys 改为 `db-`（对齐 daily 血统）；save key 保持独立命名不变。
3. **strategy_optimize.py**：L194/L210 关键字改为位置参数对齐 `run_backtest(sid, start, end, ...)`
   真实签名；补一个 5 分钟级冒烟测试（tests/test_optimize_smoke.py，用 s2_etf@v1 跑 1 个短窗口）。
4. **dashboard.py**：STRAT_CN 补齐全部 10 个已注册 sid（名称与 report_html.py STRAT_META 一致）；
   gen_reports.py 默认 sid 列表改为读 registry.yaml 全部条目。
5. **旧行为零变化验证（红线）**：取 2-3 个历史调仓日，对全部 5 个 live 策略跑 generate_orders，
   修复前后逐字段 diff=0（对比脚本放过程文件夹，输出留档）。
6. 验收：上述各点证据 + 原 tests 全绿。

### 卡M（P0·依赖卡L与全量库）：Barra 版重裁 + 新策略 s8/s9

1. **s1v3/s4v2 重新裁决**：管线修复后跑标准 `backtest.py report` 五关 + `validate.py` 蒙卡
   （此前只有 backtest_v3.py 五段报告，绕过了蒙卡关）。判定按第三节纪律执行 config 开关，
   config.yaml 注释更新为新裁决结论（旧注释"边际提升有限"删除，写明"V4重裁：<结论>"）。
2. **s8_checklist@v1 价值质量清单**（新文件 strategies/s8_checklist.py，月频，沪深300 池）：
   - 规则表（rule_id/名称/权重/check/文案，UZI 范式，全部库内数据可算）：
     R1 ROE连续5年>15%(权5)；R2 ROE连续3年>10%(权3，与R1阶梯计分不叠加，取高者)；
     R3 peTTM低于自身可得历史(≥3年)中位数(权3)；R4 净利润连续5年>0(权3)；
     R5 净利润5年复合增长>0(权2)；R6 股息率>2%(权2)；R7 年化波动率处池内后50%(权2)；
     R8 12-1月动量>0(权2)；R9 距52周高点回撤<25% 且 高于52周低点>30%(权2)。
   - 评分=Σ通过权重/Σ总权重；月末取分数 top10，行业≤2（复用 factors.get_industry），等权；
     卖出=掉出 top15 或跌破 R9 下沿（52周低点×1.3）。
   - reason 必须含逐条规则结果摘要（如 `清单8.5/24:R1✓R3✓R4✓...R7✗`），供看板卡P渲染。
   - 防未来函数：年报数据按 pub_date≤信号日；PE中位数窗口截至信号日。
3. **s9_stage2@v1 趋势模板**（新文件 strategies/s9_stage2.py，周频=每周最后交易日，沪深300 池）：
   - 硬门槛（全过才候选）：close>MA60>MA120>MA250；MA250 较21日前上行；
     close≥52周低点×1.30；close≥52周高点×0.75；20日均成交额≥5000万（复用现有流动性惯例）。
   - 排序：RSTR 12-1 动量池内排名，取 top hold_n=6，等权。
   - 卖出：跌破 MA120 或 硬门槛失效；周频执行降低 whipsaw（s3 死因=日频短均线交叉，方法论页写明差异）。
4. 两个新策略注册 registry.yaml 末尾（frozen_date=验证完成日）、STRAT_META 补条目、
   config.yaml 按第三节纪律挂载；跑五关+validate，报告如实。
5. 验收：两份五关报告+两份validate报告存在；`run_daily.py --only <sid>` 各跑通；
   原 tests 全绿；registry/config 旧条目 diff=0。

### 卡N（P1·不依赖全量库，可先行）：s2_etf@v2 动量崩溃防护

1. 新类 `S2EtfMomentumV2` 追加在 s2_etf.py 末尾（旧类一行不改），在 v1 逻辑上加两道闸：
   - **趋势过滤**：候选第一名须 close>MA200（200日均线，ETF 后复权价），否则视同无正动量→切国债；
   - **波动目标**：所选风险 ETF 近20日年化波动>25% 时，目标仓位×0.6（与 macro 仓位调节相乘，下限30%）。
2. registry.yaml 末尾注册 s2_etf@v2（universe/基准与 v1 相同，params 增 ma_filter:200 / vol_cap:0.25 /
   vol_scale:0.6）；五关+validate；按第三节纪律挂载 config（v1 继续 live 并行赛马，公平对比）。
3. 目标（写进报告对比行，不达标如实说）：相对 v1，最大回撤 25.3%→<18%，Calmar 0.36→≥0.6。
4. 验收：五关+validate 报告、`run_daily.py --only s2_etf@v2` 跑通、旧 v1 行为 diff=0、原 tests 全绿。

### 卡O（P1）：数据源增强

1. **sh000300 指数日线**：data_adapter.py 增 `fetch_index_daily(index_code)`（akshare
   `stock_zh_index_daily`/腾讯兜底，走 data_health 记账），backfill.py/run_daily.py 接线入
   daily_bar（code='sh000300'，is_suspended=0，adj_factor=1）。用途仅限展示与基准对照，
   **既有策略/回测的基准仍用 sh510300 不变**（避免历史对比断裂）；report_html 基准行加"真指数对照"。
2. **市场情绪 spike→落地**：过程文件夹先写探针验证 akshare 两融余额（沪深两市日频汇总）与
   沪深港通成交额接口在本机/CI 的可用性（CI 用 probe 工作流验证）。可用→data_adapter 增 fetcher、
   新表 `market_sentiment(date, margin_balance, hsgt_amount, ...)`（db.ensure_table，不动 schema.sql）、
   macro.py 增情绪分项（两融余额20日变化率，权重≤15%，declare在方法论页）；不可用→放弃并在
   方法论页声明"已评估不可得"。
3. **东财 push2 直连兜底**：data_adapter.py 增 `_fetch_daily_push2(code)`（requests 直连
   push2his.eastmoney.com，无新依赖），挂个股日线兜底链**末端**（腾讯→tushare→akshare东财→
   baostock→push2直连→yfinance），data_health 记账 source='push2'。
4. 验收：probe/tests 证据、backfill 增量跑通、报告页渲染正常、原 tests 全绿。

### 卡P（P1）：看板升级（只改 report_html.py + dashboard.py 对应处）

1. **决策仪表盘头卡**（置于市场信号卡之后、操作聚合区之前，或直接改造操作聚合区）：
   `📋 今日决策：买入X笔 · 卖出Y笔 · 持仓Z只 · 空仓策略N个`；风险警报行（熔断/大盘冻结/数据健康
   任一异常即红字）；催化行（news 层当日 top1 信号，无则隐藏）；跟单前检查清单三项（心跳收到?
   无熔断告警? 价格在计划带内?）。全部数据来自 state/ 与现有生成上下文，无新数据依赖。
2. **验证徽章**：每张策略卡头部加徽章：`✅入池` / `👀观察` / `⚠️未验证`（数据源=reports/ 下
   validate 报告结论解析，构建时静态嵌入；无报告=未验证）。徽章附 title 提示蒙卡5%分位数字。
3. **s8 清单渲染**：持仓表行展开显示该股规则通过情况（数据来自 reason 字段解析，s8 专属，
   其他策略不受影响）。
4. **数据源健康徽章行**（页脚上方）：读 state/data_source_health.json，每源一枚
   `🟢/🟡/🔴 源名`，title 显示成功率；json 缺失整行不渲染。
5. **情绪卡**（依赖卡O.2 落地，未落地则跳过）：两融余额20日变化 mini 条 + 港通成交额，中性色。
6. 红线自检：零外链、离线可开、375px/720px 无横滚、盈红亏绿仅用于盈亏数字、
   删除任一 state/reports 输入文件页面仍能降级生成；`python report_html.py` 三页全部再生成。
7. dashboard.py：新增策略进 STRAT_CN（卡L已做基础同步，此处补 s8/s9/s2v2）。

## 四点五、执行地点决策（2026-07-10 计划修订，实证驱动）

**证伪记录**：原计划"本机 backfill 重建全量库 → 本机跑个股回测"已被证伪。实测本机 `python backfill.py`
卡死在第3步第一只个股（15 分钟零进展，daily_bar 停在 26737 行/16 个 code、全为 ETF，沪深300 成分
300 只中仅 1 只有数据，stock_annual 表未建成）。根因=个股数据源本机不可用：东财历史被本机代理阻断、
baostock 裸 socket 挂死、腾讯 CDN 为 CI 海外 runner 设计。README L22 亦自述"东财历史被代理挡"。

**修订后分工**：

| 工作 | 执行地点 | 验证方式 |
|---|---|---|
| 卡L 代码修复（backup/死代码/看板同步） | 本机 | 已完成（批1） |
| 卡N s2_etf@v2（纯 ETF，残库数据 2019 起完整） | **本机** | backtest+validate 本机直接跑 |
| 卡L.1 factors.py NaN 修复 | 本机改码 | **合成小样库**喂 compute_style_factors 验证十列非全 NaN（独立通道）；全量正确性 CI 复验 |
| 卡M s1v3/s4v2 重裁、s8/s9 五关+validate（个股） | **CI** | 新建 backtest.yml，push 触发，报告 commit 回仓库后 git pull 复核 |
| 卡O 数据源（sh000300 指数/两融/东财 push2） | 本机改码 | 本机联网受限→接口拉取由 CI backfill/probe 验证 |
| 卡P 看板升级 | 本机 | report_html.py 本机跑（用现有 state/reports）验证三页渲染 |

**CI 回测闭环设计**：backtest.yml（`workflow_dispatch` + push 到 strategies/registry 路径触发），
CI 内 restore db cache（不足则内联补 backfill）→ 跑指定 sid 五关+validate → commit reports/ 回 main。
本机无 gh CLI，靠 git push 触发、git pull 取回报告，不需用户手动介入（首次若 CI 无全量库 cache，
需用户在 Actions dispatch 一次 backfill 建库）。

## 五、执行顺序与复核点

```
卡N（ETF数据本地已够，先行）
   ↓        （backfill 全量库完成后）
卡L（修复） → 卡M（重裁+新策略，重回测）
   ↓
卡O（数据源） → 卡P（看板）
   ↓
统筹复核：全 tests、五关/validate 报告结论、三页渲染、live策略零变化证据 → 一次性 commit+push
```

- 统筹会话（Fable）复核点：每卡验收证据抽查 + 最终推送前全量回归。
- 回测提示：validate 蒙卡多次重跑回测，s8/s9 实现时评分函数须整池向量化（pandas 批量 SQL），
  禁止逐股循环查库；factors 进程缓存复用。
- 推送 GitHub 后 dispatch daily 工作流验证线上生成（CI 全量库环境）。
