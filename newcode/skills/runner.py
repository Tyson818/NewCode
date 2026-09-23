"""Skill shared/isolated 执行的受控桥接，不直接调用 Provider 或工具。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
from typing import Callable, Iterable

from newcode.agent import AgentFinalAnswer, AgentStopped
from newcode.agent.loop import AgentLoop
from newcode.agent.mode import AgentMode
from newcode.context.manager import ContextManager
from newcode.memory.store import redact_memory_text
from newcode.providers.base import ChatProvider
from newcode.session import ChatMessage, ChatSession
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolContext

from .state import ActiveSkillState
from .types import SkillActivation, SkillCatalog


MAX_ISOLATED_SUMMARY_CHARACTERS = 4_000


@dataclass(frozen=True)
class SkillRunResult:
    ok: bool
    summary: str = ""
    code: str | None = None


class SkillRunner:
    """只复用 AgentLoop；不构造 Provider client、不执行工具、不共享 child state。"""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        registry: ToolRegistry,
        tool_context: ToolContext,
        permission_manager: object,
        parent_session: ChatSession,
        parent_context: ContextManager,
        skill_catalog: SkillCatalog,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._tool_context = tool_context
        self._permission_manager = permission_manager
        self._parent_session = parent_session
        self._parent_context = parent_context
        self._catalog = skill_catalog

    def shared_input(self, activation: SkillActivation, parameters: Iterable[tuple[str, str]]) -> str:
        values = " ".join(f"{key}={value}" for key, value in parameters)
        return f"执行已激活的受控 Skill：{activation.loaded.metadata.frontmatter.name}\n用户参数：{values or '无'}"

    def run_isolated(
        self,
        activation: SkillActivation,
        parameters: Iterable[tuple[str, str]],
        *,
        mode: AgentMode,
    ) -> SkillRunResult:
        child_session = ChatSession(messages=list(_history(self._parent_session.messages, activation, self._tool_context.sensitive_values)))
        child_state = ActiveSkillState()
        child_state.activate(activation.loaded)
        child_context = ContextManager(
            child_session,
            self._tool_context.workspace_root,
            self._tool_context.sensitive_values,
            artifact_session_id=f"skill-child-{secrets.token_hex(8)}",
        )
        try:
            loop = AgentLoop(
                provider=self._provider,
                session=child_session,
                registry=self._registry,
                tool_context=self._tool_context,
                permission_manager=self._permission_manager,
                context_manager=child_context,
                memory_service=None,
                skill_state=child_state,
                skill_catalog=SkillCatalog((activation.loaded.metadata,)),
            )
            result = SkillRunResult(False, code="skill_isolated_failed")
            for event in loop.run(self.shared_input(activation, parameters), mode=mode):
                if isinstance(event, AgentFinalAnswer):
                    summary = _safe_summary(event.content, self._tool_context.sensitive_values)
                    self._parent_session.add_assistant_message(
                        f"【isolated Skill 结果摘要｜来源：{activation.loaded.metadata.frontmatter.name}】\n{summary}"
                    )
                    result = SkillRunResult(True, summary=summary)
                elif isinstance(event, AgentStopped):
                    result = SkillRunResult(False, code="skill_isolated_failed")
            return result
        except Exception:
            return SkillRunResult(False, code="skill_isolated_failed")
        finally:
            try:
                child_context.cleanup()
            except Exception:
                pass


def _history(
    messages: Iterable[ChatMessage],
    activation: SkillActivation,
    sensitive_values: tuple[str, ...],
) -> tuple[ChatMessage, ...]:
    count = activation.loaded.metadata.frontmatter.history_messages or 0
    if count == 0:
        return ()
    selected: list[ChatMessage] = []
    for message in reversed(tuple(messages)):
        if message.role not in ("user", "assistant") or not message.content or message.tool_calls:
            continue
        selected.append(ChatMessage(message.role, redact_memory_text(message.content, sensitive_values)))
        if len(selected) == count:
            break
    return tuple(reversed(selected))


def _safe_summary(value: str, sensitive_values: tuple[str, ...]) -> str:
    safe = redact_memory_text(value or "", sensitive_values).strip()
    return safe[:MAX_ISOLATED_SUMMARY_CHARACTERS]
