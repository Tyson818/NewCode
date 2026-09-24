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
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ToolFailure("path_not_allowed", "路径无法安全解析，已拒绝访问", {"path": path}) from exc

        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ToolFailure(
                "path_not_allowed",
                "路径超出当前工作区，已拒绝访问",
                {"path": path},
            ) from exc

        relative_parts = resolved.relative_to(self.root).parts
        if any(part.casefold() in BLOCKED_DIRS for part in relative_parts):
            raise ToolFailure(
                "path_not_allowed",
                "不允许访问受保护的工作区内部路径",
                {"path": path},
            )

        return resolved

    def relative(self, path: Path) -> str:
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(self.root)
            logical = path.absolute().relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolFailure("path_not_allowed", "路径超出当前工作区，已拒绝访问") from exc
        if any(part.casefold() in BLOCKED_DIRS for part in logical.parts):
            raise ToolFailure("path_not_allowed", "不允许访问受保护的工作区内部路径")
        return logical.as_posix()

    def contains_resolved(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(self.root)
            return not any(part.casefold() in BLOCKED_DIRS for part in path.relative_to(self.root).parts)
        except (OSError, RuntimeError, ValueError):
            return False


def is_skipped_path(path: Path) -> bool:
    return any(part.casefold() in {".git", ".venv", "__pycache__"} for part in path.parts)
