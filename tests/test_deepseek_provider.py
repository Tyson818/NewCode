from __future__ import annotations

from types import SimpleNamespace
import inspect

import pytest

from newcode.config import AppConfig
from newcode.providers import deepseek
from newcode.providers.base import ProviderError, TextDelta
from newcode.providers.deepseek import DeepSeekProvider, message_to_provider_dict
from newcode.session import ChatMessage
from newcode.tools.types import ToolCall


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


def chunk(content=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None))]
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
            ],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
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
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
            "tool_choice": "auto",
        }
    ]


def test_provider_omits_tools_when_tool_calls_are_not_allowed():
    completions = FakeCompletions(result=[chunk("最终回答")])
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    list(provider.stream_chat([], tools=[{"type": "function"}], allow_tool_calls=False))

    assert "tools" not in completions.calls[0]
    assert "tool_choice" not in completions.calls[0]


def test_provider_yields_text_deltas():
    completions = FakeCompletions(result=[chunk("你"), chunk("好")])
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    assert list(provider.stream_chat([ChatMessage(role="user", content="hi")])) == [
        TextDelta("你"),
        TextDelta("好"),
    ]


def test_provider_converts_sdk_errors_to_provider_error():
    completions = FakeCompletions(error=RuntimeError("secret low level failure"))
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    with pytest.raises(ProviderError) as exc_info:
        list(provider.stream_chat([ChatMessage(role="user", content="hi")]))

    assert "secret" not in exc_info.value.message


def test_provider_serializes_system_role_without_prompt_strategy():
    message = ChatMessage(role="system", content="stable prompt")

    assert message_to_provider_dict(message) == {
        "role": "system",
        "content": "stable prompt",
    }


def test_provider_still_serializes_tool_messages():
    message = ChatMessage(role="tool", content="{}", tool_call_id="call_1")

    assert message_to_provider_dict(message) == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "{}",
    }


def test_provider_still_serializes_assistant_tool_calls():
    message = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCall(
                id="call_1",
                name="read_file",
                arguments={"path": "README.md"},
                raw_arguments='{"path": "README.md"}',
            )
        ],
    )

    assert message_to_provider_dict(message) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "README.md"}',
                },
            }
        ],
    }


def test_provider_does_not_depend_on_prompt_builder():
    source = inspect.getsource(deepseek)

    assert "newcode.prompt" not in source
    assert "PromptBuilder" not in source
    assert "system-reminder" not in source
