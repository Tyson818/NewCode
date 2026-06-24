import pytest

from newcode.agent.mode import AgentMode, allowed_tool_names
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import ToolResult, ToolSpec


class DummyTool:
    @property
    def spec(self):
        return ToolSpec(
            name="dummy",
            description="dummy tool",
            parameters={"type": "object", "properties": {}},
        )

    def run(self, arguments, context):
        return ToolResult.success("dummy", {})


def test_default_registry_contains_core_tools():
    registry = create_default_registry()

    assert registry.names() == [
        "find_files",
        "read_file",
        "replace_in_file",
        "run_command",
        "search_code",
        "write_file",
    ]


def test_registry_rejects_duplicate_names():
    registry = ToolRegistry()
    registry.register(DummyTool())

    with pytest.raises(ValueError):
        registry.register(DummyTool())


def test_registry_converts_to_openai_tools_schema():
    registry = ToolRegistry()
    registry.register(DummyTool())

    assert registry.to_openai_tools() == [
        {
            "type": "function",
            "function": {
                "name": "dummy",
                "description": "dummy tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_run_command_schema_guides_windows_commands():
    registry = create_default_registry()
    tools = {
        tool["function"]["name"]: tool["function"]
        for tool in registry.to_openai_tools()
    }

    description = tools["run_command"]["description"]
    command_description = tools["run_command"]["parameters"]["properties"]["command"]["description"]

    assert "Windows" in description
    assert "dir" in description
    assert "Get-ChildItem" in description
    assert "不要默认使用 ls" in description
    assert "Windows" in command_description
    assert "dir" in command_description


def test_find_files_schema_guides_file_listing():
    registry = create_default_registry()
    tools = {
        tool["function"]["name"]: tool["function"]
        for tool in registry.to_openai_tools()
    }

    description = tools["find_files"]["description"]

    assert "列出" in description
    assert "查找" in description
    assert "优先使用" in description
    assert "而不是 shell 命令" in description

def test_default_registry_classifies_core_tools():
    registry = create_default_registry()

    assert registry.is_read_only("read_file") is True
    assert registry.is_read_only("find_files") is True
    assert registry.is_read_only("search_code") is True
    assert registry.has_side_effects("write_file") is True
    assert registry.has_side_effects("replace_in_file") is True
    assert registry.has_side_effects("run_command") is True


def test_run_command_is_not_read_only():
    registry = create_default_registry()

    assert registry.is_read_only("run_command") is False
    assert registry.has_side_effects("run_command") is True


def test_registry_exports_tools_by_mode_whitelist():
    registry = create_default_registry()

    tools = registry.to_openai_tools(allowed_tool_names(AgentMode.PLAN))

    assert {tool["function"]["name"] for tool in tools} == {
        "read_file",
        "find_files",
        "search_code",
    }
