# -*- coding: utf-8 -*-
"""L1 大模型消息面档(SPEC_NEWS N2,可选)。支持 GLM-4.7-Flash / Anthropic / MiMo 多通道(OpenAI 兼容)。
需 config news_layer.llm=true + 对应 API KEY。
只评估已发生事实,不预测、不荐股(见 prompts/news_daily.txt)。"""
import json
import logging
import os
from pathlib import Path

import conf

log = logging.getLogger("news_llm")

PROMPTS_DIR = Path(__file__).parent / "prompts"

# 默认模型配置
DEFAULT_PROVIDERS = {
    "glm": {   # 智谱 GLM-4.7-Flash(免费、OpenAI 兼容);当前默认通道
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.7-flash",
        "api_key_env": "GLM_API_KEY",
    },
    "mimo": {  # 兼容旧配置(api.xiaomimimo.com 端点已停用,勿作默认)
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5",
        "api_key_env": "MIMO_API_KEY",
    },
    "anthropic": {
        "base_url": None,
        "model": "claude-haiku-4-5-20251001",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "agnes": {  # 备用 OpenAI 兼容通道(agnes-2.0-flash);base_url 由 config.news_layer.llm_base_url 提供
        "base_url": None,
        "model": "agnes-2.0-flash",
        "api_key_env": "AGNES_LLM_KEY",
    },
}

MAX_TITLES = 200


def _get_llm_config(cfg):
    """获取 LLM 配置(从 config 或默认值)。"""
    nl = cfg.get("news_layer") or {}
    provider = nl.get("llm_provider", "glm")
    defaults = DEFAULT_PROVIDERS.get(provider, DEFAULT_PROVIDERS["glm"])
    return {
        "provider": provider,
        "base_url": nl.get("llm_base_url", defaults["base_url"]),
        "model": nl.get("llm_model", defaults["model"]),
        "api_key_env": nl.get("llm_api_key_env", defaults["api_key_env"]),
    }


def _get_api_key(api_key_env):
    """从环境变量获取 API key;回退到本地密钥文件(仓库外的 .workbuddy/,已被 gitignore,不落盘)。"""
    key = os.environ.get(api_key_env, "")
    if not key:
        # 尝试从 conf.secret(环境变量)获取
        try:
            key = conf.secret(api_key_env)
        except Exception:
            pass
    if not key:
        # 回退:仓库外 .workbuddy/llm_key.txt(用户本地保管,不进 git)
        try:
            p = Path(__file__).resolve().parent.parent / ".workbuddy" / "llm_key.txt"
            if p.exists():
                key = p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return key


def _call_openai_compatible(base_url, model, api_key, messages, max_tokens=1024, provider="", timeout=90):
    """调用 OpenAI 兼容的 /chat/completions 端点(GLM-4.7-Flash / MiMo / agnes 等)。
    base_url 需含到版本前缀,本函数只补 /chat/completions。空内容/超时一律抛错,交由上层重试。"""
    import requests
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    # GLM-4.7-Flash 默认开启深度思考;禁用以拿到干净 JSON、更快更省 token
    if provider == "glm":
        payload["thinking"] = {"type": "disabled"}
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not content or not content.strip():
        raise ValueError(f"LLM 返回空内容(可能过载/超时): {str(data)[:160]}")
    return content


def _call_anthropic(model, api_key, messages, max_tokens=1024):
    """调用 Anthropic API。"""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
        temperature=0.1,
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def _parse_json_response(text):
    """容错解析 JSON 响应(支持 ```json 代码块围栏)。空文本直接判错。"""
    if not text or not text.strip():
        raise ValueError(f"LLM 无JSON: {text[:200]}")
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e < 0:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if m:
            return json.loads(m.group(1))
        raise ValueError(f"LLM 无JSON: {text[:200]}")
    return json.loads(text[s:e + 1])


def _call_llm(prompt, cfg, max_tokens=1024):
    """统一 LLM 调用入口(含重试),返回文本响应。失败抛错(由调用方兜底)。"""
    llm_cfg = _get_llm_config(cfg)
    api_key = _get_api_key(llm_cfg["api_key_env"])
    if not api_key:
        raise RuntimeError(f"缺 {llm_cfg['api_key_env']}")

    messages = [{"role": "user", "content": prompt}]
    provider = llm_cfg["provider"]
    last_err = None
    for attempt in range(3):                      # 端点偶发慢/空响应:重试 3 次
        try:
            if provider == "anthropic":
                return _call_anthropic(llm_cfg["model"], api_key, messages, max_tokens)
            # glm / mimo / agnes 等 OpenAI 兼容端点
            return _call_openai_compatible(llm_cfg["base_url"], llm_cfg["model"], api_key,
                                           messages, max_tokens, provider=provider)
        except Exception as e:
            last_err = e
            log.warning("LLM 调用失败(第%d次,%.1fs后重试): %s", attempt + 1, 2 + attempt * 2, repr(e)[:140])
            import time; time.sleep(2 + attempt * 2)
    raise last_err


def market_score(date, titles, cfg, holdings=None):
    """市场级风险分 + 持仓风险。返回 dict{market_score, top_risks, top_positives, holdings_flags}。"""
    titles = list(titles)[:MAX_TITLES]
    tmpl = (PROMPTS_DIR / "news_daily.txt").read_text(encoding="utf-8")
    prompt = tmpl.replace("{titles}", "\n".join(f"- {t}" for t in titles)) \
                 .replace("{holdings}", ", ".join(holdings or []))

    text = _call_llm(prompt, cfg)
    data = _parse_json_response(text)
    data["market_score"] = max(-2, min(2, int(data.get("market_score", 0))))
    return data


def industry_themes(date, titles, cfg):
    """产业主题分析。返回 dict{themes: [...], sector_score: {etf_code: score}}。"""
    titles = list(titles)[:MAX_TITLES]
    tmpl = (PROMPTS_DIR / "news_industry.txt").read_text(encoding="utf-8")
    prompt = tmpl.replace("{titles}", "\n".join(f"- {t}" for t in titles))

    text = _call_llm(prompt, cfg, max_tokens=2048)
    data = _parse_json_response(text)

    # 验证结构
    if "themes" not in data:
        data["themes"] = []
    if "sector_score" not in data:
        data["sector_score"] = {}

    # 限制分数范围
    for k in data["sector_score"]:
        data["sector_score"][k] = max(-2, min(2, int(data["sector_score"][k])))

    return data


def stock_sentiment(date, code, news_text, cfg):
    """个股新闻语义分析。返回 dict{sentiment, score, key_events, risk_level}。"""
    if not news_text.strip():
        return {"sentiment": "neutral", "score": 0, "key_events": [], "risk_level": "low"}

    tmpl = (PROMPTS_DIR / "news_stock.txt").read_text(encoding="utf-8")
    prompt = tmpl.replace("{code}", code).replace("{news}", news_text[:3000])

    try:
        text = _call_llm(prompt, cfg, max_tokens=512)
        data = _parse_json_response(text)
        data["score"] = max(-2, min(2, int(data.get("score", 0))))
        return data
    except Exception as e:
        log.warning("个股语义分析失败 %s: %s", code, e)
        return {"sentiment": "neutral", "score": 0, "key_events": [], "risk_level": "low"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = conf.load_config()
    print(f"LLM 配置: {_get_llm_config(cfg)}")
    # 测试连通性
    try:
        result = industry_themes(
            "2026-07-06",
            ["国务院发文支持存储芯片国产替代", "财政部加大集成电路产业补贴力度"],
            cfg
        )
        print("产业主题测试:", json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"LLM 不可用: {e}")
