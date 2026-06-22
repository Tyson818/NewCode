from __future__ import annotations

from pathlib import Path

from .types import ToolFailure


BLOCKED_DIRS = {".git"}


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve_user_path(self, path: str) -> Path:
        if not path:
            raise ToolFailure("invalid_arguments", "路径不能为空", {"argument": "path"})

        raw_path = Path(path)
        candidate = raw_path if raw_path.is_absolute() else self.root / raw_path
        resolved = candidate.resolve()

        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ToolFailure(
                "path_not_allowed",
                "路径超出当前工作区，已拒绝访问",
                {"path": path},
            ) from exc

        relative_parts = resolved.relative_to(self.root).parts
        if any(part in BLOCKED_DIRS for part in relative_parts):
            raise ToolFailure(
                "path_not_allowed",
                "不允许访问受保护的工作区内部路径",
                {"path": path},
            )

        return resolved

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()


def is_skipped_path(path: Path) -> bool:
    return any(part in {".git", ".venv", "__pycache__"} for part in path.parts)
