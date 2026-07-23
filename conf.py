# -*- coding: utf-8 -*-
"""配置加载 + 路径常量 + 秘钥注入 + risk_override 校验。
SPEC 模块0:config.yaml → dict,秘钥从环境变量覆盖注入。
SPEC_FILL F3:custom.risk_override 只允许比默认更严,放松一律拒绝加载并告警。
"""
import os
import logging
from pathlib import Path
import yaml

log = logging.getLogger("conf")

ROOT = Path(__file__).resolve().parent
DB_DIR = ROOT / "db"
DB_PATH = DB_DIR / "market.sqlite"
SCHEMA_PATH = ROOT / "schema.sql"
STATE_DIR = ROOT / "state"
REPORTS_DIR = ROOT / "reports"
REGISTRY_PATH = ROOT / "registry.yaml"
CONFIG_PATH = ROOT / "config.yaml"
PROMPTS_DIR = ROOT / "prompts"

for _d in (DB_DIR, STATE_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 秘钥:环境变量名 → config 里的挂载路径(仅用于运行期,不落盘)
SECRET_ENVS = ("PUSHPLUS_TOKEN", "SMTP_AUTH_CODE", "DASHBOARD_PASSWORD", "ANTHROPIC_API_KEY",
               "GLM_API_KEY", "TUSHARE_TOKEN", "AGNES_LLM_KEY")

# risk_override 校验方向:True=值越小越严(只接受更小),False=值越大越严(只接受更大)
_RISK_STRICTER_WHEN_SMALLER = {
    "strategy_max_drawdown": True,
    "max_position_pct": True,
    "min_avg_amount": False,            # 成交额门槛越高越严
    ("stop_loss", "trend"): True,
    ("stop_loss", "rotation"): True,
    ("market_freeze", "day_drop"): True,
    ("market_freeze", "m20_drop"): True,
}

# 默认数据源优先级配置。
# 云端 GitHub Actions(海外 Runner)现实:baostock/东财 push2his 常不可达;腾讯 gtimg CDN 可达。
# 故个股主源改腾讯(tencent,免费无 token),Tushare 次之(需积分,无则自动跳过),baostock 降为最后(本地仍可用)。
DEFAULT_DATA_SOURCE_PRIORITY = {
    "etf_daily": ["sina_etf", "akshare_em", "baostock"],
    "stock_daily": ["tencent", "tushare", "akshare_em", "baostock"],
    "hfq_close": ["tencent", "akshare_em", "baostock"],
    "realtime": ["tencent", "sina"],
    "calendar": ["akshare_sina", "baostock"],
    "index_daily": ["akshare"],
    "fundamental": ["tushare", "akshare_em", "baostock"],
}


def _validate_risk_override(base_risk: dict, override: dict) -> dict:
    """把 override 合并进 base_risk,只接受更严的值,放松的拒绝并告警。返回新 risk dict。"""
    risk = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base_risk.items()}
    if not override:
        return risk
    for k, v in override.items():
        if isinstance(v, dict):  # 嵌套如 stop_loss / market_freeze
            for sk, sv in v.items():
                key = (k, sk)
                cur = risk.get(k, {}).get(sk)
                smaller_stricter = _RISK_STRICTER_WHEN_SMALLER.get(key)
                if cur is None or smaller_stricter is None:
                    log.warning("risk_override 未知键 %s,忽略", key)
                    continue
                if (smaller_stricter and sv <= cur) or ((not smaller_stricter) and sv >= cur):
                    risk[k][sk] = sv
                else:
                    log.warning("🔴 risk_override 拒绝放松 %s: %s(当前=%s,只许更严)", key, sv, cur)
        else:
            cur = risk.get(k)
            smaller_stricter = _RISK_STRICTER_WHEN_SMALLER.get(k)
            if cur is None or smaller_stricter is None:
                log.warning("risk_override 未知键 %s,忽略", k)
                continue
            if (smaller_stricter and v <= cur) or ((not smaller_stricter) and v >= cur):
                risk[k] = v
            else:
                log.warning("🔴 risk_override 拒绝放松 %s: %s(当前=%s,只许更严)", k, v, cur)
    return risk


_cache = {}


def load_config(path=None, use_cache=True) -> dict:
    """读取 config.yaml,注入秘钥,应用 risk_override,合并数据源优先级配置。"""
    path = Path(path) if path else CONFIG_PATH
    if use_cache and path in _cache:
        return _cache[path]
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 秘钥注入(仅内存)
    cfg.setdefault("secrets", {})
    for env in SECRET_ENVS:
        cfg["secrets"][env] = os.environ.get(env, "")
    # risk_override(只许更严)
    override = (cfg.get("custom") or {}).get("risk_override") or {}
    cfg["risk"] = _validate_risk_override(cfg.get("risk", {}), override)
    # 数据源优先级: 配置文件覆盖默认值
    cfg["data_source_priority"] = {**DEFAULT_DATA_SOURCE_PRIORITY,
                                    **(cfg.get("data_source_priority") or {})}
    if use_cache:
        _cache[path] = cfg
    return cfg


def load_registry(path=None) -> dict:
    path = Path(path) if path else REGISTRY_PATH
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def secret(name: str) -> str:
    return os.environ.get(name, "")
