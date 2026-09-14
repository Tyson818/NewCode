from io import StringIO
from pathlib import Path

from newcode.cli import run_conversation
from newcode.context.manager import ContextManager
from newcode.providers.base import TextDelta
from newcode.session import ChatSession
from newcode.tools.registry import create_default_registry
from newcode.tools.types import ToolContext


def _summary() -> str:
    names = ("用户目标与原文约束", "当前任务状态", "关键事实与决策", "文件与工具结果索引", "验证证据", "后续动作与未决问题", "安全与边界")
    body = "".join(f"## {name}\n证据：x 推断：无 未确认：无\n" for name in names)
    return f"<analysis_draft>draft</analysis_draft><structured_summary>{body}</structured_summary>"


class Provider:
    def __init__(self):
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((tools, allow_tool_calls))
        yield TextDelta(_summary() if not allow_tool_calls else "answer")


def _inputs(*values):
    iterator = iter(values)
    return lambda prompt: next(iterator)


def test_compact_forces_summary_and_cleanup(tmp_path: Path):
    session = ChatSession()
    for index in range(6):
        session.add_user_message("x" * 5_000 + str(index))
    manager = ContextManager(session, tmp_path)
    manager.artifacts.write("tool", {"value": 1})
    provider = Provider()
    output = StringIO()
    run_conversation(provider, session, registry=create_default_registry(), tool_context=ToolContext(tmp_path), context_manager=manager, input_func=_inputs("/compact", "/exit"), output=output)
    assert "上下文已压缩" in output.getvalue()
    assert provider.calls[0] == ([], False)
    assert not manager.artifacts.session_dir.exists()


def test_compact_without_history_reports_safe_status(tmp_path: Path):
    output = StringIO()
    run_conversation(Provider(), ChatSession(), tool_context=ToolContext(tmp_path), input_func=_inputs("/compact", "/exit"), output=output)
    assert "没有可压缩的历史" in output.getvalue()
