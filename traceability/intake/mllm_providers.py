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
from pathlib import Path
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

# 项目根目录 .env 自动加载（懒加载一次）。避免每次运行都要手动 source zshrc。
# 只填充「尚未在环境变量里」的键，绝不覆盖调用方显式设置的变量。
_ENV_LOADED = False


def _load_dotenv_if_present() -> None:
    """从项目根目录 .env 补齐缺失的环境变量（幂等，只加载一次）。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    # 从当前文件向上找项目根（含 .git 或 .env 的目录）
    candidate = Path(__file__).resolve()
    for _ in range(6):
        if (candidate / ".env").exists():
            _load_dotenv_file(candidate / ".env")
            return
        candidate = candidate.parent
    # 回退：当前工作目录
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        _load_dotenv_file(cwd_env)


def _load_dotenv_file(path: Path) -> None:
    """解析 KEY=VALUE 行，仅当环境变量未设置时导入（支持单/双引号与注释）。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # 剥离首尾成对引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        # 仅当环境变量尚未设置时导入，避免覆盖显式配置
        if key not in os.environ:
            os.environ[key] = val


def _first_env(names: List[str]) -> str:
    _load_dotenv_if_present()
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
    "agent-vision": {
        "api_key_envs": [],
        "base_url_env": "",
        "default_base_url": None,
        "default_model": "gemini-3.7-flash",
        "label": "Agent 本地多模态视觉（Gemini 3.7 Flash 提取缓存）",
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
    "workbuddy": {
        "api_key_envs": ["WORKBUDDY_API_KEY", "OPENAI_API_KEY"],
        "base_url_env": "WORKBUDDY_BASE_URL",
        "default_base_url": "http://127.0.0.1:10101/v1",
        "default_model": "deepseek-v4-pro",
        "label": "WorkBuddy 本地代理（deepseek-v4-pro 免费稳定无鉴权）",
    },
    "glm-relay": {
        # 本地 opencodex relay（OAuth 鉴权在 relay 侧，客户端无需真实 key）。
        # OpenAI SDK 要求 Authorization 非空，故占位 key。
        "api_key_envs": ["GLM_RELAY_API_KEY"],
        "base_url_env": "GLM_RELAY_BASE_URL",
        "default_base_url": "http://127.0.0.1:10100/v1",
        "default_model": "workbuddy/glm-5.3-flash",
        "label": "GLM 5.3 Flash（本地 relay，对话同款模型）",
    },
    "antigravity-ocx": {
        # Google Antigravity / Cloud Code Assist 经本地 opencodex relay（DSH 同款）。
        # relay 已用 OAuth 鉴权，本地无需 client key；但 OpenAI SDK 要求 Authorization
        # header 非空，因此默认配置占位 key。
        "api_key_envs": ["OPENCODEX_API_KEY"],
        "base_url_env": "OPENCODEX_BASE_URL",
        "default_base_url": "http://127.0.0.1:10100/v1",
        "default_model": "google-antigravity/gemini-3.7-flash",
        "label": "Google Antigravity Gemini 3.7 Flash（opencodex relay，OAuth 免 key）",
    },
}


def resolve_mllm_config(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """合并环境变量与显式参数，返回 MLLMBackend 用的 api_key/base_url/model。"""
    _load_dotenv_if_present()
    provider_id = (provider or os.environ.get("MLLM_PROVIDER", "openai")).strip().lower()
    preset = MLLM_PROVIDER_PRESETS.get(provider_id, MLLM_PROVIDER_PRESETS["openai"])

    # api_key：显式传参（含空字符串，用于表达「禁用 MLLM」）优先；
    # 仅当参数为 None 时才回退读环境变量。避免 MLLMBackend(api_key="")
    # 被环境变量覆盖，导致「显式无 API」状态失效。
    if api_key is not None:
        resolved_key = api_key
    else:
        resolved_key = _first_env(preset["api_key_envs"])
        if not resolved_key and provider_id in ("antigravity-ocx", "workbuddy", "glm-relay"):
            resolved_key = "workbuddy-key"
    resolved_base = base_url or os.environ.get(preset["base_url_env"]) or preset["default_base_url"]
    # 模型解析：显式传参 > 全局 MLLM_MODEL > preset 默认。
    # 但 agent-vision / antigravity-ocx 是「免 key 本地/中继」专用 provider，
    # 全局 MLLM_MODEL（通常写给 Kimi 的 k3-256k）不应污染它们的模型命名空间；
    # 这两个 provider 只在显式传 model 时才覆盖 preset 默认值。
    if provider_id in ("agent-vision", "antigravity-ocx", "workbuddy", "glm-relay"):
        resolved_model = model or preset["default_model"]
    else:
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
