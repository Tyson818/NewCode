import json

from newcode.tools.executor import execute_tool_call
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolContext, ToolFailure, ToolResult, ToolSpec
from newcode.tools.workspace import Workspace


class ExplodingTool:
    @property
    def spec(self):
        return ToolSpec(name="explode", description="explode", parameters={"type": "object"})

    def run(self, arguments, context):
        raise RuntimeError("boom secret-value")


class FailingTool:
    @property
    def spec(self):
        return ToolSpec(name="fail", description="fail", parameters={"type": "object"})

    def run(self, arguments, context):
        raise ToolFailure("invalid_arguments", "bad secret-value", {"value": "secret-value"})


class EchoTool:
    @property
    def spec(self):
        return ToolSpec(name="echo", description="echo", parameters={"type": "object"})

    def run(self, arguments, context):
        return ToolResult.success("echo", {"text": arguments["text"]})


def context(tmp_path):
    return ToolContext(workspace_root=tmp_path, sensitive_values=("secret-value",))


def test_workspace_rejects_outside_path(tmp_path):
    outside = tmp_path.parent / "outside.txt"

    try:
        Workspace(tmp_path).resolve_user_path(str(outside))
    except ToolFailure as exc:
        assert exc.code == "path_not_allowed"
    else:
        raise AssertionError("expected path rejection")


def test_workspace_rejects_git_path(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    try:
        Workspace(tmp_path).resolve_user_path(".git/config")
    except ToolFailure as exc:
        assert exc.code == "path_not_allowed"
    else:
        raise AssertionError("expected path rejection")


def test_execute_unknown_tool_returns_failure(tmp_path):
    result = execute_tool_call(
        ToolCall(id="1", name="missing", arguments={}),
        ToolRegistry(),
        context(tmp_path),
    )

    assert result.ok is False
    assert result.error.code == "unknown_tool"


def test_execute_tool_failure_is_structured_and_masked(tmp_path):
    registry = ToolRegistry()
    registry.register(FailingTool())

    result = execute_tool_call(
        ToolCall(id="1", name="fail", arguments={}),
        registry,
        context(tmp_path),
    )

    encoded = result.to_json()
    assert result.ok is False
    assert result.error.code == "invalid_arguments"
    assert "secret-value" not in encoded
    assert "[REDACTED]" in encoded


def test_execute_unexpected_exception_is_structured_and_masked(tmp_path):
    registry = ToolRegistry()
    registry.register(ExplodingTool())

    result = execute_tool_call(
        ToolCall(id="1", name="explode", arguments={}),
        registry,
        context(tmp_path),
    )

    assert result.ok is False
    assert result.error.code == "execution_error"
    assert "secret-value" not in result.to_json()


def test_execute_success_result_is_json_serializable(tmp_path):
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = execute_tool_call(
        ToolCall(id="1", name="echo", arguments={"text": "ok"}),
        registry,
        context(tmp_path),
    )

    assert json.loads(result.to_json())["data"] == {"text": "ok"}
