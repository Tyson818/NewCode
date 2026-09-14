import json
from pathlib import Path

from newcode.context.artifacts import ArtifactStore
from newcode.context.prevention import externalize_tool_results
from newcode.session import ChatSession
from newcode.tools.types import ToolResult


def _large_result(size: int, secret: str = "secret") -> ToolResult:
    return ToolResult.success("read_file", {"content": secret + "x" * size})


def test_single_large_tool_result_is_externalized_without_changing_user(tmp_path: Path):
    session = ChatSession()
    session.add_user_message("keep this exactly")
    session.add_tool_result("call", _large_result(20_000))
    result = externalize_tool_results(session, ArtifactStore(tmp_path, "session", ("secret",)))
    assert result.externalized == 1
    assert session.messages[0].content == "keep this exactly"
    replacement = json.loads(session.messages[1].content or "{}")
    assert replacement["omitted"] is True
    assert "secret" not in replacement["preview"]
    assert (tmp_path / replacement["context_artifact"]).is_file()


def test_cumulative_tool_results_select_largest_first(tmp_path: Path):
    session = ChatSession()
    session.add_tool_result("a", _large_result(13_000))
    session.add_tool_result("b", _large_result(12_000))
    result = externalize_tool_results(session, ArtifactStore(tmp_path, "session"))
    assert result.externalized == 1
    assert "context_artifact" in (session.messages[0].content or "")
    assert "context_artifact" not in (session.messages[1].content or "")
