# 消息面 L1(大模型)影子评估报告

- 生成日:2026-07-04
- 影子样本:记录 0 天,可评估(有次日收益) 0 天;背离(L1更保守) 0 天

## 结论:**数据不足(需≥10个交易日)**
请在 config.yaml 设 `news_layer.llm_shadow: true` 并配 ANTHROPIC_API_KEY,
让系统每日记录 L0/L1 分数(不影响交易),累计约2周后重跑 `python eval_news.py`。
