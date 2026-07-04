# 开发规格书(SPEC)v5

> 实现任何模块前必读。冻结文件(models.py / strategies/base.py / schema.sql / config / registry / daily.yml)已在仓库中,**不得修改**,只能实现规格书中标注"待实现"的模块。
> 记号:【函数规格】= 必须按此签名实现;【算法】= 必须按此步骤实现;⚠ = 常见踩坑。

---

## 模块0 通用约定

- 代码统一:code 格式 `sh510300` / `sz000001`(小写前缀+6位);日期 `YYYY-MM-DD`。
- 所有金额单位:元;成交量:股;费率/比例:小数(0.0005)。
- 配置读取:`config.yaml` → dict,秘钥从环境变量覆盖注入(PUSHPLUS_TOKEN 等)。
- 日志:标准 logging,INFO 到 stdout(Actions 日志可查)。
- ⚠ 时区:Actions 是 UTC,所有"今天"必须用 `datetime.now(ZoneInfo("Asia/Shanghai"))`。

---

## 模块1 data_adapter.py + calendar.py(任务卡 M1)

【函数规格】
```python
# data_adapter.py —— 唯一允许 import akshare/baostock 的文件
def fetch_daily(code: str, start: str, end: str) -> pd.DataFrame
    # 列:trade_date,open,high,low,close,volume,amount,adj_factor,is_suspended,limit_up,limit_down,source
def fetch_calendar(start: str, end: str) -> pd.DataFrame          # cal_date,is_open
def fetch_index_members(index_code: str) -> pd.DataFrame          # code,in_date,out_date
def fetch_dividend(code: str) -> pd.DataFrame                     # ex_date,cash_per_share,shares_ratio
def fetch_security_info(codes: list[str]) -> pd.DataFrame         # name,type,is_t0,list_date,status
def upsert(df, table)                                             # 写库,主键冲突则替换

# calendar.py
def is_trade_day(date) -> bool
def prev_trade_day(date, n=1) -> str
def next_trade_day(date, n=1) -> str
def last_trade_day_of_week(date) -> bool
def last_trade_day_of_month(date) -> bool
```

【AkShare 取数映射】(⚠ AkShare 接口名随版本变动,实现时先 `pip show akshare` 并以当版本文档核对;失效即换 baostock 实现并告警)
| 需求 | AkShare 候选接口 | 备源 baostock |
|------|-----------------|---------------|
| ETF日线 | fund_etf_hist_em(symbol, adjust="") | query_history_k_data_plus |
| 个股日线(不复权) | stock_zh_a_hist(adjust="") | 同上 adjustflag='3' |
| 复权因子 | stock_zh_a_hist(adjust="hfq") 与不复权相除 | adjustflag='1' 对比 |
| 交易日历 | tool_trade_date_hist_sina | query_trade_dates |
| 指数成分变更 | index_stock_cons / 中证官网CSV | 无,标注缺失 |
| 分红 | stock_dividents_cninfo 或 stock_fhps_em | 无 |

【算法】增量更新:每标的取库内最大 trade_date,从其次日拉到今天;空库则从 2018-01-01。
【质检】`check(date)`:①日历显示开市但 daily_bar 无该日数据→FAIL ②非新股涨跌幅>11%→WARN ③adj_factor 出现回退→FAIL。FAIL 时抛异常阻断主流程(会触发 Actions 告警)。
⚠ ETF 的涨跌停价接口常缺失:按前收×1.1/0.9 计算兜底(科创/创业板个股为±20%,按代码前缀 688/300 判断)。

【验收测试】tests/test_m1.py:双源切换写日志;手动删库中某日后 check 能 FAIL;日历判断 2026-06-08(周一,端午调休需核实)正确。

---

## 模块2 engine.py(任务卡 M2)

【函数规格】
```python
class Engine:
    def __init__(self, config, registry): ...
    def load_account(self, strategy_id) -> Account       # 从 state/*.json,无则按 capital 初始化
    def save_account(self, account): ...
    def settle(self, date: str) -> list[Order]:
        """开盘撮合:处理昨日 pending + 今日除权 + 更新净值。幂等。返回成交回报单"""
    def run_strategies(self, date: str) -> list[Order]:
        """收盘信号:risk.pre_check -> 各启用策略 generate_orders -> risk.post_check
           -> 存入 pending -> 返回推送用订单"""
    def ctx(self, date) -> DataContext                    # 实现 base.py 的全部抽象方法
```

【算法】settle(date) 严格按序:
1. 除权:对每个持仓,若 dividend 表有 ex_date==date:现金红利入 cash(shares×cash_per_share,暂不扣红利税,简化并注释);送转 shares×(1+ratio) 取整,avg_cost 同比例摊薄。
2. 撮合 pending(按 signal_date==prev_trade_day(date) 过滤):
   - 成交价 = date 开盘价 × (1+slippage)(买)/(1−slippage)(卖);
   - 买单:若 prev 日收盘==limit_up → status=cancelled;股数 = floor(总资产×weight / 价 / 100)×100;现金不足按现金上限降;佣金=max(金额×rate, 5);
   - 卖单:若 date 开盘==limit_down 或停牌 → deferred(保留至次日重试,最多顺延10日后强制按可成交日成交);T+1:position.buy_date==date 且 is_t0==0 → deferred;税=金额×stamp_tax_sell;
   - weight==0 的 sell = 清仓全数。
3. 更新 highest_close、nav(=总资产/init_capital,价用当日收盘)、追加 nav_history、写 trade_log.csv(real_price 留空)。
4. 幂等:settle 开头检查 trade_log 已有 Order.key() 的记录则跳过该单。

⚠ 精度:金额统一 round(x,2);股数 int。⚠ nav 必须用**不复权收盘价×持仓股数**计算市值(除权已通过第1步调账),不要混用复权价。

【验收测试】tests/test_m2.py 至少覆盖:最低佣金5元触发;T+1 拦截;涨停买单 cancelled;跌停卖单 deferred 并次日成交;10派2元现金红利入账;同日 settle 跑两遍结果不变。

---

## 模块3 五个策略(任务卡 M3 与 P9)

统一骨架:读 registry 参数 → 判断调仓日 → 选股/打分 → 与持仓 diff → 输出 Order。**策略内不做止损、不做仓位上限**(risk.py 统一管)。

### S2 ETF动量轮动【算法】(M3,最先实现)
```
每周最后交易日:
  对 universe 每只:r20 = close[-1]/close[-21]-1;r60 = close[-1]/close[-61]-1
  rank20, rank60 = 各自降序名次;score = (rank20+rank60)/2,取 score 最小者 best
  绝对动量:若 best 的 r20 < 0 → best = safe_asset
  若 best 已满仓持有 → return []
  卖出所有 code != best 的持仓(weight=0);买入 best(weight=0.98,留2%现金缓冲)
```

### S1 红利低波【算法】(月末调仓)
```
池:全A,ctx.is_tradable 过滤,再过滤:
  股息率(近12月分红/市值) >= 0.04;连续3年每年有现金分红;
  250日收益波动率位于剩余池后30%
打分 = 股息率排名(50%) + 低波排名(50%),取前10,等权 weight=0.098
与持仓 diff:掉出前10→卖;新进→买
⚠ 需要分红与市值数据:M1 的 dividend 表 + daily_bar.amount/close 推算流通市值不准,
  实现时补 fetch_market_cap 接口(ak.stock_zh_a_spot_em 或雪球),历史市值缺失则该策略回测起点后移。
```

### S3 双均线趋势【算法】(每日)
```
池:ctx.members('sh000300', date),is_tradable + avg_amount 过滤
买:MA20 昨日<=MA60 且 今日>  且 volume > 1.5×MA(volume,20);持仓<10 且未持有 → buy weight=0.095
卖:close < MA20 → sell weight=0(清仓该票)
同日买入信号多于空位:按 (close/MA60-1) 强度降序取前若干
```

### S4 小市值多因子【算法】(月末)
```
池:全A过滤(is_tradable + 流动性 + 剔除上市<1年)→ 按市值升序取前400
打分:size分=市值升序百分位×0.5 + pb分=PB升序百分位×0.3 + mom分=20日收益降序百分位×0.2
取综合分前20,等权 weight=0.049;月末全量 diff 调仓
⚠ 需要 PB:同 S1 的补数问题,统一在 P7 任务解决(fundamental 表:code,date,pe,pb,market_cap)
```

### S5 大盘择时网格【算法】(每日,只操作 sh510300)
```
状态:tranches=5 档,记录已建档数 k(存 account 外挂字段或以持仓市值/总资产反推)
PE分位:沪深300 PE 相对近10年百分位(需 P7 的 fundamental 表;缺数据期间仅网格不择时)
分位<0.30:每跌 grid_step 加1档(每档 weight=0.19),k<5
分位>0.70:每涨 grid_step 减1档
0.30~0.70:纯网格,自最近一次操作价 ±2% 触发加/减1档
```

【每个策略交付时必须附】reports/{id}.md 五关验证报告:①2019-23样本内/2024-今样本外 Calmar 对比 ②滚动前推5轮参数稳定性 ③参数±20%扰动 ④滑点加倍 ⑤2018熊/19-21牛/22-24震荡分段。回测复用 Engine(传历史日期循环 settle+run_strategies 即可,不另写回测引擎)。

---

## 模块4 risk.py(任务卡 P8;MVP 期先实现只含熔断与大盘冻结的精简版)

【函数规格】
```python
def pre_check(date, ctx, accounts) -> dict
    # 计算各账户回撤(1 - nav/max(nav_history)),>15% → account.frozen=True,清仓单入列,告警
    # 大盘:sh510300 单日跌>3% 或 20日跌>10% → {'market_frozen': True}
def post_check(date, ctx, orders, accounts) -> list[Order]
    # 1) market_frozen:删除全部 buy 单
    # 2) frozen 策略:删除其全部单,仅保留清仓 sell
    # 3) 单票上限:成交后占比预估>15% 的 buy 削 weight
    # 4) 流动性:stock 类 avg_amount(20)<5000万 → 删单并在 reason 记录
    # 5) 止损:遍历持仓,浮亏超阈值(trend 8% / rotation 12%)生成强制 sell,reason='止损'
```

---

## 模块5 notify.py(任务卡 M4)

【函数规格】`push(title, content, level)`:level∈{op, alert, heartbeat}。PushPlus HTTP 失败(非200或异常)→ 自动走 SMTP;两者都失败 → 抛异常(让 Actions 变红)。CLI:`python notify.py --alert "..."`。
消息模板(严格按此渲染,含 emoji 与字段顺序):
```
【明日操作 | {策略中文名}】{date} 18:00
① {卖出/买入} {6位代码} {名称}  {全部X股/约Y%仓位≈Z股}  参考价{close}
   理由:{reason}
→ 请于明日开盘后按开盘价附近跟单,成交后在看板回填实盘价
```
心跳:`【心跳】{date} 系统正常 | 数据至{last_date} | {无操作策略列表或"今日有操作见上条"}`
告警:`【告警🔴】{内容}`。心跳每日必发(即使无任何操作)。

---

## 模块6 run_daily.py(任务卡 M5)

【算法】主流程(幂等,可重复执行):
```
1 now=北京时间;若 not is_trade_day(today):发心跳"非交易日"并退出
2 data.update_all() → check(today);FAIL→告警+退出(exit 1)
3 rep = engine.settle(today)        # 撮合昨日信号(开盘价已有)
4 若 rep 非空:push 成交回报(op)
5 orders = engine.run_strategies(today)
6 若 orders 非空:push 明日操作(op,按策略分条)
7 push 心跳
8 各 account save;trade_log 落盘
```
⚠ 第3步撮合"昨日信号今日开盘价"与第5步"今日信号明日成交"在同一次 17:40 运行中完成——因为 17:40 时今日开盘价早已产生。无需早盘任务,回报消息与计划消息同刻先后发出,回报标题写"今日模拟成交回报"。

---

## 模块7 dashboard.py(任务卡 M6 前4页 + P10 补齐)

Streamlit 多页(st.tabs),进入先密码(st.text_input type=password 对比 env DASHBOARD_PASSWORD)。
1. **赛马榜**:表(策略/累计收益/年化/最大回撤/Calmar/夏普/胜率/超额vs基准/状态);plotly 净值曲线叠加(含各基准虚线)。
2. **持仓**:按策略分组表(代码/名称/股数/成本/现价/浮盈%/持有天数)。
3. **操作流水**:trade_log 表,策略筛选;real_price 列用 st.data_editor 可编辑,保存按钮写回 CSV。
4. **明日计划**:读各 state 的 pending_orders 渲染。
5. 系统健康(P10):state 最新 as_of、日历核对、最近5次 git snapshot 时间。
6. 跟单摩擦+决策日志(P10):|real-sim|/sim 分布直方图、每策略年化摩擦估算=平均偏差×年换手;决策日志表单(日期/策略/偏离内容/原因)存 state/journal.csv。
⚠ 手机适配:表格列数≤6,超出的放 expander;所有图 use_container_width=True。

---

## 模块8 P7 基本面补数(fundamental 表)

```sql
CREATE TABLE fundamental (
  code TEXT, trade_date TEXT, pe REAL, pb REAL,
  market_cap REAL, dividend_yield REAL,
  PRIMARY KEY(code, trade_date));
```
来源:ak.stock_a_indicator_lg(个股历史PE/PB/市值,按公告日已对齐)或乐咕乐股系接口;指数PE分位:ak.stock_index_pe_lg。⚠ 若历史深度不足,S1/S4/S5 回测起点相应后移并在验证报告注明,不许用当前值回填历史。

---

## 附A 全局测试清单(实现完 M1–M6 后统跑)
□ 全新克隆仓库,只配3个Secret+改config两处,手动触发Actions一次成功并收到心跳
□ 断网重跑 data 更新:告警且退出码非0
□ 手工往 state 塞一个回撤16%的账户:pre_check 熔断+清仓单+告警
□ 周五运行产生 S2 调仓单,周一 settle 按周一开盘价成交,费用与手算一致(给出手算过程)
□ 看板手机打开(390px宽)无横向滚动

## 附B 明确不做(防实现模型自由发挥)
不接实盘/券商接口;不做盘中任务;不做数据库以外的缓存层;不引入重型框架(backtrader/airflow等);不加密state(私有仓库足够);不做多用户。
