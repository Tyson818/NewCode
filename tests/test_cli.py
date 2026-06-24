from io import StringIO

from newcode import cli
from newcode.providers.base import ProviderError, TextDelta
from newcode.session import ChatSession


class FakeProvider:
    def __init__(self, events=None, error=None):
        self.events = events or [[TextDelta("好的")]]
        self.error = error
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "allow_tool_calls": allow_tool_calls,
            }
        )
        if self.error:
            raise self.error
        index = len(self.calls) - 1
        yield from self.events[min(index, len(self.events) - 1)]


class PromptRecorder:
    def __init__(self, values):
        self.values = list(values)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.values:
            raise EOFError
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def run_with_inputs(provider, values, session=None):
    output = StringIO()
    error = StringIO()
    recorder = PromptRecorder(values)
    code = cli.run_conversation(
        provider,
        session or ChatSession(),
        input_func=recorder,
        output=output,
        error_output=error,
    )
    return code, output.getvalue(), error.getvalue(), recorder


def non_system_messages(messages):
    return [message for message in messages if message.role != "system"]


def test_exit_commands_end_session():
    for command in ("/exit", "/quit", "exit"):
        provider = FakeProvider()
        code, output, error, _ = run_with_inputs(provider, [command])

        assert code == 0
        assert "已结束对话" in output
        assert error == ""
        assert provider.calls == []


def test_empty_input_does_not_call_provider():
    provider = FakeProvider()

    code, output, _, recorder = run_with_inputs(provider, [" ", "", "/exit"])

    assert code == 0
    assert provider.calls == []
    assert recorder.prompts == ["你> ", "你> ", "你> "]
    assert "NewCode 已启动" in output


def test_eof_exits_cleanly():
    provider = FakeProvider()

    code, output, error, _ = run_with_inputs(provider, [EOFError()])

    assert code == 0
    assert "已结束对话" in output
    assert error == ""


def test_keyboard_interrupt_exits_cleanly():
    provider = FakeProvider()

    code, output, error, _ = run_with_inputs(provider, [KeyboardInterrupt()])

    assert code == 0
    assert "已中断对话" in output
    assert error == ""


def test_streaming_output_and_history_are_preserved():
    provider = FakeProvider(events=[[TextDelta("你"), TextDelta("好")]])
    session = ChatSession()

    code, output, error, _ = run_with_inputs(provider, ["你好", "/exit"], session)

    assert code == 0
    assert "NewCode> 你好" in output
    assert error == ""
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert [message.content for message in session.messages] == ["你好", "你好"]


def test_second_turn_receives_previous_history():
    provider = FakeProvider(events=[[TextDelta("收到")]])

    run_with_inputs(provider, ["第一轮", "第二轮", "/exit"])

    assert len(provider.calls) == 2
    assert [(message.role, message.content) for message in non_system_messages(provider.calls[1]["messages"])] == [
        ("user", "第一轮"),
        ("assistant", "收到"),
        ("user", "第二轮"),
    ]


def test_provider_error_recovers_without_assistant_history():
    provider = FakeProvider(error=ProviderError("服务暂时不可用"))
    session = ChatSession()

    code, output, error, _ = run_with_inputs(provider, ["你好", "/exit"], session)

    assert code == 0
    assert "模型错误：服务暂时不可用" in error
    assert [(message.role, message.content) for message in session.messages] == [
        ("user", "你好")
    ]


def test_main_returns_nonzero_for_config_error(tmp_path, capsys):
    code = cli.main(["--config", str(tmp_path / "missing.yaml")])

    captured = capsys.readouterr()
    assert code == 1
    assert "配置错误" in captured.err


def test_main_does_not_create_provider_when_api_key_missing(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: deepseek-chat\napi_key_env: MISSING_KEY\n", encoding="utf-8")
    monkeypatch.delenv("MISSING_KEY", raising=False)

    def fail_provider(*args, **kwargs):
        raise AssertionError("不应该创建 Provider")

    monkeypatch.setattr(cli, "DeepSeekProvider", fail_provider)

    code = cli.main(["--config", str(config_path)])

    captured = capsys.readouterr()
    assert code == 1
    assert "MISSING_KEY" in captured.err


def test_main_starts_conversation_with_valid_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: deepseek-chat\napi_key_env: TEST_KEY\n", encoding="utf-8")
    monkeypatch.setenv("TEST_KEY", "secret-value")

    created = {}

    class ProviderForMain(FakeProvider):
        def __init__(self, config, api_key):
            super().__init__()
            created["config"] = config
            created["api_key"] = api_key

    monkeypatch.setattr(cli, "DeepSeekProvider", ProviderForMain)
    monkeypatch.setattr(cli, "run_conversation", lambda **kwargs: 0)

    assert cli.main(["--config", str(config_path)]) == 0
    assert created["config"].model == "deepseek-chat"
    assert created["api_key"] == "secret-value"


def test_out_of_scope_request_is_plain_provider_message():
    provider = FakeProvider(events=[[TextDelta("我不能执行未请求的额外循环")]])

    run_with_inputs(provider, ["请执行 git status 并修改文件", "/exit"])

    messages = non_system_messages(provider.calls[0]["messages"])
    assert messages[0].role == "user"
    assert messages[0].content == "请执行 git status 并修改文件"
