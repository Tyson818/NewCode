from types import SimpleNamespace

import pytest

from newcode.config import AppConfig
from newcode.providers.base import ProviderError
from newcode.providers.deepseek import DeepSeekProvider
from newcode.session import ChatMessage


class FakeCompletions:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def chunk(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content))]
    )


def test_provider_sends_expected_request_parameters():
    completions = FakeCompletions(result=[chunk("你好")])
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat", base_url="https://example.test", api_key_env="KEY"),
        api_key="secret",
        client=FakeClient(completions),
    )

    list(
        provider.stream_chat(
            [
                ChatMessage(role="user", content="第一句"),
                ChatMessage(role="assistant", content="收到"),
            ]
        )
    )

    assert completions.calls == [
        {
            "model": "deepseek-chat",
            "messages": [
                {"role": "user", "content": "第一句"},
                {"role": "assistant", "content": "收到"},
            ],
            "stream": True,
        }
    ]


def test_provider_yields_non_empty_stream_deltas():
    completions = FakeCompletions(result=[chunk("你"), chunk(""), chunk(None), chunk("好")])
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    assert list(provider.stream_chat([ChatMessage(role="user", content="hi")])) == ["你", "好"]


def test_provider_converts_sdk_errors_to_provider_error():
    completions = FakeCompletions(error=RuntimeError("secret low level failure"))
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    with pytest.raises(ProviderError) as exc_info:
        list(provider.stream_chat([ChatMessage(role="user", content="hi")]))

    assert "模型服务请求失败" in exc_info.value.message
    assert "secret" not in exc_info.value.message
