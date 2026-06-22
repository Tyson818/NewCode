from types import SimpleNamespace

from newcode.config import AppConfig
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.providers.deepseek import (
    DISALLOWED_TOOL_CALL_TEXT,
    DSML_TOOL_CALLS_END,
    DSML_TOOL_CALLS_START,
    DeepSeekProvider,
)


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


def content_chunk(content):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=None)
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


def test_dsml_tool_call_in_content_is_parsed_without_text_delta():
    completions = FakeCompletions(
        [
            content_chunk("<｜｜DSML｜｜tool_calls>\n"),
            content_chunk('<｜｜DSML｜｜invoke name="run_command">\n'),
            content_chunk(
                '<｜｜DSML｜｜parameter name="command" string="true">dir</｜｜DSML｜｜parameter>\n'
            ),
            content_chunk(
                '<｜｜DSML｜｜parameter name="timeout_seconds" string="false">10</｜｜DSML｜｜parameter>\n'
            ),
            content_chunk("</｜｜DSML｜｜invoke>\n</｜｜DSML｜｜tool_calls>"),
        ]
    )
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    events = list(provider.stream_chat([]))

    assert len(events) == 1
    assert isinstance(events[0], ToolCallEvent)
    tool_call = events[0].tool_calls[0]
    assert tool_call.name == "run_command"
    assert tool_call.arguments == {"command": "dir", "timeout_seconds": 10}
    assert tool_call.raw_arguments == '{"command": "dir", "timeout_seconds": 10}'


def test_dsml_start_marker_split_across_chunks_is_buffered():
    completions = FakeCompletions(
        [
            content_chunk(DSML_TOOL_CALLS_START[:5]),
            content_chunk(DSML_TOOL_CALLS_START[5:13]),
            content_chunk(DSML_TOOL_CALLS_START[13:]),
            content_chunk(
                '<｜｜DSML｜｜invoke name="run_command">'
                '<｜｜DSML｜｜parameter name="command" string="true">dir</｜｜DSML｜｜parameter>'
                "</｜｜DSML｜｜invoke>"
                f"{DSML_TOOL_CALLS_END}"
            ),
        ]
    )
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    events = list(provider.stream_chat([]))

    assert len(events) == 1
    assert isinstance(events[0], ToolCallEvent)
    tool_call = events[0].tool_calls[0]
    assert tool_call.name == "run_command"
    assert tool_call.arguments == {"command": "dir"}


def test_dsml_content_is_text_warning_when_tool_calls_are_not_allowed():
    dsml = (
        f"{DSML_TOOL_CALLS_START}"
        '<｜｜DSML｜｜invoke name="run_command">'
        '<｜｜DSML｜｜parameter name="command" string="true">dir</｜｜DSML｜｜parameter>'
        "</｜｜DSML｜｜invoke>"
        f"{DSML_TOOL_CALLS_END}"
    )
    completions = FakeCompletions(
        [
            content_chunk(dsml[:8]),
            content_chunk(dsml[8:]),
        ]
    )
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    events = list(provider.stream_chat([], allow_tool_calls=False))

    assert events == [TextDelta(DISALLOWED_TOOL_CALL_TEXT)]
    assert not any(isinstance(event, ToolCallEvent) for event in events)
    assert "DSML" not in "".join(event.text for event in events if isinstance(event, TextDelta))


def test_dsml_tool_call_can_have_text_before_and_after():
    completions = FakeCompletions(
        [
            content_chunk("before "),
            content_chunk(
                '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="find_files">'
                '<｜｜DSML｜｜parameter name="pattern" string="true">*.py</｜｜DSML｜｜parameter>'
                "</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls> after"
            ),
        ]
    )
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    events = list(provider.stream_chat([]))

    assert events[0].text == "before "
    assert isinstance(events[1], ToolCallEvent)
    assert events[1].tool_calls[0].name == "find_files"
    assert events[1].tool_calls[0].arguments == {"pattern": "*.py"}
    assert events[2].text == " after"
