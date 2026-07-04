# -*- coding: utf-8 -*-
"""L1 大模型消息面档(SPEC_NEWS N2,可选)。把当日快讯标题交给低成本模型产出严格JSON。
需 config news_layer.llm=true + Secrets ANTHROPIC_API_KEY。任何失败由调用方回退 L0。
只评估已发生事实,不预测、不荐股(见 prompts/news_daily.txt)。"""
import json
import logging

import conf

log = logging.getLogger("news_llm")
MODEL = "claude-haiku-4-5-20251001"     # 低成本档
MAX_TITLES = 200
PROMPT_PATH = conf.PROMPTS_DIR / "news_daily.txt"


def _client():
    import anthropic
    key = conf.secret("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("缺 ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=key)


def market_score(date, titles, cfg, holdings=None):
    """返回 dict{market_score,top_risks,top_positives,holdings_flags};解析失败抛异常(调用方回退L0)。"""
    titles = list(titles)[:MAX_TITLES]
    tmpl = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = tmpl.replace("{titles}", "\n".join(f"- {t}" for t in titles)) \
                 .replace("{holdings}", ", ".join(holdings or []))
    client = _client()
    resp = client.messages.create(
        model=MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    # 容错:抽取第一个 JSON 对象
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e < 0:
        raise ValueError(f"L1 无JSON: {text[:80]}")
    data = json.loads(text[s:e + 1])
    data["market_score"] = max(-2, min(2, int(data.get("market_score", 0))))
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        print(market_score("2026-07-03", ["某公司被立案调查", "央行降准0.5个百分点"],
                           conf.load_config(), holdings=["sz000001"]))
    except Exception as e:
        print("L1 不可用(预期,未配key/SDK):", e)
