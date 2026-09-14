from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .redaction import redact_value
from .types import ArtifactWriteResult

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
_SESSION_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class ArtifactStore:
    def __init__(self, workspace_root: Path, session_id: str, sensitive_values: tuple[str, ...] = ()) -> None:
        if not session_id or len(session_id) > 64 or any(char not in _SESSION_ID for char in session_id):
            raise ValueError("session_id 不安全")
        self.workspace_root = workspace_root.resolve()
        self.root = self.workspace_root / ".newcode" / "context-artifacts"
        self.session_dir = self.root / session_id
        self.sensitive_values = sensitive_values
        self._sequence = 0

    def write(self, tool_name: str, data: Any) -> ArtifactWriteResult:
        directory = self._ensure_session_dir()
        self._sequence += 1
        payload = {"version": 1, "sequence": self._sequence, "tool_name": tool_name, "data": redact_value(data, self.sensitive_values)}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        truncated = len(encoded) > MAX_ARTIFACT_BYTES
        if truncated:
            payload["data"] = _truncate_data(payload["data"])
            payload["truncated"] = True
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        name = f"artifact-{self._sequence:06d}.json"
        path = directory / name
        self._assert_inside(path, directory)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(encoded)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return ArtifactWriteResult(str(path.relative_to(self.workspace_root)).replace("\\", "/"), truncated, len(encoded))

    def cleanup_current(self) -> None:
        if self.session_dir.exists():
            self._assert_inside(self.session_dir, self.root)
            shutil.rmtree(self.session_dir)

    def cleanup_stale(self, now: float | None = None, max_age_seconds: float = 7 * 24 * 60 * 60) -> None:
        if not self.root.exists():
            return
        current = time.time() if now is None else now
        for entry in self.root.iterdir():
            if not entry.is_dir() or entry == self.session_dir:
                continue
            self._assert_inside(entry, self.root)
            if current - entry.stat().st_mtime > max_age_seconds:
                shutil.rmtree(entry)

    def _ensure_session_dir(self) -> Path:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._assert_inside(self.session_dir, self.root)
        return self.session_dir

    def _assert_inside(self, path: Path, root: Path) -> None:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(resolved_root) or not resolved_path.is_relative_to(self.workspace_root):
            raise ValueError("artifact 路径超出 workspace sandbox")


def _truncate_data(value: Any) -> Any:
    if isinstance(value, str):
        return value[:1024] + "[TRUNCATED]"
    return {"notice": "[TRUNCATED]"}
