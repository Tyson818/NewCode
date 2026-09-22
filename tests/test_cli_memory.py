from __future__ import annotations

from io import StringIO
from pathlib import Path

from newcode import cli
from newcode.memory.service import MemoryGenerationRequest
from newcode.providers.base import TextDelta
from newcode.session import ChatSession
from newcode.tools.types import ToolContext


class Provider:
    def __init__(self):
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), tools, allow_tool_calls))
        yield TextDelta('{"action":"ignore"}' if not allow_tool_calls else "answer")


class MemorySpy:
    def __init__(self):
        self.submits = 0
        self.shutdowns = 0

    def submit(self, messages):
        self.submits += 1
        return True

    def shutdown(self):
        self.shutdowns += 1
        return True


def test_cli_passes_single_memory_service_to_natural_agent_turn(tmp_path: Path):
    values = iter(("hello", "/exit"))
    memory = MemorySpy()

    cli.run_conversation(
        Provider(),
        ChatSession(),
        tool_context=ToolContext(tmp_path),
        memory_service=memory,
        input_func=lambda prompt: next(values),
        output=StringIO(),
    )

    assert memory.submits == 1
    assert memory.shutdowns == 1


def test_memory_provider_bridge_keeps_tools_empty(tmp_path: Path):
    provider = Provider()
    request = MemoryGenerationRequest(prompt="safe", tools=(), allow_tool_calls=False, allow_file_reads=False)

    response = cli._generate_memory(provider, request)

    assert response == '{"action":"ignore"}'
    assert provider.calls[0][1] == []
    assert provider.calls[0][2] is False
