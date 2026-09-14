from pathlib import Path

from newcode.agent import AgentFinalAnswer, AgentLoop
from newcode.context.manager import ContextManager
from newcode.providers.base import TextDelta
from newcode.session import ChatSession
from newcode.tools.registry import create_default_registry
from newcode.tools.types import ToolContext
from newcode.tools.types import ToolResult


def _summary() -> str:
    names = ("用户目标与原文约束", "当前任务状态", "关键事实与决策", "文件与工具结果索引", "验证证据", "后续动作与未决问题", "安全与边界")
    body = "".join(f"## {name}\n证据：x 推断：无 未确认：无\n" for name in names)
    return f"<analysis_draft>draft</analysis_draft><structured_summary>{body}</structured_summary>"


class Provider:
    def __init__(self):
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), tools, allow_tool_calls))
        if not allow_tool_calls:
            yield TextDelta(_summary())
        else:
            yield TextDelta("done")


def test_agent_loop_summary_uses_zero_tools_before_main_request(tmp_path: Path):
    session = ChatSession()
    for index in range(12):
        session.add_user_message("x" * 10_000 + str(index))
    provider = Provider()
    manager = ContextManager(session, tmp_path)
    loop = AgentLoop(provider=provider, session=session, registry=create_default_registry(), tool_context=ToolContext(tmp_path), context_manager=manager)
    events = list(loop.run("current"))
    assert any(isinstance(event, AgentFinalAnswer) for event in events)
    assert provider.calls[0][1] == []
    assert provider.calls[0][2] is False
    assert provider.calls[1][2] is True
    assert session.messages[-2].content == "current"


def test_main_request_sees_externalized_tool_preview_not_raw_result(tmp_path: Path):
    session = ChatSession()
    session.add_user_message("keep")
    session.add_tool_result("old", ToolResult.success("read_file", {"content": "secret" + "x" * 20_000}))
    provider = Provider()
    manager = ContextManager(session, tmp_path, ("secret",))
    loop = AgentLoop(provider=provider, session=session, registry=create_default_registry(), tool_context=ToolContext(tmp_path), context_manager=manager)
    list(loop.run("current"))
    main_messages = provider.calls[0][0]
    tool_contents = [message.content or "" for message in main_messages if message.role == "tool"]
    assert any("context_artifact" in content for content in tool_contents)
    assert all("secret" not in content for content in tool_contents)
