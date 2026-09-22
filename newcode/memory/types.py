from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class MemoryScope(str, Enum):
    USER = "user"
    PROJECT = "project"


class MemoryCategory(str, Enum):
    USER_PREFERENCE = "用户偏好"
    CORRECTION_FEEDBACK = "纠正反馈"
    PROJECT_KNOWLEDGE = "项目知识"
    REFERENCE = "参考资料"


@dataclass(frozen=True)
class MemoryNote:
    id: str
    category: MemoryCategory
    scope: MemoryScope
    content: str
    tags: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    workspace_fingerprint: str | None = None
