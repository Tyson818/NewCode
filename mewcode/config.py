from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class AppConfig:
    model: str
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")

    try:
        with path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ConfigError("配置文件不是有效的 YAML") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{path}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 YAML 对象")

    model = raw.get("model")
    base_url = raw.get("base_url", DEFAULT_BASE_URL)
    api_key_env = raw.get("api_key_env", DEFAULT_API_KEY_ENV)

    return AppConfig(
        model=_require_non_empty_string(model, "model"),
        base_url=_require_non_empty_string(base_url, "base_url"),
        api_key_env=_require_non_empty_string(api_key_env, "api_key_env"),
    )


def resolve_api_key(env_name: str) -> str:
    checked_env_name = _require_non_empty_string(env_name, "api_key_env")
    api_key = os.environ.get(checked_env_name)
    if api_key is None or not api_key.strip():
        raise ConfigError(f"缺少 API Key 环境变量：{checked_env_name}")
    return api_key


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"配置项 {field_name} 必须是非空字符串")
    return value.strip()
