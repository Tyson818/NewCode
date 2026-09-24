"""Hook 与既有 AgentLoop 安全链和请求级背景的组合测试。"""

from pathlib import Path
from threading import Event

import pytest

from newcode.agent import AgentFinalAnswer, AgentLoop, AgentStopped, AgentToolError
from newcode.agent.mode import AgentMode
from newcode.hooks import (
    HookAction, HookActionType, HookContext, HookEngine, HookEvent,
    HookNetworkPolicy, HookRule, HookSource, parse_condition,
)
from newcode.hooks.actions import HookActionRunner, HookGatewayResult, HookHttpResponse
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import PermissionMode
from newcode.providers.base import ProviderError, TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolContext, ToolResult, ToolSpec


class Provider:
    def __init__(self, batches, *, on_call=None):
        self.batches = batches
        self.calls = []
        self.on_call = on_call

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), list(tools or []), allow_tool_calls))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        yield from self.batches[len(self.calls) - 1]


class FakeTool:
    def __init__(self, name="read_file"):
        self.name = name
        self.calls = []

    @property
    def spec(self):
        return ToolSpec(self.name, "fixture", {"type": "object"})

    def run(self, arguments, context):
        self.calls.append(dict(arguments))
        return ToolResult.success(self.name, {"content": "secret-value tool output"})


class TracingEngine(HookEngine):
    def __init__(self, rules=(), **kwargs):
        super().__init__(rules, **kwargs)
        self.trace = []

    def emit(self, event, context, *, hook_origin=False):
        self.trace.append((event, context, hook_origin))
        return super().emit(event, context, hook_origin=hook_origin)


def _rule(name, event, kind=HookActionType.PROMPT_INJECTION, args=None, *, once=False, condition=None, deny=False):
    return HookRule(
        name, event, HookAction(kind, args if args is not None else {"text": name}),
        HookSource.USER, condition=condition, once=once, deny=deny,
    )


def _loop(tmp_path: Path, provider, *, rules=(), runner=None, registry=None, permission=None, session=None):
    registry = registry or ToolRegistry()
    runner = runner or HookActionRunner()
    engine = TracingEngine(tuple(rules), action_sink=runner.submit, on_session_reset=runner.reset_session)
    loop = AgentLoop(
        provider=provider, session=session or ChatSession(session_id="test-session"), registry=registry,
        tool_context=ToolContext(tmp_path, sensitive_values=("secret-value",)),
        permission_manager=permission or PermissionManager(mode=PermissionMode.TRUSTED),
        hook_engine=engine, hook_actions=runner,
    )
    return loop, engine, runner


def _registry(tool):
    registry = ToolRegistry()
    registry.register(tool, read_only=tool.name == "read_file", do_visible=True)
    return registry


def _bodies(messages):
    return [message.content or "" for message in messages]


def test_event_order_user_summary_and_safe_tool_context(tmp_path):
    tool = FakeTool()
    provider = Provider([[ToolCallEvent([ToolCall("a", "read_file", {"path": "README.md"})])], [TextDelta("done")]])
    loop, engine, runner = _loop(tmp_path, provider, registry=_registry(tool))
    original = "API_KEY=secret-value " + "x" * 600
    events = list(loop.run(original))
    assert isinstance(events[-1], AgentFinalAnswer)
    assert [event for event, _, _ in engine.trace] == [
        HookEvent.TURN_START, HookEvent.USER_MESSAGE_RECEIVED,
        HookEvent.BEFORE_MODEL_REQUEST, HookEvent.AFTER_MODEL_RESPONSE,
        HookEvent.BEFORE_TOOL, HookEvent.AFTER_TOOL,
        HookEvent.BEFORE_MODEL_REQUEST, HookEvent.AFTER_MODEL_RESPONSE,
        HookEvent.TURN_END,
    ]
    message_context = engine.trace[1][1]
    assert len(message_context.fields["message.summary"]) == 512
    assert "secret-value" not in str(message_context.fields)
    assert "message.full" not in message_context.fields
    assert loop.session.messages[0].content == original
    for event, context, _ in engine.trace:
        assert "secret-value tool output" not in str(context.fields)
    assert engine.trace[5][1].fields["tool.result.ok"] is True
    runner.shutdown()


def test_sync_before_model_injection_is_current_request_only(tmp_path):
    tool = FakeTool()
    provider = Provider([[ToolCallEvent([ToolCall("a", "read_file", {})])], [TextDelta("done")]])
    rule = _rule("before_prompt", HookEvent.BEFORE_MODEL_REQUEST, once=True)
    loop, _engine, runner = _loop(tmp_path, provider, rules=(rule,), registry=_registry(tool))
    list(loop.run("hello"))
    assert "before_prompt" in " ".join(_bodies(provider.calls[0][0]))
    assert "before_prompt" not in " ".join(_bodies(provider.calls[1][0]))
    assert "before_prompt" not in " ".join(_bodies(loop.session.messages))
    assert runner.consume_prompt_injections() == ()
    runner.shutdown()


@pytest.mark.parametrize("event", [HookEvent.AFTER_MODEL_RESPONSE, HookEvent.AFTER_TOOL])
def test_later_event_injection_goes_to_next_request(tmp_path, event):
    tool = FakeTool()
    provider = Provider([[ToolCallEvent([ToolCall("a", "read_file", {})])], [TextDelta("done")]])
    rule = _rule("later_prompt", event, once=True)
    loop, _engine, runner = _loop(tmp_path, provider, rules=(rule,), registry=_registry(tool))
    list(loop.run("hello"))
    assert "later_prompt" not in " ".join(_bodies(provider.calls[0][0]))
    assert "later_prompt" in " ".join(_bodies(provider.calls[1][0]))
    assert "later_prompt" not in " ".join(_bodies(loop.session.messages))
    runner.shutdown()


def test_hook_deny_preserves_tool_result_order_and_skips_its_action(tmp_path):
    tool = FakeTool()
    provider = Provider([[
        ToolCallEvent([ToolCall("a", "read_file", {}), ToolCall("b", "read_file", {}), ToolCall("c", "read_file", {})]),
    ], [TextDelta("done")]])
    condition = parse_condition({"field": "tool.call_id", "exact": "b"})
    deny = _rule("deny_b", HookEvent.BEFORE_TOOL, HookActionType.SUBAGENT, {}, condition=condition, deny=True)
    loop, engine, runner = _loop(tmp_path, provider, rules=(deny,), registry=_registry(tool))
    events = list(loop.run("hello"))
    assert len(tool.calls) == 2
    assert [message.tool_call_id for message in loop.session.messages if message.role == "tool"] == ["a", "b", "c"]
    assert any(isinstance(event, AgentToolError) and event.tool_call.id == "b" and event.code == "hook_tool_denied" for event in events)
    assert any(message.role == "tool" and message.tool_call_id == "b" and "hook_tool_denied" in (message.content or "") for message in provider.calls[1][0])
    assert [event for event, _, _ in engine.trace].count(HookEvent.AFTER_TOOL) == 2
    assert not runner.diagnostics
    runner.shutdown()


def test_plan_and_permission_sandbox_reject_before_hook(tmp_path):
    rule = _rule("deny_everything", HookEvent.BEFORE_TOOL, HookActionType.SUBAGENT, {}, deny=True)
    write = FakeTool("write_file")
    plan_provider = Provider([[ToolCallEvent([ToolCall("plan", "write_file", {})])]])
    plan, plan_engine, plan_runner = _loop(tmp_path, plan_provider, rules=(rule,), registry=_registry(write))
    plan_events = list(plan.run("plan", mode=AgentMode.PLAN))
    assert any(isinstance(event, AgentToolError) and event.code == "disallowed_tool" for event in plan_events)
    assert HookEvent.BEFORE_TOOL not in [event for event, _, _ in plan_engine.trace]
    assert write.calls == []
    plan_runner.shutdown()

    read = FakeTool()
    sandbox_provider = Provider([[ToolCallEvent([ToolCall("outside", "read_file", {"path": "../outside"})])], [TextDelta("done")]])
    sandbox, sandbox_engine, sandbox_runner = _loop(tmp_path, sandbox_provider, rules=(rule,), registry=_registry(read))
    sandbox_events = list(sandbox.run("read"))
    assert any(isinstance(event, AgentToolError) and event.code == "permission_denied" for event in sandbox_events)
    assert HookEvent.BEFORE_TOOL not in [event for event, _, _ in sandbox_engine.trace]
    assert read.calls == []
    sandbox_runner.shutdown()


def test_plan_mode_hook_shell_action_cannot_execute_via_turn_event(tmp_path):
    class RecordingShell:
        def __init__(self):
            self.calls = 0

        def run_shell(self, command, timeout_seconds):
            self.calls += 1
            return HookGatewayResult(True)

    shell = RecordingShell()
    runner = HookActionRunner(shell_gateway=shell)
    rule = _rule("plan_shell", HookEvent.TURN_START, HookActionType.SHELL, {"command": "git status"})
    provider = Provider([[TextDelta("plan answer")]])
    loop, _engine, runner = _loop(tmp_path, provider, rules=(rule,), runner=runner)
    assert any(isinstance(event, AgentFinalAnswer) for event in loop.run("plan", mode=AgentMode.PLAN))
    assert shell.calls == 0
    assert runner.diagnostics[0].code == "hook_action_failed"
    runner.shutdown()


def test_mcp_permission_denial_precedes_hook_and_has_no_remote_call(tmp_path):
    class MCPTool(FakeTool):
        mcp_metadata = {"mcp_server": "fixture", "mcp_tool": "work", "transport": "stdio"}

    mcp = MCPTool("mcp__fixture__work__123456789abc")
    provider = Provider([[ToolCallEvent([ToolCall("mcp", mcp.name, {})])], [TextDelta("done")]])
    permission = PermissionManager(confirmer=DenyByDefaultConfirmer())
    deny = _rule("deny", HookEvent.BEFORE_TOOL, HookActionType.SUBAGENT, {}, deny=True)
    loop, engine, runner = _loop(tmp_path, provider, rules=(deny,), registry=_registry(mcp), permission=permission)
    events = list(loop.run("mcp"))
    assert any(isinstance(event, AgentToolError) and event.code == "permission_denied" for event in events)
    assert HookEvent.BEFORE_TOOL not in [event for event, _, _ in engine.trace]
    assert mcp.calls == []
    runner.shutdown()


def test_hard_deny_precedes_hook_even_in_trusted_mode(tmp_path):
    command = FakeTool("run_command")
    provider = Provider([[ToolCallEvent([ToolCall("bad", "run_command", {"command": "shutdown /s"})])], [TextDelta("done")]])
    deny = _rule("deny", HookEvent.BEFORE_TOOL, HookActionType.SUBAGENT, {}, deny=True)
    loop, engine, runner = _loop(tmp_path, provider, rules=(deny,), registry=_registry(command))
    events = list(loop.run("run"))
    assert any(isinstance(event, AgentToolError) and event.code == "permission_denied" for event in events)
    assert HookEvent.BEFORE_TOOL not in [event for event, _, _ in engine.trace]
    assert command.calls == []
    runner.shutdown()


def test_hook_action_timeout_does_not_become_tool_deny(tmp_path):
    class TimeoutShell:
        def run_shell(self, command, timeout_seconds):
            raise TimeoutError("private failure")

    tool = FakeTool()
    runner = HookActionRunner(shell_gateway=TimeoutShell())
    rule = _rule("timeout", HookEvent.BEFORE_TOOL, HookActionType.SHELL, {"command": "git status"})
    provider = Provider([[ToolCallEvent([ToolCall("a", "read_file", {})])], [TextDelta("done")]])
    loop, engine, runner = _loop(tmp_path, provider, rules=(rule,), runner=runner, registry=_registry(tool))
    events = list(loop.run("hello"))
    assert len(tool.calls) == 1
    assert not any(isinstance(event, AgentToolError) and event.code == "hook_tool_denied" for event in events)
    assert runner.diagnostics[0].code == "hook_timeout"
    runner.shutdown()


def test_hook_engine_internal_error_does_not_deny_tool(tmp_path):
    tool = FakeTool()
    provider = Provider([[ToolCallEvent([ToolCall("a", "read_file", {})])], [TextDelta("done")]])
    loop, engine, runner = _loop(tmp_path, provider, registry=_registry(tool))
    original_emit = engine.emit

    def broken_emit(event, context, *, hook_origin=False):
        if event is HookEvent.BEFORE_TOOL:
            raise RuntimeError("private failure")
        return original_emit(event, context, hook_origin=hook_origin)

    engine.emit = broken_emit
    events = list(loop.run("hello"))
    assert len(tool.calls) == 1
    assert not any(isinstance(event, AgentToolError) and event.code == "hook_tool_denied" for event in events)
    runner.shutdown()


def test_active_skill_whitelist_rejects_before_hook(tmp_path):
    path = tmp_path / ".newcode" / "skills" / "reader.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: reader\ndescription: reader.\ntools:\n- read_file\nmode: shared\n---\n\nSOP\n",
        encoding="utf-8",
    )
    write = FakeTool("write_file")
    registry = _registry(write)
    registry.register(FakeTool("read_file"), read_only=True, do_visible=True)
    provider = Provider([
        [ToolCallEvent([ToolCall("load", "load_skill", {"name": "reader", "parameters": {}})])],
        [ToolCallEvent([ToolCall("write", "write_file", {})])],
    ])
    loop, engine, runner = _loop(tmp_path, provider, registry=registry)
    events = list(loop.run("load skill"))
    assert any(isinstance(event, AgentToolError) and event.code == "disallowed_tool" for event in events)
    before_names = [context.fields.get("tool.name") for event, context, _ in engine.trace if event is HookEvent.BEFORE_TOOL]
    assert before_names == ["load_skill"]
    assert write.calls == []
    runner.shutdown()


def test_async_before_model_injection_missed_first_request_then_used_once(tmp_path):
    class BlockingShell:
        def __init__(self):
            self.entered = Event()
            self.release = Event()

        def run_shell(self, command, timeout_seconds):
            self.entered.set()
            self.release.wait(1)
            return HookGatewayResult(True)

    class CompletionRunner(HookActionRunner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.prompt_ready = Event()

        def _perform(self, rule, context, generation):
            result = super()._perform(rule, context, generation)
            if rule.id == "async_prompt":
                self.prompt_ready.set()
            return result

    shell = BlockingShell()
    runner = CompletionRunner(shell_gateway=shell)

    def on_call(number):
        if number == 1:
            assert shell.entered.wait(1)
            shell.release.set()
            assert runner.prompt_ready.wait(1)

    tool = FakeTool()
    provider = Provider([[ToolCallEvent([ToolCall("a", "read_file", {})])], [TextDelta("done")]], on_call=on_call)
    rules = (
        _rule("block", HookEvent.TURN_START, HookActionType.SHELL, {"command": "git status"}),
        _rule("async_prompt", HookEvent.BEFORE_MODEL_REQUEST, args={"text": "async payload"}, once=True),
    )
    rules = tuple(HookRule(rule.id, rule.event, rule.action, rule.source,
                           condition=rule.condition, once=rule.once, async_requested=True) for rule in rules)
    loop, _engine, runner = _loop(tmp_path, provider, rules=rules, runner=runner, registry=_registry(tool))
    list(loop.run("hello"))
    first_snapshot = " ".join(_bodies(provider.calls[0][0]))
    second_snapshot = " ".join(_bodies(provider.calls[1][0]))
    assert "async payload" not in first_snapshot
    assert "async payload" in second_snapshot
    assert runner.consume_prompt_injections() == ()
    runner.shutdown()


def test_end_of_session_discards_unconsumed_injection(tmp_path):
    provider = Provider([[TextDelta("done")]])
    rule = _rule("later", HookEvent.AFTER_MODEL_RESPONSE, once=True)
    loop, engine, runner = _loop(tmp_path, provider, rules=(rule,))
    list(loop.run("hello"))
    engine.reset_session()
    assert runner.consume_prompt_injections() == ()
    runner.shutdown()


def test_request_injection_is_not_submitted_to_memory(tmp_path):
    class MemorySpy:
        def __init__(self):
            self.calls = []

        def submit(self, messages):
            self.calls.append(list(messages))
            return True

    memory = MemorySpy()
    provider = Provider([[TextDelta("done")]])
    rule = _rule("private_hook_prompt", HookEvent.BEFORE_MODEL_REQUEST)
    loop, _engine, runner = _loop(tmp_path, provider, rules=(rule,))
    loop.memory_service = memory
    list(loop.run("hello"))
    assert "private_hook_prompt" in " ".join(_bodies(provider.calls[0][0]))
    assert len(memory.calls) == 1
    assert "private_hook_prompt" not in " ".join(_bodies(memory.calls[0]))
    runner.shutdown()


def test_cancel_provider_error_and_unhandled_exception_events(tmp_path):
    cancelled, cancel_engine, cancel_runner = _loop(tmp_path, Provider([]))
    assert isinstance(list(cancelled.run("cancel", cancel_flag=True))[-1], AgentStopped)
    assert HookEvent.TURN_CANCELLED in [event for event, _, _ in cancel_engine.trace]
    cancel_runner.shutdown()

    class ErrorProvider:
        def stream_chat(self, messages, tools=None, allow_tool_calls=True):
            raise ProviderError("secret-value")

    error, error_engine, error_runner = _loop(tmp_path, ErrorProvider())
    assert isinstance(list(error.run("error"))[-1], AgentStopped)
    assert HookEvent.TURN_EXCEPTION in [event for event, _, _ in error_engine.trace]
    assert "secret-value" not in str([context.fields for _, context, _ in error_engine.trace])
    error_runner.shutdown()

    class BrokenBuilder:
        def build_messages(self, messages, context, **kwargs):
            raise RuntimeError("secret-value")

    broken, broken_engine, broken_runner = _loop(tmp_path, Provider([]))
    broken.prompt_builder = BrokenBuilder()
    with pytest.raises(RuntimeError):
        list(broken.run("boom"))
    assert HookEvent.TURN_EXCEPTION in [event for event, _, _ in broken_engine.trace]
    broken_runner.shutdown()


def test_non_deny_action_results_stay_out_of_main_history(tmp_path):
    tool = FakeTool()
    provider = Provider([[ToolCallEvent([ToolCall("a", "read_file", {})])], [TextDelta("done")]])

    class FakeShell:
        def run_shell(self, command, timeout_seconds):
            return HookGatewayResult(True)

    class FakeHttp:
        def request_pinned(self, **kwargs):
            return HookHttpResponse(200, kwargs["resolved_ip"], b"secret remote response")

    runner = HookActionRunner(
        network=HookNetworkPolicy(True, ("example.com",)), shell_gateway=FakeShell(),
        http_transport=FakeHttp(), resolver=lambda _host: ("93.184.216.34",),
    )
    rules = (
        _rule("shell", HookEvent.AFTER_TOOL, HookActionType.SHELL, {"command": "git status"}),
        _rule("http", HookEvent.AFTER_TOOL, HookActionType.HTTP_REQUEST, {"url": "https://example.com/"}),
        _rule("prompt", HookEvent.AFTER_TOOL),
        _rule("subagent", HookEvent.AFTER_TOOL, HookActionType.SUBAGENT, {}),
    )
    loop, _engine, runner = _loop(tmp_path, provider, rules=rules, runner=runner, registry=_registry(tool))
    list(loop.run("hello"))
    session_text = " ".join(_bodies(loop.session.messages))
    assert "secret remote response" not in session_text
    assert "hook_subagent_not_available" not in session_text
    assert "prompt" not in session_text
    assert "prompt" in " ".join(_bodies(provider.calls[1][0]))
    assert runner.diagnostics[0].code == "hook_subagent_not_available"
    runner.shutdown()
