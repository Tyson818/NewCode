from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from newcode.session import ChatSession

from .artifacts import ArtifactStore
from .estimator import TokenEstimator
from .history import AUTO_THRESHOLD, compact_history, select_recent_start
from .prevention import externalize_tool_results


class ContextManager:
    def __init__(self, session: ChatSession, workspace_root: Path, sensitive_values: tuple[str, ...] = (), *, artifact_session_id: str = "context-session") -> None:
        self.session = session
        self.estimator = TokenEstimator()
        self.artifacts = ArtifactStore(workspace_root, artifact_session_id, sensitive_values)
        self.consecutive_failures = 0
        self.circuit_open = False

    def prepare(self, generator: Callable[[str], str] | None = None) -> bool:
        externalize_tool_results(self.session, self.artifacts)
        self.estimator.invalidate_for_version(self.session.context_version)
        estimated = self.estimator.estimate(self.session.messages, self.session.context_version)
        if generator is None or self.circuit_open or estimated < AUTO_THRESHOLD:
            return False
        success = compact_history(self.session, generator)
        if success:
            self.consecutive_failures = 0
            self.circuit_open = False
            self.estimator.invalidate_for_version(self.session.context_version)
            return True
        self.consecutive_failures += 1
        self.circuit_open = self.consecutive_failures >= 3
        return False

    def retry_manual(self, generator: Callable[[str], str]) -> bool:
        success = compact_history(self.session, generator)
        if success:
            self.consecutive_failures = 0
            self.circuit_open = False
        else:
            self.consecutive_failures += 1
            self.circuit_open = self.consecutive_failures >= 3
        return success

    def manual_compact(self, generator: Callable[[str], str]) -> str:
        externalize_tool_results(self.session, self.artifacts)
        self.estimator.invalidate_for_version(self.session.context_version)
        if select_recent_start(self.session.messages) <= 0:
            return "no_history"
        return "compacted" if self.retry_manual(generator) else "failed"

    def cleanup(self) -> bool:
        try:
            self.artifacts.cleanup_current()
            return True
        except (OSError, ValueError):
            return False

    def record_usage(self, usage: Any) -> bool:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        return self.estimator.record_usage(prompt_tokens, self.session.context_version)
