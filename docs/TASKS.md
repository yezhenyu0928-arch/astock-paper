# 任务卡(每张 = 一个新AI会话,按序执行)

统一开场白(每张卡前都贴):
```
背景:A股多策略模拟跟单系统。仓库已有冻结文件:models.py、strategies/base.py、
schema.sql、config.example.yaml、registry.yaml、.github/workflows/daily.yml——一律不得修改。
唯一实现依据:docs/SPEC.md 对应模块章节。硬性要求:
① 函数签名/表名/字段/消息模板与SPEC完全一致,不得自行发明;
② Python3.11,依赖不超出 requirements.txt;
③ 交付必须包含 tests/ 下的验收测试代码,并展示运行通过的输出;
④ 取数只经 data_adapter.py;⑤ SPEC附B列出的事情一律不做。
```

## M1(会话1)
实现 SPEC 模块1:data_adapter.py、calendar.py、data.py(update_all+check)。
先确认当前 akshare 版本并核对接口名,失效接口改用备源并在代码注释说明。
交付:三个文件 + tests/test_m1.py + 测试通过输出。

## M2(会话2,附上M1产物)
实现 SPEC 模块2:engine.py(Engine 类含 DataContext 实现)。
交付:engine.py + tests/test_m2.py(覆盖SPEC列出的6个用例)+ 通过输出。

## M3(会话3,附M1/M2产物)
实现 SPEC 模块3-S2 + 用 Engine 做 2022-01-01~今 历史回放 backtest.py;
产出 reports/s2_etf@v1.md 五关验证报告(报告结构见SPEC模块3末尾)。
通过后把 registry.yaml 的 frozen_date 填为当日(这是唯一允许改 registry 的情形:填空值)。

## M4(会话4)
实现 SPEC 模块5:notify.py。交付含"断主通道走邮件"的可复现测试说明。

## M5(会话5,附全部产物)
实现 SPEC 模块6:run_daily.py;本地串通全流程;写部署自检清单。

## M6(会话6)
实现 SPEC 模块7 前4页 dashboard.py。附手机宽度截图或说明。

—— 到此 MVP 完成,开启 S2 实时模拟赛马,以下与观察期并行 ——

## P7(会话7):SPEC 模块8 基本面补数 + index_members 抓取,抽查3个历史调整日。
## P8(会话8):SPEC 模块4 risk.py 完整版(替换MVP精简版),含全部5条规则测试。
## P9(会话9-12):S1 / S3 / S4 / S5 各一张卡,格式同M3,各附五关报告。
## P10(会话13):dashboard 补第5/6页 + 决策日志。

## 维护卡(不定期)
akshare 升级后接口失效:只修 data_adapter.py,跑 tests/test_m1.py 回归。

—— 消息面情绪层(依据 docs/SPEC_NEWS.md,可在 M6 后任意时点插入)——

## P11(会话):news_adapter.py + news_raw/news_signal 建表 + 去重。验收见 SPEC_NEWS N7。
## P12(会话,附risk.py):news_engine.py L0词表档 + exposure_mult 接入 + 三类消息模板。
## P13(会话):run_intraday.py + daily.yml 增加盘中三个cron(UTC 02:00/05:30/06:45 工作日)。
## P14(可选):L1大模型档 + 有/无消息层对比回测报告;Calmar未提升则默认关闭该层。

—— 真实成交建模与小资金适配(依据 docs/SPEC_FILL.md,覆盖原SPEC滑点规则)——

## M2/M3/P9 验收追加:见 SPEC_FILL F4(实现这些卡时把 SPEC_FILL.md 一并附上)。
## P15(会话):validate.py 蒙特卡洛(200次)+ 双重成本加压;入池依据改为净值5%分位。
