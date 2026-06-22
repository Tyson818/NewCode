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
