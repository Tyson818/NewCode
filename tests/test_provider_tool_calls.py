from types import SimpleNamespace

from newcode.config import AppConfig
from newcode.providers.base import ToolCallEvent
from newcode.providers.deepseek import DeepSeekProvider


class FakeCompletions:
    def __init__(self, result):
        self.result = result

    def create(self, **kwargs):
        return self.result


class FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def tool_chunk(index=0, call_id="", name="", arguments=""):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=index,
                            id=call_id,
                            function=SimpleNamespace(name=name, arguments=arguments),
                        )
                    ],
                )
            )
        ]
    )


def test_streaming_tool_call_arguments_are_joined():
    completions = FakeCompletions(
        [
            tool_chunk(call_id="call_1", name="read_file", arguments='{"path"'),
            tool_chunk(arguments=': "pyproject.toml"}'),
        ]
    )
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    events = list(provider.stream_chat([]))

    assert events == [
        ToolCallEvent(
            tool_calls=[
                events[0].tool_calls[0],
            ]
        )
    ]
    tool_call = events[0].tool_calls[0]
    assert tool_call.id == "call_1"
    assert tool_call.name == "read_file"
    assert tool_call.arguments == {"path": "pyproject.toml"}
    assert tool_call.raw_arguments == '{"path": "pyproject.toml"}'


def test_invalid_json_arguments_become_parse_error_tool_call():
    completions = FakeCompletions(
        [tool_chunk(call_id="call_1", name="read_file", arguments="{bad")]
    )
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    event = list(provider.stream_chat([]))[0]

    assert event.tool_calls[0].name == "__tool_call_parse_error__"
    assert event.tool_calls[0].raw_arguments == "{bad"
