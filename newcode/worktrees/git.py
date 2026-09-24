"""Manager-only fixed-argv, read-only Git adapter.

No method accepts a command or arbitrary argv from callers. Lifecycle writes are
intentionally absent from this Phase 1 adapter and are not child capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Callable, Mapping, Protocol, Sequence

from .types import WorktreeError, WorktreeErrorCode


MAX_GIT_OUTPUT_BYTES = 1_048_576
DEFAULT_GIT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class GitResult:
    stdout: bytes


Runner = Callable[..., object]


class WorktreeGitAdapter:
    """仅供 Manager 使用的 Git 只读查询接口，绝非 child 命令代理。"""

    def __init__(
        self,
        *,
        empty_hooks_dir: Path,
        executable: str | Path | None = None,
        timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
        max_output_bytes: int = MAX_GIT_OUTPUT_BYTES,
        runner: Runner | None = None,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60:
            raise ValueError("worktree_git_failed")
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or not 1 <= max_output_bytes <= MAX_GIT_OUTPUT_BYTES:
            raise ValueError("worktree_git_failed")
        self._hooks = _validate_empty_hooks_dir(Path(empty_hooks_dir))
        resolved = str(executable) if executable is not None else shutil.which("git")
        if not resolved:
            raise WorktreeError(WorktreeErrorCode.GIT_UNAVAILABLE)
        self._executable = resolved
        self._timeout = float(timeout_seconds)
        self._max_output = max_output_bytes
        self._runner = runner or subprocess.run
        self._base_env = dict(base_env or os.environ)

    def repository_root(self, cwd: Path) -> Path:
        return _canonical_git_path(self._text(self._query(cwd, "rev-parse", "--show-toplevel")))

    def common_git_dir(self, cwd: Path) -> Path:
        value = Path(self._text(self._query(cwd, "rev-parse", "--git-common-dir")))
        candidate = value if value.is_absolute() else Path(cwd).resolve() / value
        return _canonical_git_path(str(candidate))

    def is_bare_repository(self, cwd: Path) -> bool:
        value = self._text(self._query(cwd, "rev-parse", "--is-bare-repository"))
        if value not in {"true", "false"}:
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
        return value == "true"

    def head(self, cwd: Path) -> str:
        value = self._text(self._query(cwd, "rev-parse", "--verify", "HEAD"))
        if not _valid_object_id(value):
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
        return value

    def current_branch(self, cwd: Path) -> str:
        value = self._text(self._query(cwd, "symbolic-ref", "--quiet", "--short", "HEAD"))
        if not value or "\n" in value or any(ord(char) < 32 for char in value):
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
        return value

    def list_worktrees(self, cwd: Path) -> tuple[str, ...]:
        output = self._query(cwd, "worktree", "list", "--porcelain")
        return _parse_porcelain_records(output)

    def status(self, cwd: Path, *, include_ignored: bool = False) -> bytes:
        if include_ignored:
            return self._query(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
        return self._query(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")

    def ignored_paths(self, cwd: Path) -> bytes:
        return self._query(cwd, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")

    def is_tracked_path(self, cwd: Path, relative_path: str) -> bool:
        checked = _validated_repository_relative_path(relative_path)
        result = self._execute(
            cwd,
            ("ls-files", "--error-unmatch", "--", checked),
            allow_exit_codes=frozenset({0, 1}),
        )
        return result.returncode == 0

    def is_ignored_path(self, cwd: Path, relative_path: str) -> bool:
        checked = _validated_repository_relative_path(relative_path)
        result = self._execute(
            cwd,
            ("check-ignore", "--quiet", "--", checked),
            allow_exit_codes=frozenset({0, 1}),
        )
        return result.returncode == 0

    def upstream_ahead_count(self, cwd: Path) -> int:
        # 远端跟踪 ref 可能过期；本章不联网更新，故不能把缓存的远端状态当成当前 clean 证据。
        upstream = self._text(self._query(cwd, "rev-parse", "--symbolic-full-name", "@{upstream}"))
        if not upstream.startswith("refs/heads/"):
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        value = self._text(self._query(cwd, "rev-list", "--count", "@{upstream}..HEAD"))
        if not value.isascii() or not value.isdecimal():
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
        return int(value)

    def check_branch_name(self, cwd: Path, branch: str) -> bool:
        # Branch is only passed as a single argv item to Git's validation-only command.
        result = self._execute(cwd, ("check-ref-format", "--branch", branch), allow_exit_codes=frozenset({0, 1}))
        if result.returncode not in {0, 1}:
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        return result.returncode == 0

    def branch_exists(self, cwd: Path, branch: str) -> bool:
        # 固定 refs/heads 前缀，branch 在路径层先生成并校验。
        result = self._execute(
            cwd,
            ("show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
            allow_exit_codes=frozenset({0, 1}),
        )
        return result.returncode == 0

    def _manager_add_worktree(self, cwd: Path, branch: str, path: Path, base_commit: str) -> None:
        from .paths import validate_branch_syntax

        validate_branch_syntax(branch)
        if not Path(path).is_absolute() or not _valid_object_id(base_commit):
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        self._execute(cwd, ("worktree", "add", "-b", branch, str(Path(path)), base_commit))

    def _manager_remove_worktree(self, cwd: Path, path: Path) -> None:
        """仅非 force 删除单个 Manager 已证明归属的 Worktree。"""
        if not Path(path).is_absolute():
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        self._execute(cwd, ("worktree", "remove", str(Path(path))) )

    def _query(self, cwd: Path, *fixed_args: str) -> bytes:
        return self._execute(cwd, fixed_args).stdout

    def _execute(self, cwd: Path, args: Sequence[str], *, allow_exit_codes: frozenset[int] = frozenset({0})):
        try:
            root = Path(cwd).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED) from exc
        if not root.is_dir() or any(not isinstance(item, str) or "\x00" in item for item in args):
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        hooks = _validate_empty_hooks_dir(self._hooks)
        try:
            hooks.relative_to(root)
        except ValueError:
            pass
        else:
            # Child checkout 不得写入 Manager 用作 hooksPath 的目录。
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        argv = [
            self._executable,
            "-c", f"core.hooksPath={self._hooks}",
            "-c", "core.fsmonitor=false",
            "-c", "core.pager=cat",
            "--no-pager", "-C", str(root), *args,
        ]
        env = _controlled_environment(self._base_env)
        try:
            completed = self._runner(
                argv,
                cwd=str(root),
                env=env,
                shell=False,
                timeout=self._timeout,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorktreeError(WorktreeErrorCode.GIT_TIMEOUT) from exc
        except (OSError, ValueError) as exc:
            raise WorktreeError(WorktreeErrorCode.GIT_UNAVAILABLE) from exc
        except Exception as exc:
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED) from exc
        stdout = _as_bytes(getattr(completed, "stdout", b""))
        stderr = _as_bytes(getattr(completed, "stderr", b""))
        if len(stdout) > self._max_output or len(stderr) > self._max_output:
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
        returncode = getattr(completed, "returncode", None)
        if isinstance(returncode, bool) or not isinstance(returncode, int) or returncode not in allow_exit_codes:
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        return _Completed(returncode, stdout)

    @staticmethod
    def _text(value: bytes) -> str:
        try:
            text = value.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID) from exc
        if not text or "\n" in text or "\r" in text or any(ord(char) < 32 for char in text):
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
        return text


class ReadOnlyGitCapability(Protocol):
    """Phase 3 OS/process sandbox must implement this; cwd alone is insufficient."""

    def run_readonly_git(self, *, argv: tuple[str, ...], cwd: Path) -> bytes: ...


class ChildGitReadOnlyGate:
    """子 Agent Git 只读命令门；无已验证隔离 capability 时始终 fail closed。"""

    def __init__(self, *, cwd: Path, capability: ReadOnlyGitCapability | None = None) -> None:
        self._cwd = Path(cwd)
        self._capability = capability

    def execute(self, argv: Sequence[str]) -> bytes:
        checked = _validate_readonly_child_argv(argv)
        if self._capability is None:
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        try:
            return self._capability.run_readonly_git(argv=checked, cwd=self._cwd)
        except WorktreeError:
            raise
        except Exception as exc:
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED) from exc


def _validate_readonly_child_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(argv, (tuple, list)) or not argv or any(not isinstance(arg, str) for arg in argv):
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
    values = tuple(argv)
    if values[0] != "git" or any(not value or "\x00" in value or value.startswith(("-c", "--config-env", "--exec-path")) for value in values[1:]):
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
    if len(values) < 2 or values[1] in {
        "add", "commit", "update-ref", "branch", "checkout", "switch", "reset", "clean",
        "fetch", "push", "config", "worktree", "merge", "rebase", "cherry-pick", "tag",
        "stash", "gc", "repack", "reflog", "symbolic-ref", "replace", "notes",
    }:
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
    command = values[1]
    allowed_options: dict[str, frozenset[str]] = {
        "status": frozenset({"--no-optional-locks", "--short", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"}),
        "diff": frozenset({"--no-ext-diff", "--no-textconv", "--stat", "--name-only", "--name-status"}),
        "log": frozenset({"--oneline", "--decorate=short", "-n", "--max-count"}),
        "show": frozenset({"--stat", "--name-only", "--format=short"}),
        "cat-file": frozenset({"-t", "-s", "-p"}),
        "ls-files": frozenset({"--cached", "--others", "--exclude-standard", "-z"}),
        "rev-parse": frozenset({"--show-toplevel", "--git-common-dir", "--show-prefix", "--verify"}),
    }
    if command not in allowed_options or len(values) > 12:
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
    required_options = {
        "status": frozenset({"--no-optional-locks"}),
        "diff": frozenset({"--no-ext-diff", "--no-textconv"}),
    }
    if not required_options.get(command, frozenset()).issubset(values[2:]):
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
    for argument in values[2:]:
        if argument.startswith("-"):
            option = argument.split("=", 1)[0]
            if option not in allowed_options[command]:
                raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        elif argument == "--" or "/" in argument or "\\" in argument or ":" in argument:
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
    return values


def _validated_repository_relative_path(value: str) -> str:
    from .paths import validate_relative_name

    try:
        parts = validate_relative_name(value, max_segments=16)
    except Exception as exc:
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED) from exc
    normalized = "/".join(parts)
    if normalized != value:
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
    return normalized


@dataclass(frozen=True)
class _Completed:
    returncode: int
    stdout: bytes


def _validate_empty_hooks_dir(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current = current / component
            info = current.lstat()
            attrs = getattr(info, "st_file_attributes", 0)
            if (
                not stat_is_directory(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        mode = path.lstat().st_mode
        if not path.is_dir() or path.is_symlink() or not path.resolve(strict=True).is_absolute():
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        if not stat_is_directory(mode) or any(path.iterdir()):
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED)
        return path.resolve(strict=True)
    except WorktreeError:
        raise
    except OSError as exc:
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED) from exc


def _controlled_environment(source: Mapping[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        value = source.get(key)
        if isinstance(value, str) and value and "\x00" not in value:
            env[key] = value
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_COUNT": "0",
            "GIT_PAGER": "cat",
        }
    )
    return env


def _parse_porcelain_records(output: bytes) -> tuple[str, ...]:
    if not output:
        return ()
    if not output.endswith(b"\n"):
        raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
    lines = output.splitlines()
    records: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                records.append("\n".join(current))
                current = []
            continue
        try:
            decoded = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID) from exc
        if any(ord(char) < 32 and char != "\t" for char in decoded):
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
        current.append(decoded)
    if current:
        records.append("\n".join(current))
    if not records or any(not record.startswith("worktree ") for record in records):
        raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
    return tuple(records)


def _valid_object_id(value: str) -> bool:
    return (len(value) in {40, 64}) and all(char in "0123456789abcdefABCDEF" for char in value)


def _canonical_git_path(value: str) -> Path:
    try:
        path = Path(value).resolve(strict=True)
        if not path.is_absolute():
            raise ValueError
        return path
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID) from exc


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return b""


def stat_is_directory(mode: int) -> bool:
    import stat

    return stat.S_ISDIR(mode)
