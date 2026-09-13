from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pytest

from newcode import cli
from newcode.config import AppConfig, ConfigError, load_config
from newcode.permissions.confirmer import CliPermissionConfirmer
from newcode.permissions.manager import PermissionManager
from newcode.permissions.modes import decision_for_risk
from newcode.permissions.types import (
    ConfirmationResult,
    ConfirmationScope,
    PermissionDecision,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMode,
    PermissionRequest,
    RiskLevel,
)
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import JsonObject, ToolCall, ToolContext, ToolResult, ToolSpec


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


class FakeProvider:
    def __init__(self, event_batches):
        self.event_batches = event_batches
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "allow_tool_calls": allow_tool_calls,
            }
        )
        yield from self.event_batches[min(len(self.calls) - 1, len(self.event_batches) - 1)]


class FakeConfirmer:
    def __init__(self, result: ConfirmationResult):
        self.result = result
        self.calls = 0

    def confirm(self, request, decision):
        self.calls += 1
        return self.result


@dataclass
class RecordingTool:
    name: str
    calls: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=f"{self.name} fake tool",
            parameters={"type": "object", "properties": {}},
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult.success(self.name, arguments)


def write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def registry_with(*tools: RecordingTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def tool_call(name: str, call_id: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )


def confirmation_request() -> PermissionRequest:
    return PermissionRequest(
        tool_name="write_file",
        original_args={"path": "notes.txt"},
        normalized_args={"relative_path": "notes.txt"},
        workspace_root=Path.cwd(),
        mode=PermissionMode.DEFAULT,
    )


def confirmation_decision() -> PermissionDecision:
    return PermissionDecision(
        decision=PermissionDecisionValue.REQUIRE_CONFIRMATION,
        tool_name="write_file",
        reason="Permission mode default default for medium risk.",
        risk_level=RiskLevel.MEDIUM,
        matched_rule=None,
        layer=PermissionLayer.PERMISSION_MODE,
    )


def tool_messages(session: ChatSession):
    return [message for message in session.messages if message.role == "tool"]


def tool_payload(session: ChatSession):
    return json.loads(tool_messages(session)[0].content or "{}")


def run_cli(provider, tmp_path, inputs, **kwargs):
    output = StringIO()
    error = StringIO()
    recorder = PromptRecorder(inputs)
    session = kwargs.pop("session", ChatSession())
    code = cli.run_conversation(
        provider,
        session,
        registry=kwargs.pop("registry", ToolRegistry()),
        tool_context=ToolContext(workspace_root=tmp_path),
        input_func=recorder,
        output=output,
        error_output=error,
        **kwargs,
    )
    return code, output.getvalue(), error.getvalue(), session, recorder


def test_permission_mode_defaults_to_default(tmp_path):
    config = load_config(write_config(tmp_path / "config.yaml", "model: deepseek-chat\n"))

    assert config.permission_mode is PermissionMode.DEFAULT


@pytest.mark.parametrize(
    "mode",
    ["strict", "default", "permissive", "trusted"],
)
def test_permission_modes_are_valid_config_values(tmp_path, mode):
    config = load_config(
        write_config(
            tmp_path / "config.yaml",
            f"model: deepseek-chat\npermission_mode: {mode}\n",
        )
    )

    assert config.permission_mode is PermissionMode(mode)


def test_invalid_permission_mode_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            write_config(
                tmp_path / "config.yaml",
                "model: deepseek-chat\npermission_mode: danger\n",
            )
        )


def test_trusted_config_uses_permissive_semantics(tmp_path):
    config = load_config(
        write_config(
            tmp_path / "config.yaml",
            "model: deepseek-chat\npermission_mode: trusted\n",
        )
    )
    trusted = decision_for_risk(
        config.permission_mode,
        RiskLevel.MEDIUM,
        confirmation_request(),
    )
    permissive = decision_for_risk(
        PermissionMode.PERMISSIVE,
        RiskLevel.MEDIUM,
        confirmation_request(),
    )

    assert trusted.decision is permissive.decision


def test_cli_confirmer_can_deny():
    output = StringIO()
    recorder = PromptRecorder(["n"])
    confirmer = CliPermissionConfirmer(
        input_func=recorder,
        output=output,
    )

    result = confirmer.confirm(confirmation_request(), confirmation_decision())

    assert result.allowed is False
    assert result.scope is ConfirmationScope.ONCE
    assert "[权限] 工具请求需要确认" in output.getvalue()
    assert "工具: write_file" in output.getvalue()
    assert "原因: default 权限模式下，中风险操作需要确认" in output.getvalue()
    assert "风险: medium" in output.getvalue()
    assert "层级: permission_mode" in output.getvalue()
    assert "参数: relative_path=notes.txt" in output.getvalue()
    assert recorder.prompts == ["是否允许？[n] 拒绝 / [o] 仅本次允许 / [s] 本会话允许: "]


def test_cli_confirmer_can_allow_once():
    recorder = PromptRecorder(["o"])
    confirmer = CliPermissionConfirmer(
        input_func=recorder,
        output=StringIO(),
    )

    result = confirmer.confirm(confirmation_request(), confirmation_decision())

    assert result.allowed is True
    assert result.scope is ConfirmationScope.ONCE
    assert recorder.prompts == ["是否允许？[n] 拒绝 / [o] 仅本次允许 / [s] 本会话允许: "]


def test_cli_confirmer_can_allow_session():
    recorder = PromptRecorder(["s"])
    confirmer = CliPermissionConfirmer(
        input_func=recorder,
        output=StringIO(),
    )

    result = confirmer.confirm(confirmation_request(), confirmation_decision())

    assert result.allowed is True
    assert result.scope is ConfirmationScope.SESSION
    assert recorder.prompts == ["是否允许？[n] 拒绝 / [o] 仅本次允许 / [s] 本会话允许: "]


def test_cli_confirmer_translates_strict_mode_reason():
    request = PermissionRequest(
        tool_name="write_file",
        original_args={"path": "notes.txt"},
        normalized_args={"relative_path": "notes.txt"},
        workspace_root=Path.cwd(),
        mode=PermissionMode.STRICT,
    )
    output = StringIO()
    confirmer = CliPermissionConfirmer(
        input_func=PromptRecorder(["n"]),
        output=output,
    )

    confirmer.confirm(request, confirmation_decision())

    assert "原因: strict 权限模式下，该操作需要确认" in output.getvalue()


def test_cli_confirmer_translates_trusted_mode_reason():
    request = PermissionRequest(
        tool_name="write_file",
        original_args={"path": "notes.txt"},
        normalized_args={"relative_path": "notes.txt"},
        workspace_root=Path.cwd(),
        mode=PermissionMode.TRUSTED,
    )
    decision = PermissionDecision(
        decision=PermissionDecisionValue.REQUIRE_CONFIRMATION,
        tool_name="write_file",
        reason="Permission mode trusted default for medium risk.",
        risk_level=RiskLevel.MEDIUM,
        matched_rule=None,
        layer=PermissionLayer.PERMISSION_MODE,
    )
    output = StringIO()
    confirmer = CliPermissionConfirmer(
        input_func=PromptRecorder(["n"]),
        output=output,
    )

    confirmer.confirm(request, decision)

    assert "原因: permissive/trusted 权限模式下允许低中风险操作" in output.getvalue()


def test_cli_confirmer_non_interactive_denies_without_input():
    recorder = PromptRecorder([])
    confirmer = CliPermissionConfirmer(
        input_func=recorder,
        output=StringIO(),
        interactive=False,
    )

    result = confirmer.confirm(confirmation_request(), confirmation_decision())

    assert result.allowed is False
    assert recorder.prompts == []


def test_run_conversation_accepts_fake_confirmer(tmp_path):
    tool = RecordingTool("write_file")
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("write_file", "call_1", {"path": "notes.txt", "content": "x"})])],
            [TextDelta("done")],
        ]
    )
    fake_confirmer = FakeConfirmer(
        ConfirmationResult(True, ConfirmationScope.ONCE, "allow once")
    )

    code, output, error, _, _ = run_cli(
        provider,
        tmp_path,
        ["write", "/exit"],
        registry=registry_with(tool),
        permission_confirmer=fake_confirmer,
    )

    assert code == 0
    assert error == ""
    assert "done" in output
    assert tool.calls == 1
    assert fake_confirmer.calls == 1


def test_run_conversation_accepts_permission_manager_injection(tmp_path):
    tool = RecordingTool("write_file")
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("write_file", "call_1", {"path": "notes.txt", "content": "x"})])],
            [TextDelta("done")],
        ]
    )

    code, _, error, _, _ = run_cli(
        provider,
        tmp_path,
        ["write", "/exit"],
        registry=registry_with(tool),
        permission_manager=PermissionManager(mode=PermissionMode.TRUSTED),
    )

    assert code == 0
    assert error == ""
    assert tool.calls == 1


def test_no_confirmer_default_deny_does_not_block_when_manager_injected(tmp_path):
    tool = RecordingTool("write_file")
    session = ChatSession()
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("write_file", "call_1", {"path": "notes.txt", "content": "x"})])],
            [TextDelta("denied")],
        ]
    )

    code, output, error, session, recorder = run_cli(
        provider,
        tmp_path,
        ["write", "/exit"],
        session=session,
        registry=registry_with(tool),
        permission_manager=PermissionManager(mode=PermissionMode.DEFAULT),
    )

    assert code == 0
    assert error == ""
    assert "denied" in output
    assert tool.calls == 0
    assert len(recorder.prompts) == 2
    assert tool_payload(session)["error"]["code"] == "permission_denied"


def test_cli_plan_do_exit_do_not_regress(tmp_path):
    provider = FakeProvider([[TextDelta("plan")], [TextDelta("do")]])

    code, output, error, _, _ = run_cli(
        provider,
        tmp_path,
        ["/plan", "plan task", "/do", "do task", "/exit"],
        permission_manager=PermissionManager(mode=PermissionMode.TRUSTED),
    )

    assert code == 0
    assert error == ""
    assert "Plan Mode" in output
    assert "Do Mode" in output
    assert len(provider.calls) == 2


def test_main_passes_permission_mode_and_rules_to_conversation(tmp_path, monkeypatch):
    config_path = write_config(
        tmp_path / "config.yaml",
        "\n".join(
            [
                "model: deepseek-chat",
                "api_key_env: TEST_KEY",
                f"workspace_root: {tmp_path.as_posix()}",
                "permission_mode: trusted",
            ]
        ),
    )
    monkeypatch.setenv("TEST_KEY", "secret-value")
    captured = {}

    class ProviderForMain(FakeProvider):
        def __init__(self, config: AppConfig, api_key: str) -> None:
            super().__init__([[TextDelta("ok")]])
            captured["config"] = config
            captured["api_key"] = api_key

    def fake_run_conversation(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "DeepSeekProvider", ProviderForMain)
    monkeypatch.setattr(cli, "run_conversation", fake_run_conversation)

    assert cli.main(["--config", str(config_path)]) == 0
    assert captured["config"].permission_mode is PermissionMode.TRUSTED
    assert captured["permission_mode"] is PermissionMode.TRUSTED
    assert captured["permission_rules"] is not None


def test_no_permanent_permission_write_is_exposed():
    assert "permanent" not in {scope.value for scope in ConfirmationScope}
