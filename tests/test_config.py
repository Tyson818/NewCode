from pathlib import Path

import pytest

from mewcode.config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    AppConfig,
    ConfigError,
    load_config,
    resolve_api_key,
)


def write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_reads_valid_yaml(tmp_path):
    config_path = write_config(
        tmp_path / "config.yaml",
        "model: deepseek-chat\nbase_url: https://api.deepseek.com\napi_key_env: DEEPSEEK_API_KEY\n",
    )

    assert load_config(config_path) == AppConfig(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
    )


def test_load_config_applies_defaults(tmp_path):
    config_path = write_config(tmp_path / "config.yaml", "model: deepseek-chat\n")

    config = load_config(config_path)

    assert config.base_url == DEFAULT_BASE_URL
    assert config.api_key_env == DEFAULT_API_KEY_ENV


def test_load_config_rejects_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "model: ''\n",
        "model: 123\n",
        "model: deepseek-chat\nbase_url: ''\n",
        "model: deepseek-chat\napi_key_env: []\n",
        "- model\n- deepseek-chat\n",
    ],
)
def test_load_config_rejects_invalid_values(tmp_path, text):
    config_path = write_config(tmp_path / "config.yaml", text)

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_resolve_api_key_reads_configured_env(monkeypatch):
    monkeypatch.setenv("CUSTOM_KEY", "secret-value")

    assert resolve_api_key("CUSTOM_KEY") == "secret-value"


def test_resolve_api_key_rejects_missing_env(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)

    with pytest.raises(ConfigError, match="MISSING_KEY"):
        resolve_api_key("MISSING_KEY")


def test_resolve_api_key_rejects_empty_env(monkeypatch):
    monkeypatch.setenv("EMPTY_KEY", " ")

    with pytest.raises(ConfigError, match="EMPTY_KEY"):
        resolve_api_key("EMPTY_KEY")
