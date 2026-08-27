"""MLLM 提供商预设（OpenAI / Kimi Code / Moonshot 开放平台）。

环境变量（Kimi Coding 套餐）：

    export MLLM_PROVIDER=kimi-code
    export KIMI_API_KEY=sk-...
    export MLLM_MODEL=k3-256k         # 或 k3（1M 上下文，Allegretto+）

说明：
    * Kimi Code 会员：Base URL https://api.kimi.com/coding/v1
      K2.7 Code 的 Model ID 是 kimi-for-coding（不是版本名 "K2.7 Code"）
    * Moonshot 开放平台按量：MLLM_PROVIDER=moonshot，模型 kimi-k2.7-code
      Base URL https://api.moonshot.ai/v1
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# Kimi Code 端点上，用户口语「k2.7 code」映射到官方 Model ID
KIMI_CODE_MODEL_ALIASES: Dict[str, str] = {
    "kimi-k2.7-code": "kimi-for-coding",
    "kimi-k2.7-code-highspeed": "kimi-for-coding-highspeed",
    "k2.7-code": "kimi-for-coding",
    "kimi k2.7 code": "kimi-for-coding",
    "kimi-k2.7": "kimi-for-coding",
    "k3": "k3",
    "k3-256k": "k3-256k",
}


def _first_env(names: List[str]) -> str:
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


MLLM_PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "api_key_envs": ["OPENAI_API_KEY"],
        "base_url_env": "OPENAI_BASE_URL",
        "default_base_url": None,
        "default_model": "gpt-4o",
        "label": "OpenAI 兼容（默认官方）",
    },
    "kimi-code": {
        "api_key_envs": ["KIMI_API_KEY", "OPENAI_API_KEY"],
        "base_url_env": "OPENAI_BASE_URL",
        "default_base_url": "https://api.kimi.com/coding/v1",
        "default_model": "k3-256k",
        "label": "Kimi Code 会员（K3 视觉，默认 k3-256k）",
    },
    "moonshot": {
        "api_key_envs": ["MOONSHOT_API_KEY", "OPENAI_API_KEY"],
        "base_url_env": "OPENAI_BASE_URL",
        "default_base_url": "https://api.moonshot.ai/v1",
        "default_model": "kimi-k2.7-code",
        "label": "Moonshot 开放平台（kimi-k2.7-code 按量）",
    },
}


def resolve_mllm_config(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """合并环境变量与显式参数，返回 MLLMBackend 用的 api_key/base_url/model。"""
    provider_id = (provider or os.environ.get("MLLM_PROVIDER", "openai")).strip().lower()
    preset = MLLM_PROVIDER_PRESETS.get(provider_id, MLLM_PROVIDER_PRESETS["openai"])

    # api_key：显式传参（含空字符串，用于表达「禁用 MLLM」）优先；
    # 仅当参数为 None 时才回退读环境变量。避免 MLLMBackend(api_key="")
    # 被环境变量覆盖，导致「显式无 API」状态失效。
    if api_key is not None:
        resolved_key = api_key
    else:
        resolved_key = _first_env(preset["api_key_envs"])
    resolved_base = base_url or os.environ.get(preset["base_url_env"]) or preset["default_base_url"]
    resolved_model = model or os.environ.get("MLLM_MODEL") or preset["default_model"]
    if provider_id == "kimi-code":
        key = resolved_model.strip().lower()
        resolved_model = KIMI_CODE_MODEL_ALIASES.get(key, resolved_model)

    return {
        "provider": provider_id,
        "api_key": resolved_key,
        "base_url": resolved_base,
        "model": resolved_model,
        "label": preset["label"],
    }


def mllm_config_status() -> str:
    """人类可读的当前 MLLM 配置（不打印完整 key）。"""
    cfg = resolve_mllm_config()
    key = cfg["api_key"]
    key_hint = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else ("已设置" if key else "未设置")
    return (
        f"MLLM_PROVIDER={cfg['provider']} ({cfg['label']})\n"
        f"  API Key: {key_hint}\n"
        f"  Base URL: {cfg['base_url']}\n"
        f"  Model: {cfg['model']}"
    )
