from io import StringIO

import pytest

from mewcode import cli
from mewcode.providers.base import ProviderError
from mewcode.session import ChatSession


class FakeProvider:
    def __init__(self, chunks=None, error=None):
        self.chunks = chunks or ["好的"]
        self.error = error
        self.calls = []

    def stream_chat(self, messages):
        self.calls.append(list(messages))
        if self.error:
            raise self.error
        yield from self.chunks


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
    assert "MewCode 已启动" in output


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
    provider = FakeProvider(chunks=["你", "好"])
    session = ChatSession()

    code, output, error, _ = run_with_inputs(provider, ["你好", "/exit"], session)

    assert code == 0
    assert "MewCode> 你好" in output
    assert error == ""
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert [message.content for message in session.messages] == ["你好", "你好"]


def test_second_turn_receives_previous_history():
    provider = FakeProvider(chunks=["收到"])

    run_with_inputs(provider, ["第一轮", "第二轮", "/exit"])

    assert len(provider.calls) == 2
    assert [(message.role, message.content) for message in provider.calls[1]] == [
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
    assert "你> " not in output
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
        raise AssertionError("不应创建 Provider")

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
    monkeypatch.setattr(cli, "run_conversation", lambda provider, session: 0)

    assert cli.main(["--config", str(config_path)]) == 0
    assert created["config"].model == "deepseek-chat"
    assert created["api_key"] == "secret-value"


def test_out_of_scope_request_is_plain_provider_message():
    provider = FakeProvider(chunks=["我不能执行本地操作"])

    run_with_inputs(provider, ["请执行 git status 并修改文件", "/exit"])

    assert provider.calls[0][0].role == "user"
    assert provider.calls[0][0].content == "请执行 git status 并修改文件"
