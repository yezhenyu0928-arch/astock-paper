# 优化蓝图 V3 —— Barra 风格因子体系 + 风险模型 + 看板方法论折叠（2026-07-04 定稿）

> 本文档由统筹会话（Fable）产出，是后续执行会话（Opus）的**唯一实现依据**。体例沿用 docs/OPTIMIZE_V2.md。
> 方法论参考：MSCI Barra 中国A股模型 CNE5/CNE6（长期版 CNLT 与交易版 CNTR 公开 Empirical Notes）、
> Axioma Robust Risk Model V4 Handbook。按本项目**免费数据现实**裁剪，所有简化必须在方法论页如实声明。
> 背景不变：用户 5 万本金、低风险偏好；2026-07-06（周一）实盘模拟期开始；V2 卡A–H 已全部完成上线。

---

## 一、需求与现状差距（本轮三条主线）

| # | 用户需求 | 现状 | 差距 |
|---|---------|------|------|
| 1 | 各策略的因子太简单 | S1/S4 单描述符排名法打分（股息率、市值、PB、20日收益），无去极值、无标准化 | **缺**：Barra 式多描述符复合因子（去极值→标准化→加权复合→再标准化） |
| 2 | 没有风险因子 | risk.py 只有硬熔断/仓位纪律；策略与看板均无"风格暴露"概念 | **缺**：风险因子库（Size/Beta/动量/残差波动/流动性/价值）、组合暴露计算、预测波动、选股期风险约束 |
| 3 | 因子之间要正交化 | 无任何正交化；低波与 Beta/Size 天然共线 | **缺**：WLS 回归残差正交化管线（ResVol⊥Beta,Size 等，仿 CNE6 固定关系） |
| 4 | 看板不直接呈现投资逻辑，模型逻辑折叠或跳转 | 每张策略卡把 tagline+因子权重表**平铺**在卡片头下（`_factor_block`） | 改为默认折叠 `<details>` + 新增 methodology.html 方法论页跳转 |

设计红线（与 V2 一致，全卡适用）：
- **冻结文件不可改**：models.py、strategies/base.py、schema.sql、config.example.yaml。
  （schema.sql 冻结=既有表结构不得改；**新增独立表**用 `db.ensure_table` 创建，先例 stock_annual/fundamental。）
- **registry.yaml 只增不改**：策略逻辑变化 = 注册新版本并行赛马，旧版本条目与旧策略类一字不动、输出零变化。
- 取数只经 data_adapter.py；Python 3.11；**不新增依赖**（numpy/pandas 已在 requirements）。
- daily.yml 等 workflow 本轮**不解冻**（无需改动）。
- 每卡交付含验收证据；改完必须本地跑通 `python report_html.py` 与相关 tests/。

---

## 二、数据现实与可行因子集（统筹会话已实测验证，2026-07-04）

**库内可得**（db/market.sqlite，912k 日线行、数据至 2026-07-03）：
- `daily_bar`：不复权 OHLCV + amount + adj_factor（**后复权价 = close × adj_factor**）+ is_suspended。
- `fundamental`：peTTM、pbMRQ、market_cap（≈amount×100/turn 反推）、dividend_yield，日频，2018 起。
- `stock_annual`：roe（roeAvg 小数）、net_profit、pub_date（公告日，防未来函数），年度，2015 起，沪深300 全覆盖（300 codes）。
- `dividend`、`index_members`（沪深300 快照，含幸存者偏差，报告注明——现状已如此）。
- **换手率可推**：turn% = amount×100/market_cap（最近交易日 amount 与 market_cap 双有效占比 99.7%）。
- **市场代理**：库内无 sh000300 指数日线（0 行）；市场收益一律用 **sh510300 ETF 后复权收益**替代（daily_bar 内有全历史）。

**不可得（如实放弃并在方法论页声明）**：分析师预期数据（无 Analyst Sentiment / 预期EP / 预期股息）、
季频资产负债表与现金流（无严格 Leverage / Investment Quality / Earnings Quality 因子）、真实流通股本
（换手率为反推近似）。

**本项目可实现的 CNE6 适配因子清单**（描述符定义见第三节）：

| 因子 | 描述符（权重沿用 Barra CNE5/USE4 公开惯例） | 用途 |
|---|---|---|
| Size | LNCAP ×1.0 | 风险因子 + S4 alpha（反向） |
| Beta | HBETA ×1.0 | 风险因子 |
| Momentum | RSTR(12-1月) ×1.0 | 风险因子 + S4 alpha |
| Residual Volatility | DASTD 0.74 + CMRA 0.16 + HSIGMA 0.10，**正交化到 Beta、Size** | 风险因子 + S1 alpha（反向） |
| Liquidity | STOM 0.35 + STOQ 0.35 + STOA 0.30 | 风险因子 |
| Book-to-Price | BTOP ×1.0 | 风险因子（价值）+ S4 alpha |
| Earnings Yield | ETOP ×1.0（仅 TTM，无预期档） | S4 alpha |
| Dividend Yield | DTOP ×1.0（仅历史档） | S1 alpha |
| Profitability | 年报 ROE ×1.0（无季频 ATO/GPM 档） | S1/S4 alpha |
| Growth | EGRO ×1.0（净利润5年回归斜率/均值） | 备用（本轮不进策略） |
| Earnings Variability | VERN ×1.0（净利润5年变异系数） | 备用（本轮不进策略） |
| Short-Term Reversal | STREV(1月) ×1.0 | 备用（5万资金摩擦不友好，V2 已论证不用） |

---

## 三、因子工程方法论规范（factors.py = 全项目唯一因子实现，卡I 落地）

### 3.1 预处理管线（顺序固定，全因子统一）

1. **去极值**（Axioma 式 MAD winsorize）：`winsorize_mad(x, n=3)`
   下上界 = median(x) ± n × 1.4826 × MAD(x)，MAD = median(|x−median(x)|)；越界值截断到边界。
2. **标准化**（Barra 式）：`standardize(x, cap)`
   z = (x − μ_cw) / σ_eq；μ_cw = **市值加权均值**，σ_eq = **等权标准差**。
   估计域 = 当日沪深300 成分中该因子有效的样本。性质：市值加权组合的因子暴露≈0。
3. **缺失处理**：标准化后 NaN → 0（池中性）；复合因子中某描述符缺失时，按**可得描述符权重重归一**。

### 3.2 正交化

`orthogonalize(y, X, w)`：以 w=√市值 为权的 WLS 回归 y = Xb + ε，取残差 ε 再 standardize。
固定正交化关系（仿 CNE6）：
- **ResVol ⊥ (Beta, Size)**：三描述符先各自标准化→按权重合成→对 Beta、Size 正交化→再标准化。
- 复合 alpha 分数本身不强制对全部风险因子正交（保留策略风格），风险控制走第四节的暴露约束。

### 3.3 描述符定义（防未来函数：一切输入 trade_date ≤ 信号日、pub_date ≤ 信号日）

价格类（后复权收益 r_t；市场收益 rm_t = sh510300 后复权收益；超额 e_t = r_t − rm_t）：
- **LNCAP** = ln(market_cap)（fundamental 当日或此前最近一条）。
- **HBETA**：e 对 rm 的 EW-WLS 回归斜率；窗口 252 交易日、半衰期 63 日、最少 120 个有效双样本日。
  （CNE6 用 504/252 并做 4 日聚合；本库 2018 起+沪深300 池，取 252/63 的短窗适配，声明差异。）
- **HSIGMA**：上述回归残差的等权标准差（同窗口）。
- **RSTR**（12-1 动量）= Σ_{t∈[信号日-252, 信号日-21]} w_t · [ln(1+r_t) − ln(1+rm_t)]，w_t 指数权半衰期 126 日。
  （简化：不做 CNE6 的 11 日滞后平均，声明差异。）
- **STREV**（1月反转）= −Σ_{t∈[-21,0]} [ln(1+r_t) − ln(1+rm_t)]（备用，本轮不进策略）。
- **DASTD**：超额收益 e_t 的 EW 标准差，窗口 252、半衰期 42。
- **CMRA**：Z(T) = Σ_{最近 T×21 日} ln(1+e_t)，T=1..12；CMRA = max Z(T) − min Z(T)。
- 换手率 turn_t = amount_t × 100 / market_cap_t（%）；
  **STOM** = ln(Σ 最近21日 turn_t)，**STOQ** = ln(mean 最近3个21日窗口的 Σturn)，**STOA** = ln(mean 最近12个21日窗口的 Σturn)；turn 累计≤0 时该描述符缺失。
- 价格类窗口内有效样本 < 窗口×60% → 该描述符缺失。

基本面类：
- **BTOP** = 1/pbMRQ（pb≤0 → 缺失）；**ETOP** = 1/peTTM（保留负值——亏损股 ETOP 为负，交给去极值处理；pe=0/缺 → 缺失）；**DTOP** = dividend_yield。
- **ROE**：stock_annual 中 pub_date≤date 的最近一期 roe。
- **EGRO**：pub_date≤date 的最近 5 期（≥3 期）net_profit 对时间回归斜率 / mean(|net_profit|)。
- **VERN**：同窗 net_profit 的 std / |mean|。

### 3.4 核心 API（卡I 实现，卡J/卡K 只准调用不准重算）

```python
# factors.py
RISK_FACTORS = ["size", "beta", "momentum", "resvol", "liquidity", "btop"]
ALPHA_DESCRIPTORS = ["dtop", "etop", "roe", "egro", "vern", "strev"]  # 已标准化，方向=原始方向

def compute_exposures(conn, date, pool=None, use_cache=True) -> "pd.DataFrame":
    """返回 DataFrame(index=code, columns=RISK_FACTORS+ALPHA_DESCRIPTORS+['lncap_raw','market_cap','industry'])。
    pool 默认=当日沪深300成分(ctx.members 同源 SQL)。全流程 pandas 批量 SQL，禁止逐股循环查库。
    use_cache=True 时按 (date, tuple(pool) 摘要) 做进程级 LRU 缓存（validate 多次回测复用，务必实现）。"""

def winsorize_mad(s, n=3.0): ...
def standardize(s, cap_weight): ...
def orthogonalize(y, X_df, w): ...
def composite(z_df, weights: dict) -> "pd.Series":
    """Σ w·z，方向统一'越大越好'由调用方通过负权重表达；缺失描述符按剩余权重重归一；结果再 standardize。"""
```

---

## 四、风险模型规范（riskmodel.py，卡I 落地）

结构模型 r = Xf + u（Axioma/Barra 横截面法），全部日频、纯 numpy/pandas，无新依赖：

1. **暴露矩阵 X**：factors.compute_exposures 的 RISK_FACTORS 六列（N×6）。
2. **因子收益**：对每个交易日 t，用 t−1 日暴露对 t 日个股收益做 **WLS（权=√市值）**横截面回归
   （含截距=市场项），得 f_t 与残差 u_t；回看窗口 504 交易日（不足则有多少用多少，≥120 才建模）。
3. **因子协方差 F**：f_t 的 EWMA 协方差，半衰期 90 日，×252 年化。
   （简化声明：不做 Newey-West 串行相关校正、不做 VRA/特征值调整——日频人工跟单场景，非组合优化用途。）
4. **特异波动 σ_i**：u_{i,t} 的 EWMA 标准差，半衰期 42 日，×√252 年化；某股样本不足 → 取池中位数。
5. **组合预测波动**：σ_p = √( h'X F X'h + Σ h_i²σ_i² )，h=各持仓市值权重（现金权重暴露为 0、特异为 0）。
6. **组合暴露**：X_p = Σ h_i·z_i。因标准化以市值加权均值为中心，市值加权基准的风格暴露≈0，
   故 **X_p 本身即可读作主动暴露**（方法论页要向用户解释这一点）。

```python
# riskmodel.py
class RiskModel:  # exposures(X: DataFrame), fcov(F: 6×6 年化), spec_vol(Series 年化), date
def estimate(conn, date, lookback=504) -> RiskModel   # 进程级缓存同日结果
def portfolio_exposure(rm, holdings: dict[str, float]) -> dict     # 权重=市值占比
def portfolio_vol(rm, holdings: dict[str, float]) -> float         # 年化小数
def export_exposures(conn=None, out_path=None) -> str:
    """读 state/*.json 全部启用策略持仓 → 写 state/factor_exposure.json：
    { "date": "...", "factors": RISK_FACTORS,
      "strategies": { sid: {"exposures": {f: x}, "pred_vol": 0.18, "n_pos": 8, "weight_invested": 0.95} } }
    纯个股策略正常计算；ETF 持仓（S2/S5/S6）不映射风格因子 → 该 sid 输出 {"etf_only": true, "pred_vol": null}。
    任何异常：log.warning 后跳过该策略，函数不抛异常（供 run_daily 收尾安全调用）。"""
```

---

## 五、任务卡（Opus 按序执行）

统一开场白（每张卡前都贴）：
```
背景：A股多策略模拟跟单系统 astock-paper（C:\Users\zhenyu\Desktop\测试\astock-paper），
已上线赛马、2026-07-06 实盘模拟期开始。本次实现依据：docs/OPTIMIZE_V3.md 对应任务卡，
逐条落实、不得自行发明需求；蓝图未定义处按 CNE5/CNE6 公开惯例并在代码注释声明。
冻结文件不可改：models.py、strategies/base.py、schema.sql、config.example.yaml；
registry.yaml 只允许"新增注册"，旧条目一字不动；旧策略类代码一行不改。
Python 3.11；不新增 requirements 依赖；取数只经 data_adapter.py。
每卡交付含验收证据（测试输出/生成文件检查清单勾选结果）。
改完必须本地跑通 python report_html.py 与 tests/ 全部原测试。
```

### 卡I（P0·基础设施）：factors.py + riskmodel.py + 行业表 + tests/test_factors.py

1. **行业数据**：data_adapter.py 增 `fetch_industry()`（baostock `query_stock_industry()`，返回全A申万分类：
   code、industry、updateDate；industry 空串→None）。新表（ensure_table，不动 schema.sql）：
   `stock_industry(code TEXT PRIMARY KEY, industry TEXT, update_date TEXT)`。
   提供 `factors.ensure_industry(conn)`：表空或 update_date 距今>30天时全量刷新（一次 API 调用，很便宜），
   失新败旧：抓取失败保留旧表并 log.warning。backfill.py 末尾调用一次；run_daily.py 数据更新段调用一次。
2. **factors.py**：按第三节全部规范实现。性能要求：单日全池（300股）compute_exposures ≤ 10 秒
   （批量 SQL：一次拉池内全部 daily_bar 近300日 + fundamental 当日截面 + stock_annual，pandas 矩阵化）。
3. **riskmodel.py**：按第四节实现。
4. **tests/test_factors.py**（合成数据为主，真实库冒烟为辅）：
   - winsorize_mad：人造含极端值序列，验证边界与截断；
   - standardize：验证结果市值加权均值≈0（<1e-8）、等权 std≈1；
   - orthogonalize：正交化后残差对 X 的 WLS 系数≈0；
   - composite：某描述符整列缺失时权重重归一，结果仍 std≈1；
   - 防未来：合成两日数据，date 取前一日时后一日数据不影响结果；
   - 风险模型：合成 3 因子已知协方差数据，estimate 恢复的组合波动与理论值同数量级（宽松断言）；
   - 真实库冒烟（db 存在才跑，否则 skip 并打印）：最近交易日 compute_exposures 形状=(池数, 全列)，
     RISK_FACTORS 列 NaN 率=0（缺失已填0）、填0前有效率≥85%；estimate 成功；
     对一个等权 10 股合成组合 portfolio_vol ∈ (5%, 60%)。
5. **验收**：`python tests/test_factors.py` 全绿输出留档；冒烟数字（有效率/耗时/预测波动样例）写入交付说明；
   原 tests（m1/m2/m4/risk/news）仍全绿；无任何旧文件行为变化（git diff 仅新增文件 + data_adapter/backfill/run_daily 的最小增量）。

### 卡J（P1·依赖卡I）：注册 s1_dividend@v3 与 s4_smallcap@v2 + 五关 + 暴露导出接线

1. **registry.yaml 末尾只增两条**（frozen_date=验证完成日）：

```yaml
s1_dividend@v3:
  class: strategies.s1_dividend.S1DividendBarra
  benchmark: sh510300
  frozen_date: <验证完成日>
  params:
    min_dividend_yield: 0.04     # 门槛三件套与 v2 相同
    dividend_years: 3
    roe_years: 3
    roe_min: 0.08
    weights: {dtop: 0.4, low_resvol: 0.3, roe: 0.3}   # Barra式标准化复合(z分),非排名法
    hold_n: 10
    max_per_industry: 2          # 申万一级行业集中度约束(行业未知不计数)
  universe: dynamic              # 沪深300过滤,generate_orders 内实现
  rebalance: monthly
  # 与 v2 差异声明:①排名法→去极值+标准化+正交化复合(ResVol⊥Beta,Size 取负向);
  # ②取消"低波后30%"硬截断(低波已进复合分,硬截断与之重复且破坏连续性);③新增行业≤2约束。

s4_smallcap@v2:
  class: strategies.s4_smallcap.S4MultiFactorValue
  benchmark: sh510300
  frozen_date: <验证完成日>
  params:
    weights: {small_size: 0.30, btop: 0.20, etop: 0.20, momentum: 0.15, roe: 0.15}
    resvol_cap_z: 1.28           # 风险过滤:剔除残差波动 z>1.28(约最高10%)的高波股
    max_per_industry: 3
    hold_n: 20
  universe: dynamic              # 沪深300,无市值预筛(池即300只)
  rebalance: monthly
  # 与 v1 差异声明:①20日动量(噪声/反转区)→RSTR 12-1月动量;②PB排名→BTOP z分并加 ETOP/ROE;
  # ③市值排名→-LNCAP z分;④新增残差波动帽与行业约束。名称展示"多因子价值增强(沪深300)"。
```

2. **新策略类**追加在原文件（s1_dividend.py / s4_smallcap.py）末尾，新类新代码路径，
   旧类一行不改。实现要点：
   - 月末信号日调用 `factors.compute_exposures(ctx.conn, date)` 一次拿全池截面；
   - S1 门槛过滤（DY/连续分红/ROE 质量，均复用 ctx / fundamental 现有方法）→ 幸存者上取
     score = 0.4·z(dtop) + 0.3·(−z(resvol)) + 0.3·z(roe)（z 取自截面表；幸存者子集不重新标准化，
     直接用全池 z——声明理由：保持与风险模型同一参考系）；
   - S4 过滤（可交易+上市满1年+bp/ep 有效）→ 剔 resvol z>resvol_cap_z → score 按权重表；
   - 行业约束：按 score 降序贪心装入，某行业已满 max_per_industry 则跳过取下一名；
   - reason 沿用卡H体例，含关键数字：如
     `红利Barra:买入XX(股息率5.2% z+1.8·低波z-1.2·ROE 14% z+0.9·综合第3/41·行业:银行)`；
     卖出注明掉出排名/门槛/行业约束原因分支；
   - 首持仓建立、卖出置换逻辑仿旧类结构（held→sell 不在 target、target→buy 未持有）。
3. **run_daily.py 收尾接线**：生成看板之前调用 `riskmodel.export_exposures()`（try/except，
   失败 log.warning + 继续，不告警不阻断——展示层增强，非关键路径）。
4. **五关报告**：`python backtest.py report <sid>` 与 `python validate.py <sid>` 跑两个新 sid，
   生成 reports/*.md；报告中附与旧版本（s1@v2 / s4@v1）同期指标对比一行；结论如实（不达标就写不达标）。
5. **config.yaml** strategies 段加两个新 sid: true（旧的不动）。
6. **STRAT_META 补两条**（若与卡K并行冲突，以先合入者为准，后者补齐）：
   名称"红利低波·Barra增强" / "多因子价值增强(沪深300)"，factors 表列 z 分权重与约束。
7. **旧行为零变化验证**（红线）：临时对比脚本（放 测试/过程文件/，不入库）：取 2-3 个历史月末调仓日，
   对 s1@v1、s1@v2、s4@v1 分别跑 generate_orders，改前后逐字段 diff=0；输出留档到交付说明。
8. **验收**：五关报告与 validate 报告生成；`python run_daily.py --only s1_dividend@v3`、`--only s4_smallcap@v2`
   本地跑通；state/factor_exposure.json 生成且含全部启用策略；原 tests + test_factors 全绿；
   registry/config 旧条目 diff=0。

### 卡K（P1·可与卡J并行）：看板改版——逻辑折叠 + methodology.html + 风格暴露展示

只改 report_html.py（含新增 generate_methodology()）。产出仍是零外部依赖、离线可开、手机自适应静态页。

1. **策略卡逻辑折叠（用户需求4）**：`_factor_block(meta)` 的输出（tagline+因子表+调仓行）整体包进
   `<details class='intro'><summary>📖 策略说明与选股因子</summary>…</details>`，默认收起；
   details 内容末尾加 `<a href='methodology.html#<sid>'>完整方法论 →</a>`。
   卡片头保留：名称 + 风险星级 + 状态徽章（这些是标识不是"投资逻辑"，保留）。
2. **新增 docs/methodology.html**（generate_methodology()，与 index 同一次 generate() 里生成）：
   - 顶部锚点导航（各策略 + 因子工程方法论）；
   - 每策略一节（id=sid）：名称/定位/完整因子与权重表/调仓与持仓规则/风险星级依据/适合资金——
     内容以 STRAT_META 为基础扩写，v3/v2 新策略写明与旧版差异；
   - 「因子工程与风险模型」一节：预处理管线（去极值/标准化/正交化，公式+通俗解释）、
     六个风险因子定义、风险模型（WLS 因子收益→EWMA 协方差→预测波动）、
     与 Barra CNE6 / Axioma 的关系与全部简化声明（第二、三、四节内容的通俗化改写）、
     免费数据局限（无分析师预期/杠杆/真实流通股本等）；
   - 页脚免责同 index。
3. **策略卡新增「⚖️ 风格暴露」折叠区**（置于持仓表之后）：读 state/factor_exposure.json；
   - summary 行：`⚖️ 风格暴露与预测波动（年化 x.x%）`；etf_only 策略 summary 显示"（ETF策略，不适用个股风格因子）"且无展开内容或仅一行说明；
   - 展开内容：六因子水平条形（纯 CSS：中心零轴、宽度∝|z| 裁剪至[-2,2]、左负右正），
     颜色用**中性蓝灰**（勿用盈红亏绿——暴露非盈亏）；每条右侧标注数值（+1.23 格式）；
     底部一行小字链接 methodology.html#risk 解释"暴露怎么读"；
   - json 不存在 / 该 sid 缺失 / 字段异常 → **整块不渲染**，页面其余部分不受影响（try/except）。
4. **页脚"使用说明"段**（V2 加的资金配比建议等投资建议类文案）改为 `<details>` 折叠，summary="📖 使用说明与资金配比建议"。
5. **验收**：本地 `python report_html.py` 生成 index/trades/methodology 三页；离线打开正常、无外链资源；
   锚点跳转正确；375px 与 720px 无横向滚动；删掉 factor_exposure.json 后页面仍正常生成；
   原 tests 全绿；盈红亏绿规范未被破坏（暴露条为中性色）。

---

## 六、执行顺序与分工备忘

- **卡I 先行**（基础设施，卡J 强依赖其 API）→ **卡J 与 卡K 并行**（K 对 J 仅弱依赖：
  factor_exposure.json 缺失时优雅降级，故可并行）。
- 每卡完成后 commit 注明卡号（card-I / card-J / card-K）；统筹会话（Fable）复核点：
  test_factors 输出、五关报告结论、三页渲染与折叠交互、旧行为零变化证据；复核通过后统一推送并
  dispatch daily 工作流验证线上生成。
- 回测/validate 计算量提示：validate 会多次重跑回测——**factors.compute_exposures 的进程级缓存是硬要求**，
  否则五关跑不完。
