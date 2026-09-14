from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageAnchor:
    prompt_tokens: int
    session_version: int


@dataclass(frozen=True)
class ArtifactWriteResult:
    relative_path: str
    truncated: bool
    byte_count: int
