import pytest

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
