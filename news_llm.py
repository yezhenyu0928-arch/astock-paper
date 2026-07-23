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
    """构造 LLM 通道列表(主 → 备)。返回 list[dict{provider,base_url,model,api_key_env}]。
    主通道取 llm_provider;若配置了 llm_provider_backup 且不同于主通道,则追加为备通道。
    主/备分别读取带(或不带)_backup 后缀的 config 键,缺省回落 DEFAULT_PROVIDERS。"""
    nl = cfg.get("news_layer") or {}
    out = []
    seen = set()
    for key in ("llm_provider", "llm_provider_backup"):
        provider = nl.get(key, "glm" if key == "llm_provider" else None)
        if not provider or provider in seen:
            continue
        seen.add(provider)
        defaults = DEFAULT_PROVIDERS.get(provider, DEFAULT_PROVIDERS["glm"])
        suffix = "" if key == "llm_provider" else "_backup"
        out.append({
            "provider": provider,
            "base_url": nl.get(f"llm_base_url{suffix}", defaults["base_url"]),
            "model": nl.get(f"llm_model{suffix}", defaults["model"]),
            "api_key_env": nl.get(f"llm_api_key_env{suffix}", defaults["api_key_env"]),
        })
    return out


def _get_api_key(api_key_env):
    """获取 API key:环境变量 → conf.secret(环境变量) → 本地密钥文件(仓库外 .workbuddy/,gitignore,不落盘)。
    本地文件按 api_key_env 精确匹配,避免 GLM 误用 agnes 的 key。"""
    key = os.environ.get(api_key_env, "")
    if not key:
        try:
            key = conf.secret(api_key_env)
        except Exception:
            pass
    if not key:
        # 推荐:多密钥 JSON 映射 { "AGNES_LLM_KEY": "...", "GLM_API_KEY": "..." }
        try:
            p = Path(__file__).resolve().parent.parent / ".workbuddy" / "llm_keys.json"
            if p.exists():
                import json as _json
                key = _json.loads(p.read_text(encoding="utf-8")).get(api_key_env, "")
        except Exception:
            pass
    if not key and api_key_env == "AGNES_LLM_KEY":   # 兼容旧版单文件
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


def _call_llm(prompt, cfg, max_tokens=1024, attempts_per_provider=2):
    """统一 LLM 调用入口(主备通道 + 每通道内重试)。
    主通道(llm_provider)经 attempts_per_provider 次重试全失败 → 自动切备通道(llm_provider_backup)重试;
    所有通道都失败才抛错,交由调用方兜底(如 stock_sentiment 返回中性)。
    为兼容盘中 8 分钟超时,单通道重试次数保守(默认 2),避免主通道长超时把总时长拖爆。"""
    providers = _get_llm_config(cfg)
    if not providers:
        raise RuntimeError("news_layer 未配置任何 LLM 通道(llm_provider 缺失)")
    messages = [{"role": "user", "content": prompt}]
    last_err = None
    for pidx, pcfg in enumerate(providers):
        provider = pcfg["provider"]
        api_key = _get_api_key(pcfg["api_key_env"])
        if not api_key:
            log.warning("LLM 通道[%s] 缺密钥 %s,跳过该通道", provider, pcfg["api_key_env"])
            continue
        tag = "主" if pidx == 0 else f"备{pidx}"
        for attempt in range(attempts_per_provider):
            try:
                if provider == "anthropic":
                    return _call_anthropic(pcfg["model"], api_key, messages, max_tokens)
                # glm / mimo / agnes 等 OpenAI 兼容端点
                return _call_openai_compatible(pcfg["base_url"], pcfg["model"], api_key,
                                               messages, max_tokens, provider=provider)
            except Exception as e:
                last_err = e
                log.warning("LLM[%s通道 %s] 调用失败(第%d次/共%d,%.1fs后重试): %s",
                            tag, provider, attempt + 1, attempts_per_provider,
                            2 + attempt * 2, repr(e)[:160])
                import time; time.sleep(2 + attempt * 2)
        log.warning("LLM[%s通道 %s] %d次重试全失败,切下一通道", tag, provider, attempts_per_provider)
    raise RuntimeError(f"所有 LLM 通道均失败,最后错误: {last_err}")


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
