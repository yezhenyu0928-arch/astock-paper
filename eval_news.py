# -*- coding: utf-8 -*-
"""消息面 L1(大模型)影子评估(卡F / SPEC_NEWS P14)。
读 state/news_shadow.csv(影子模式每日记录的 L0规则分 vs L1大模型分)+ 沪深300次日收益,
评估"若采纳 L1 更保守的降险信号"相对纯 L0 是否改善:
  - 命中率:L1 比 L0 更保守(降险)的日子里,次日沪深300是否确实下跌(降险正确);
  - 误报率:L1 降险但次日上涨(错失/踏空);
  - 敞口净值模拟:分别按 L0-only 与 L0∪L1(取更保守)的 exposure_map 施加于沪深300日收益,比较累计与Calmar。
判据:L1 在样本内 命中率>误报率 且 敞口净值Calmar不劣于 L0 → 建议转正式 llm:true;否则维持关闭。
用法:python eval_news.py   (输出 reports/news_llm_eval.md)"""
import csv
import logging

import conf
import util
from db import get_conn

log = logging.getLogger("eval_news")

MIN_DAYS = 10          # 至少10个交易日(约2周)才给结论


def _load_shadow():
    p = conf.STATE_DIR / "news_shadow.csv"
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)]


def _next_ret(conn, date):
    """沪深300 次日收益(date 之后第一个交易日 close/当日 close - 1)。"""
    cur = conn.execute("SELECT close FROM daily_bar WHERE code='sh000300' AND trade_date<=? "
                       "ORDER BY trade_date DESC LIMIT 1", (date,)).fetchone()
    nxt = conn.execute("SELECT close FROM daily_bar WHERE code='sh000300' AND trade_date>? "
                       "ORDER BY trade_date LIMIT 1", (date,)).fetchone()
    if not cur or not nxt or not cur[0]:
        return None
    return nxt[0] / cur[0] - 1


def evaluate():
    rows = _load_shadow()
    conn = get_conn()
    cfg = conf.load_config()
    emap = (cfg.get("news_layer") or {}).get("exposure_map",
                                             {-2: 0.0, -1: 0.5, 0: 1.0, 1: 1.0, 2: 1.0})

    def expo(score):
        return float(emap.get(int(score), emap.get(str(int(score)), 1.0)))

    n = len(rows)
    diverge = hit = miss = 0
    nav_l0 = nav_l1 = 1.0
    rets_l0, rets_l1 = [], []
    detail = []
    for r in rows:
        try:
            l0, l1 = int(float(r["l0_score"])), int(float(r["l1_score"]))
        except Exception:
            continue
        nr = _next_ret(conn, r["date"])
        if nr is None:
            continue
        e0, e1 = expo(l0), expo(min(l0, l1))     # L1档=取更保守
        nav_l0 *= (1 + e0 * nr)
        nav_l1 *= (1 + e1 * nr)
        rets_l0.append(e0 * nr); rets_l1.append(e1 * nr)
        if l1 < l0:                               # L1 更保守(降险)
            diverge += 1
            if nr < 0:
                hit += 1
            else:
                miss += 1
            detail.append((r["date"], l0, l1, nr))
    conn.close()

    def calmar(navs_rets):
        import math
        if len(navs_rets) < 2:
            return 0.0
        nav = 1.0; peak = 1.0; mdd = 0.0
        for x in navs_rets:
            nav *= (1 + x); peak = max(peak, nav); mdd = max(mdd, 1 - nav / peak)
        ann = nav ** (252 / len(navs_rets)) - 1
        return (ann / mdd) if mdd > 1e-9 else 0.0

    cal0, cal1 = calmar(rets_l0), calmar(rets_l1)
    usable = len(rets_l0)
    L = ["# 消息面 L1(大模型)影子评估报告", "",
         f"- 生成日:{util.today_str()}",
         f"- 影子样本:记录 {n} 天,可评估(有次日收益) {usable} 天;背离(L1更保守) {diverge} 天", ""]
    if usable < MIN_DAYS:
        L += [f"## 结论:**数据不足(需≥{MIN_DAYS}个交易日)**",
              "请在 config.yaml 设 `news_layer.llm_shadow: true` 并配 ANTHROPIC_API_KEY,",
              "让系统每日记录 L0/L1 分数(不影响交易),累计约2周后重跑 `python eval_news.py`。", ""]
    else:
        hit_rate = hit / diverge if diverge else 0.0
        L += ["## 评估结果",
              f"- L1 降险命中(次日确实跌):{hit}/{diverge} = {hit_rate:.0%}",
              f"- L1 降险误报(次日反涨):{miss}/{diverge}",
              f"- 敞口模拟累计:L0-only {nav_l0-1:+.2%} vs L0∪L1 {nav_l1-1:+.2%}",
              f"- 敞口模拟 Calmar:L0 {cal0:.2f} vs L0∪L1 {cal1:.2f}", ""]
        better = (hit_rate > 0.5) and (cal1 >= cal0 - 1e-9) and (nav_l1 >= nav_l0 - 1e-9)
        L += [f"## 结论:**{'建议转正式 llm:true' if better else '维持 llm:false(L1未显现改善)'}**",
              f"- 判据:命中率>50% 且 敞口净值/Calmar 不劣于纯L0 → {'满足' if better else '不满足'}", ""]
        if detail:
            L += ["## 背离明细(L1更保守的日子)", "| 日期 | L0 | L1 | 次日沪深300 |", "|---|---|---|---|"]
            for d, a, b, nr in detail[:30]:
                L.append(f"| {d} | {a} | {b} | {nr:+.2%} |")
    text = "\n".join(L)
    out = conf.REPORTS_DIR / "news_llm_eval.md"
    out.write_text(text, encoding="utf-8")
    return str(out), usable


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    path, usable = evaluate()
    print("评估报告:", path, f"(可评估 {usable} 天)")
