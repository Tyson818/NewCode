"""项目指令的受控本地加载器。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
import re


DEFAULT_USER_INSTRUCTIONS_ROOT = Path.home() / ".newcode"
USER_INSTRUCTIONS_NAME = "INSTRUCTIONS.md"
WORKSPACE_INSTRUCTIONS_RELATIVE_PATH = Path(".newcode") / "INSTRUCTIONS.md"
PROJECT_INSTRUCTIONS_NAME = "AGENTS.md"
MAX_INCLUDE_DEPTH = 5

_INCLUDE_PATTERN = re.compile(r"^@include\s+([^\s]+)$")


class InstructionSource(str, Enum):
    """指令层级；枚举定义顺序与加载优先级一致。"""

    USER = "user"
    WORKSPACE = "workspace"
    PROJECT = "project"


@dataclass(frozen=True)
class LoadedInstruction:
    """一层已经展开且可安全注入的项目指令。"""

    source: InstructionSource
    content: str


@dataclass(frozen=True)
class InstructionLoadError:
    """不含路径或文件内容的安全加载诊断。"""

    code: str
    source: InstructionSource


@dataclass(frozen=True)
class ProjectInstructionsLoadResult:
    """按注入优先级（高到低）排列的指令与安全错误。"""

    instructions: tuple[LoadedInstruction, ...]
    errors: tuple[InstructionLoadError, ...] = ()


def load_project_instructions(
    workspace_root: Path,
    *,
    user_root: Path | None = None,
) -> ProjectInstructionsLoadResult:
    """加载三层指令，并将高优先级内容排在返回结果前面。"""

    workspace = Path(workspace_root).resolve(strict=False)
    user_instructions_root = (user_root or DEFAULT_USER_INSTRUCTIONS_ROOT).resolve(
        strict=False
    )
    layers = (
        (InstructionSource.USER, user_instructions_root / USER_INSTRUCTIONS_NAME, user_instructions_root),
        (
            InstructionSource.WORKSPACE,
            workspace / WORKSPACE_INSTRUCTIONS_RELATIVE_PATH,
            workspace,
        ),
        (InstructionSource.PROJECT, workspace / PROJECT_INSTRUCTIONS_NAME, workspace),
    )

    loaded: dict[InstructionSource, LoadedInstruction] = {}
    errors: list[InstructionLoadError] = []
    for source, path, allowed_root in layers:
        content = _load_layer(path, allowed_root, source, errors)
        if content:
            loaded[source] = LoadedInstruction(source=source, content=content)

    return ProjectInstructionsLoadResult(
        instructions=tuple(
            loaded[source]
            for source in (
                InstructionSource.PROJECT,
                InstructionSource.WORKSPACE,
                InstructionSource.USER,
            )
            if source in loaded
        ),
        errors=tuple(errors),
    )


def _load_layer(
    path: Path,
    allowed_root: Path,
    source: InstructionSource,
    errors: list[InstructionLoadError],
) -> str:
    if not path.exists():
        return ""
    if not _is_safe_path(path, allowed_root):
        _add_error(errors, "instruction_path_outside_workspace", source)
        return ""

    visited: set[Path] = set()
    return _expand_file(path.resolve(strict=False), allowed_root, source, 0, visited, errors).strip()


def _expand_file(
    path: Path,
    allowed_root: Path,
    source: InstructionSource,
    depth: int,
    visited: set[Path],
    errors: list[InstructionLoadError],
) -> str:
    canonical = path.resolve(strict=False)
    if canonical in visited:
        _add_error(errors, "instruction_include_cycle", source)
        return ""
    if not _is_safe_path(canonical, allowed_root):
        _add_error(errors, "instruction_path_outside_workspace", source)
        return ""

    visited.add(canonical)
    try:
        text = canonical.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        _add_error(errors, "instruction_load_failed", source)
        return ""

    output: list[str] = []
    for line in text.splitlines(keepends=True):
        include = _parse_include(line)
        if include is None:
            output.append(line)
            continue
        if include == "":
            _add_error(errors, "instruction_load_failed", source)
            continue
        if depth >= MAX_INCLUDE_DEPTH:
            _add_error(errors, "instruction_include_depth", source)
            continue
        include_path = _safe_include_path(canonical, include, allowed_root)
        if include_path is None:
            _add_error(errors, "instruction_path_outside_workspace", source)
            continue
        expanded = _expand_file(
            include_path,
            allowed_root,
            source,
            depth + 1,
            visited,
            errors,
        )
        output.append(expanded)
        if expanded and line.endswith(("\n", "\r")) and not expanded.endswith(("\n", "\r")):
            output.append("\n")
    return "".join(output)


def _parse_include(line: str) -> str | None:
    """返回 None 表示普通文本，空串表示格式非法的 include 指令。"""

    stripped = line.strip()
    if not stripped.startswith("@include"):
        return None
    matched = _INCLUDE_PATTERN.fullmatch(stripped)
    if matched is None:
        return ""
    return matched.group(1)


def _safe_include_path(current: Path, value: str, allowed_root: Path) -> Path | None:
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if (
        relative.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in relative.parts
        or not relative.parts
        or relative.suffix.lower() != ".md"
    ):
        return None
    candidate = current.parent.joinpath(*relative.parts)
    return candidate.resolve(strict=False) if _is_safe_path(candidate, allowed_root) else None


def _is_safe_path(path: Path, allowed_root: Path) -> bool:
    """拒绝 root 外路径，以及允许根下任意符号链接。"""

    root = allowed_root.resolve(strict=False)
    candidate = path.resolve(strict=False)
    try:
        raw_relative = Path(path).absolute().relative_to(root)
        candidate.relative_to(root)
    except ValueError:
        return False

    current = root
    for part in raw_relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _add_error(
    errors: list[InstructionLoadError],
    code: str,
    source: InstructionSource,
) -> None:
    error = InstructionLoadError(code=code, source=source)
    if error not in errors:
        errors.append(error)
