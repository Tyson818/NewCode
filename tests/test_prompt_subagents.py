from __future__ import annotations

import json

from newcode.prompt.builder import PromptBuilder
from newcode.prompt.reminder import PromptBuildContext, PromptEnvironment
from newcode.prompt.modules import DynamicPromptBackground
from newcode.session import ChatMessage
from newcode.subagents.types import AgentCatalog, AgentDefinition, AgentPermissionMode, AgentSource
from newcode.agent.mode import AgentMode


def test_catalog_prompt_projection_is_only_name_and_description():
    definition = AgentDefinition(
        name="reviewer", description="Review local changes", source=AgentSource.PROJECT,
        tools_allow=("read_file",), tools_deny=(), max_iterations=4,
        permission_mode=AgentPermissionMode.TRUSTED, model="sensitive-model",
        body="private full SOP", digest="private digest",
    )
    catalog = AgentCatalog((definition,))
    entries = catalog.startup_directory()
    assert entries == (type(entries[0])(name="reviewer", description="Review local changes"),)
    assert set(entries[0].__dict__) == {"name", "description"}


def test_prompt_builder_marks_agent_catalog_as_dynamic_background_without_persisting():
    context = PromptBuildContext(
        mode=AgentMode.DO, iteration=1, max_iterations=8,
        environment=PromptEnvironment(workspace_root=".", platform="test"),
    )
    projection = json.dumps([{"name": "reviewer", "description": "Review local changes"}], ensure_ascii=False)
    session_messages = [ChatMessage("user", "hello")]
    prompt = PromptBuilder().build_messages(
        session_messages,
        context,
        dynamic_background=DynamicPromptBackground(agent_catalog=projection),
    )
    dynamic = [message.content or "" for message in prompt if "Agent 目录" in (message.content or "")]
    assert len(dynamic) == 1 and "reviewer" in dynamic[0] and "Review local changes" in dynamic[0]
    assert "private full SOP" not in repr(prompt)
    assert session_messages == [ChatMessage("user", "hello")]
