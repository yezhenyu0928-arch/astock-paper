# A股回测项目 — 交接文档（WorkBuddy → Claude Code）

> 生成时间：2026-07-24。本文档供 Claude Code 接手后直接执行，所有结论均经实跑/静态审计验证，非推测。
> 项目根目录：`C:\Users\zhenyu\Desktop\测试\astock-paper`

---

## 0. 一句话现状

代码重构**已完成**并通过静态审计 + 首次实跑验证（段错误已修复），但**尚未拿到 6 策略的真实年化/回撤数字**——WorkBuddy 起的搜索后台任务被用户中断放弃了。数据库完好（258 万行日线），GitHub 连接器断开，云端无法跑。

---

## 1. 硬目标（不可让步，达不成就继续改）

| 目标 | 要求 |
|---|---|
| 年化收益率 | **> 10%** |
| 最大回撤 | **< 10%** |
| 双达标 | 两者**同时**满足才算成功；任一不达标就继续迭代 |
| 各策略本金 | 全部 **10 万元**（已确认 `config.yaml` → `capital: 100000`） |
| 策略逻辑 | 可随意修改（用户明确："我重构策略逻辑"） |
| 看板说明 | 需标注各策略**调仓频率**（weekly / daily） |
| 大佬逻辑调研 | 参考用，非必须（公开资料里有没有可借鉴的炒股逻辑） |

**历史背景**：6 个 @v3 策略均为纯多头红利/价值类，在 A股 2022–2025 熊市**结构性亏损**。已知唯一可信基线：`s1_dividend@v3` 重构前 年化 **-1.6%**、回撤 **10.6%**、Sharpe -0.33。

---

## 2. 项目结构 & 关键文件

```
C:\Users\zhenyu\Desktop\测试\astock-paper\
├── macro.py            ⭐核心：macro_exposure_mult() 已重写为"大盘趋势择时总开关"
├── search_arms.py     ⭐多臂参数搜索（5 臂 × 6 策略）
├── backtest_one.py    ⭐单策略回测 CLI（子进程隔离用，修复段错误的关键）
├── run_search_now.py  ⭐自走脚本（跳过建库、吃现有 DB、跑搜参、推送）
├── run_local.bat      ⭐原 bat，第4步已改为 search_arms
├── config.yaml        capital: 100000（已确认）
├── db/
│   └── market.sqlite  ⭐524MB，daily_bar=2581518行，mainboard=3044只，sh510300=1833根
├── registry.yaml      6 个 @v3 策略定义
├── risk.py            _exposure_mult = min(news_mult, macro_mult)；L205 把 mult 乘到每张买单权重
├── backtest.py        run_backtest(sid,start,end,capital) → r["metrics"]{annual,max_dd,sharpe,...}
└── reports/          各策略五关报告（*.md），命名 sid 中 @ 替换为 _at_
```

**6 个 @v3 策略**（`registry.yaml`，`DEFAULT_SIDS`）：
- `s1_dividend@v3`（大盘红利，weekly）
- `s15_core_allocation@v3`（大盘核心，weekly）
- `s8_checklist@v3`（全市场低波，weekly，关分红）
- `s4_smallcap@v3`（中小盘，daily）
- `s13_growth_quality_rotation@v3`（中盘成长，daily）
- `s14_value_reversal_rotation@v3`（中小盘价值反转，daily）

---

## 3. 已完成的重构（代码层面，接手者无需重做）

### 3.1 大盘趋势择时总开关（`macro.py` → `macro_exposure_mult`，约 L554-615）

这是本次重构核心。用沪深300（`sh510300`）的 MA50/MA200 + 市场 regime 做**仓位分档**，返回值经 `risk._exposure_mult` 直接乘到每张买单权重 → **6 策略一处改、全体降回撤**。

```python
def macro_exposure_mult(date, ctx, cfg=None):
    import os
    try:
        conn = getattr(ctx, "conn", None)
        if conn is None:
            return 1.0
        score, _ = macro_score_7(date, conn=conn, cfg=cfg)
        rg = compute_market_regime(date, conn=conn)
    except Exception:
        return 1.0
    base = float(os.environ.get("MACRO_BASELINE", "0.85"))
    bear_floor = float(os.environ.get("MACRO_BEAR_FLOOR", "0.15"))
    weak_mult = float(os.environ.get("MACRO_WEAK_MULT", "0.45"))
    ma200_mult = float(os.environ.get("MACRO_MA200_MULT", "0.65"))
    regime = rg.get("regime", "")
    above50 = rg.get("aboveMa50"); above200 = rg.get("aboveMa200")
    risk_ratio = rg.get("risk_ratio")
    macro_tilt = (base + score * 0.10) if score >= 0 else (base + score * 0.60)
    if regime in ("数据不足", ""):
        return float(max(0.10, min(0.90, macro_tilt)))
    if regime == "风险" or (above200 is False and above50 is False):
        trend_mult = bear_floor          # 0.15 → 85% 现金
    elif regime == "转弱" or above50 is False:
        trend_mult = weak_mult            # 0.45
    elif above200 is False:
        trend_mult = ma200_mult          # 0.65
    else:
        trend_mult = base                # 0.85
    if risk_ratio is not None and risk_ratio >= 0.5 and trend_mult > 0.30:
        trend_mult = 0.30
    mult = min(trend_mult, macro_tilt)
    return float(max(0.05, min(0.90, mult)))
```

**四档阈值可由环境变量覆盖**（多臂搜参用）：
`MACRO_BASELINE` / `MACRO_BEAR_FLOOR` / `MACRO_WEAK_MULT` / `MACRO_MA200_MULT`

**依赖确认**（曾担心漏数据，已核实）：`compute_market_regime` 返回的 key 正是 `aboveMa50`/`aboveMa200`/`risk_ratio`/`regime`；`macro_score_7` 返回 `(score, regime)` 元组。`_REGIME_BENCHES[0]` = `("sh510300","沪深300")`，趋势闸读对了指数。

### 3.2 多臂参数搜索（`search_arms.py`）

- **5 组臂**（每组 4 个 MACRO_* 阈值）：
  - `A_base` = 0.85 / 0.15 / 0.45 / 0.65
  - `B_defensive` = 0.80 / 0.10 / 0.35 / 0.55
  - `C_aggressive` = 0.90 / 0.20 / 0.55 / 0.75
  - `D_ultradef` = 0.85 / 0.05 / 0.30 / 0.50
  - `E_mid` = 0.88 / 0.12 / 0.40 / 0.60
- **流程**：扫 5 臂 × 6 策略主回测 → 每策略挑最优臂 → 用其 env 重生成五关报告 + 蒙卡（非 `--no-final` 时）。
- **产出**：`reports/_arm_search.md`（榜单）+ `_arm_best.json`。
- **评分**：`_score = annual*100 - max(0, max_dd-TARGET)*300 + (100 if passed)`（达标优先）。
- **CLI**：`--quick`（前 3 臂）/ `--no-final`（不重生成五关）/ 跟 `sid` 限定策略。
- `_run_main(sid, capital)` 已改为 **subprocess 调 `backtest_one.py`**（见 3.3）。

### 3.3 段错误修复（关键！否则搜参必崩）

**根因**：`run_backtest` 内部 `cache_bars=True` 把全市场日线缓存进内存；`search_arms` 在**同一进程内连跑 18~30 次**回测 → 内存暴涨 → C 层段错误 **`0xC0000005`（退出码 `3221225477`）**。Python 的 `try/except` 抓不住段错误，直接干掉 `search_arms` 自身 → **连 `_arm_search.md` 都来不及写**。

**修复**：新建 `backtest_one.py`（单策略回测 CLI，只向 stdout 写一行 JSON metrics），`search_arms._run_main` 改为 `subprocess` 调它。每次回测在**独立解释器**内运行，进程结束内存自动释放；某次若仍段错误，只拖垮子进程，主进程捕获 stderr 跳过该组合，不会整次搜索崩掉。

`backtest_one.py` 用法：
```
python backtest_one.py <sid> <start> <end> <capital>
# 从环境变量读 MACRO_* 阈值；成功输出一行 JSON(metrics)；失败非零退出+stderr
```

---

## 4. 当前未完成的任务（Claude Code 接手后要做）

### 4.1 【最高优先】拿到真实数字
跑 `search_arms` 得到 6 策略各最优臂的 **年化 / 回撤 / 是否达标（>10% & <10%）**。

推荐命令（**必须在本机、用系统 Python**）：
```bat
cd C:\Users\zhenyu\Desktop\测试\astock-paper
C:\Users\zhenyu\AppData\Local\Programs\Python\Python313\python.exe run_search_now.py --quick --no-push
```
- 先用 `--quick --no-push` 验证链路不崩、能产出 `reports/_arm_search.md`，再跑**全量**（去掉 `--quick`，去掉 `--no-push` 以推送）。
- **必须用系统 Python**（`C:\Users\zhenyu\AppData\Local\Programs\Python\Python313\python.exe`）：依赖（pandas 3.0.3 / numpy / akshare / baostock）装在它下面，C 扩展稳定。WorkBuddy 的 managed 版（`.workbuddy/binaries/python/...`）虽也能跑但曾崩，优先系统版。
- **不要重建库**：`run_search_now.py` 已跳过建库（建库是卡死/假成功的元凶，A股数据源海外不可达）。它只校验现有 `db/market.sqlite`（daily_bar≥5万 / mainboard≥800 / sh510300≥200），实测 258万/3044/1833，充足。
- 若遇锁文件残留（`run_search_now.lock`），先 `del run_search_now.lock` 再跑。

### 4.2 若回撤达标但年化仍 < 10% → 换动量因子
用户选项 D 的另一半：趋势闸主要压**回撤**（硬约束那一半）。若回撤压住了但年化不够，换 2022–25 真正有效的**动量/趋势因子**（红利/价值类在熊市结构性无效）。闸门是"一处改全体生效"，因子调整只在 `mf_core.py` / 各策略 params，不冲突。

### 4.3 推送到 GitHub
- GitHub 连接器**当前断开**。恢复方式：本机 `git push`（用户机器直连），或等 WorkBuddy/Claude Code 的 GitHub connector 重连后用 `_api_push.py`。
- ⚠️ **历史污染**：之前 WorkBuddy 的每小时自动化（`automation-1784880286669`，**现已 PAUSED**）曾把仓库里 **Jul 11 的旧 `s1_dividend_at_v3.md`** 推上去污染提交历史（commit `313474f`）。接手者跑出新结果覆盖即可。

### 4.4 方案 B（云端无人值守，未做）
本机建好的 `db/market.sqlite` → 上传成 GitHub **Release 资产 / LFS** → 改 `daily.yml` 让它**下载预建库**而非现抓 A股数据。这样云端日常盘才能稳定吃真实数据（详见第 5 节"云端结构性跑不了"）。此项尚未实现。

---

## 5. 已知坑 & 教训（避免重踩）

1. **云端（海外 GitHub runner）结构性跑不了本项目**：`daily.yml` 跑在 `ubuntu-latest`，连 akshare/baostock/东财**全部超时不可达**，建不出库；且 258 万行数据库**只在本机**。除非做方案 B 上传预建库，否则云端只会产出空 / 假 0%（之前流水线 #40 "假成功全是 0" 正是此因）。
2. **段错误必现**：同进程连跑 18+ 次回测会爆内存 → 必须在**子进程**里跑（已用 `backtest_one.py` 修好），切勿改回同进程调用。
3. **`self_heal` 自杀 bug（已修）**：原 `taskkill /F /IM python.exe` 会把自己（也是 python.exe）一起杀。已改为排除自身 PID，只清**其它**残留进程。
4. **`self_heal` 的 `tasklist` 返回 None（已修）**：加 `or ""` 防御，避免 `splitlines` 报错。
5. **命令执行后端会反复掉线**：WorkBuddy 的 Bash/PowerShell 曾 `exit 1` 空返回（connector disconnected），但**文件工具（Read/Write/Edit/Glob/Grep）不受影响**。若 Claude Code 也遇到跑不了命令，先确认执行后端是否在线。
6. **Git Bash 中文路径坑**：`git -C "/c/Users/zhenyu/Desktop/测试/..."` 会报 "No such file or directory"（非 ASCII 路径解析问题）。改用 `cd` 进目录再 `git`，或换 PowerShell。
7. **`run_daily.py` 自检**：若 `daily_bar < 50000` 行会直接 abort。现有库 258 万行，充足。
8. **趋势闸数据依赖已核实**：`sh510300` 全历史（1833 根）已在 `daily_bar`，`OPTIMIZE_V3.md` 也确认；闸在现有库上能真正触发，不会静默失效。

---

## 6. 立即可执行的下一步（给 Claude Code 的剧本）

```bat
REM Step 1: 进项目目录（避开中文路径的 git -C 坑，用 cd）
cd C:\Users\zhenyu\Desktop\测试\astock-paper

REM Step 2: 杀干净可能残留的 python 回测进程（排除自身），清锁
taskkill /F /IM python.exe  REM 注意：若 Claude Code 自身也是 python 跑的，先排除自身 PID
del /F run_search_now.lock 2>nul

REM Step 3: 先用 quick 验证链路（不推送，避免污染 GitHub）
C:\Users\zhenyu\AppData\Local\Programs\Python\Python313\python.exe run_search_now.py --quick --no-push

REM Step 4: 读榜单，逐策略核对 年化>10% & 回撤<10%
REM 打开 reports/_arm_search.md

REM Step 5: 达标 → 跑全量并推送；不达标 → 调 MACRO_* 阈值 / 换动量因子，迭代
C:\Users\zhenyu\AppData\Local\Programs\Python\Python313\python.exe run_search_now.py
```

**预期耗时**：`--quick`（3 臂 × 6 = 18 次回测）约 15~25 分钟（daily 策略 s13/s14 单次较慢）；全量（5 臂 × 6 = 30 次）更久。每次回测是独立子进程，内存不累积。

---

## 7. 用户偏好 & 情绪（沟通参考）

- **不双击 .bat**：明确要求助手**自己执行并自己修复问题**，不要甩回给用户。
- **中文交流**。
- **情绪**：对本项目反复"假成功 / 命令掉线 / 段错误"已不耐烦，**要看到真实可验证的数字**，不要再给"快出结果了"这类空头承诺。
- 用户曾提议"本地+云端并行不同参数，哪个达标用哪个"——架构上可行，但云端受第 5.1 节限制，需先做方案 B。
