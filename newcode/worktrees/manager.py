"""当前进程拥有的 Worktree lease、创建、验证和 fail-closed 回滚。"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import threading
import time
from .git import WorktreeGitAdapter
from .paths import build_worktree_path, validate_agent_slug, validate_branch, validate_task_id
from .types import WorktreeError, WorktreeErrorCode, WorktreeLease, WorktreeRequest, WorktreeState


OWNER_METADATA_DIRNAME = "newcode-worktrees"
OWNER_SCHEMA_VERSION = 1
OWNER_MARKER_MAX_BYTES = 8_192
LOCK_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.025

_TRANSITIONS: dict[WorktreeState, frozenset[WorktreeState]] = {
    WorktreeState.REQUESTED: frozenset({WorktreeState.VALIDATING, WorktreeState.FAILED}),
    WorktreeState.VALIDATING: frozenset({WorktreeState.CREATING, WorktreeState.FAILED}),
    WorktreeState.CREATING: frozenset({WorktreeState.READY, WorktreeState.FAILED, WorktreeState.CLEANUP_FAILED, WorktreeState.PRESERVED}),
    WorktreeState.READY: frozenset({WorktreeState.RUNNING, WorktreeState.FAILED, WorktreeState.CANCELLED, WorktreeState.TIMED_OUT}),
    WorktreeState.RUNNING: frozenset({WorktreeState.COMPLETED, WorktreeState.FAILED, WorktreeState.CANCELLED, WorktreeState.TIMED_OUT}),
    WorktreeState.COMPLETED: frozenset({WorktreeState.CLEANED, WorktreeState.PRESERVED, WorktreeState.CLEANUP_FAILED}),
    WorktreeState.FAILED: frozenset({WorktreeState.CLEANED, WorktreeState.PRESERVED, WorktreeState.CLEANUP_FAILED}),
    WorktreeState.CANCELLED: frozenset({WorktreeState.CLEANED, WorktreeState.PRESERVED, WorktreeState.CLEANUP_FAILED}),
    WorktreeState.TIMED_OUT: frozenset({WorktreeState.CLEANED, WorktreeState.PRESERVED, WorktreeState.CLEANUP_FAILED}),
    WorktreeState.CLEANED: frozenset(),
    WorktreeState.PRESERVED: frozenset(),
    WorktreeState.CLEANUP_FAILED: frozenset(),
}


class WorktreeManager:
    """Git 写操作的唯一 Worktree lifecycle owner；child 仅能获得只读 gate。"""

    def __init__(
        self,
        *,
        empty_hooks_dir: Path,
        git: WorktreeGitAdapter | None = None,
        lock_wait_seconds: float = LOCK_WAIT_SECONDS,
    ) -> None:
        if isinstance(lock_wait_seconds, bool) or not isinstance(lock_wait_seconds, (int, float)) or not 0 < lock_wait_seconds <= 30:
            raise ValueError("worktree_lock_timeout")
        self._git = git or WorktreeGitAdapter(empty_hooks_dir=empty_hooks_dir)
        self._lock_wait = float(lock_wait_seconds)
        self._owner_token = secrets.token_hex(32)
        self._lock = threading.RLock()
        self._leases: dict[str, WorktreeLease] = {}
        self._occupied: set[str] = set()
        self._active_locks: dict[str, _FileLock] = {}
        self._diagnostics: list[str] = []

    @property
    def diagnostics(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._diagnostics)

    def leases(self) -> tuple[WorktreeLease, ...]:
        with self._lock:
            return tuple(self._leases[key] for key in sorted(self._leases))

    def is_tracked_source_path(self, repository_root: Path, relative_path: str) -> bool:
        """固定只读 Git 查询：仅用于区分用户已授权的 tracked 输入文件。"""
        return self._git.is_tracked_path(Path(repository_root), relative_path)

    def is_ignored_source_path(self, repository_root: Path, relative_path: str) -> bool:
        """固定只读 Git 查询：仅用于区分用户已授权的 ignored 输入文件。"""
        return self._git.is_ignored_path(Path(repository_root), relative_path)

    def create(self, request: WorktreeRequest) -> WorktreeLease:
        if not isinstance(request, WorktreeRequest):
            raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
        validate_agent_slug(request.agent_slug)
        validate_task_id(request.task_id)
        if request.task_id in self._leases:
            raise WorktreeError(WorktreeErrorCode.PATH_CONFLICT)
        try:
            repository_root = self._git.repository_root(Path(request.repository))
            common_dir = self._git.common_git_dir(repository_root)
            if self._git.is_bare_repository(repository_root):
                raise WorktreeError(WorktreeErrorCode.REPOSITORY_INVALID)
            # Detached HEAD、缺失/损坏 HEAD 均安全失败，不尝试猜测基线。
            self._git.current_branch(repository_root)
        except WorktreeError:
            raise
        except Exception as exc:
            raise WorktreeError(WorktreeErrorCode.REPOSITORY_INVALID) from exc

        with ExitStack() as stack:
            metadata_root = _ensure_metadata_root(common_dir)
            _ensure_directory_chain(metadata_root / "locks", metadata_root)
            repo_key = hashlib.sha256(str(repository_root).encode("utf-8")).hexdigest()
            task_key = hashlib.sha256(request.task_id.encode("utf-8")).hexdigest()
            stack.enter_context(_FileLock(metadata_root / "locks" / f"repo-{repo_key}.lock", self._lock_wait))
            stack.enter_context(_FileLock(metadata_root / "locks" / f"task-{task_key}.lock", self._lock_wait))
            with self._lock:
                if request.task_id in self._leases or request.task_id in self._occupied:
                    raise WorktreeError(WorktreeErrorCode.PATH_CONFLICT)
            if (
                self._git.repository_root(repository_root) != repository_root
                or self._git.common_git_dir(repository_root) != common_dir
                or self._git.is_bare_repository(repository_root)
            ):
                raise WorktreeError(WorktreeErrorCode.REPOSITORY_INVALID)
            self._git.current_branch(repository_root)
            return self._create_locked(request, repository_root, common_dir, metadata_root)

    def verify(self, lease: WorktreeLease) -> WorktreeLease:
        """只复验当前 Manager 本进程登记且未占用的 READY lease。"""
        if not isinstance(lease, WorktreeLease):
            raise WorktreeError(WorktreeErrorCode.LEASE_INVALID)
        with self._lock:
            registered = self._leases.get(lease.task_id)
            if registered != lease or lease.state is not WorktreeState.READY or lease.task_id in self._occupied:
                raise WorktreeError(WorktreeErrorCode.LEASE_INVALID)
        metadata_root = _metadata_root(lease.common_git_dir)
        repo_key = hashlib.sha256(str(lease.repository_root).encode("utf-8")).hexdigest()
        task_key = hashlib.sha256(lease.task_id.encode("utf-8")).hexdigest()
        try:
            with _FileLock(metadata_root / "locks" / f"repo-{repo_key}.lock", self._lock_wait):
                with _FileLock(metadata_root / "locks" / f"task-{task_key}.lock", self._lock_wait):
                    if not self._verify_registration(lease):
                        raise WorktreeError(WorktreeErrorCode.REGISTRATION_INVALID)
        except WorktreeError:
            raise
        except Exception as exc:
            raise WorktreeError(WorktreeErrorCode.REGISTRATION_INVALID) from exc
        return lease

    def claim(self, lease: WorktreeLease) -> WorktreeLease:
        """当前 Manager 内占用 lease，阻止重复 verify/claim。"""
        with self._lock:
            if self._leases.get(lease.task_id) != lease or lease.state is not WorktreeState.READY or lease.task_id in self._occupied:
                raise WorktreeError(WorktreeErrorCode.LEASE_INVALID)
            active_lock = _FileLock(_task_lock_path(lease), 0.0)
            active_lock.__enter__()
            try:
                self._occupied.add(lease.task_id)
                self._active_locks[lease.task_id] = active_lock
                return self._transition(lease, WorktreeState.RUNNING)
            except Exception:
                self._occupied.discard(lease.task_id)
                self._active_locks.pop(lease.task_id, None)
                active_lock.__exit__(None, None, None)
                raise

    def release(self, lease: WorktreeLease, terminal_state: WorktreeState) -> WorktreeLease:
        """记录 child 终态并释放进程内占用；不清理 Worktree 文件。"""
        if terminal_state not in {WorktreeState.COMPLETED, WorktreeState.FAILED, WorktreeState.CANCELLED, WorktreeState.TIMED_OUT}:
            raise WorktreeError(WorktreeErrorCode.LEASE_INVALID)
        with self._lock:
            if self._leases.get(lease.task_id) != lease or lease.state is not WorktreeState.RUNNING or lease.task_id not in self._occupied:
                raise WorktreeError(WorktreeErrorCode.LEASE_INVALID)
            updated = self._transition(lease, terminal_state)
            self._occupied.discard(lease.task_id)
            active_lock = self._active_locks.pop(lease.task_id, None)
            if active_lock is not None:
                active_lock.__exit__(None, None, None)
            return updated

    def cleanup_stale(
        self,
        repository: Path,
        *,
        now: float | None = None,
        cleanup_after_days: int = 30,
        max_candidates: int = 32,
    ) -> tuple[str, ...]:
        """只清理超过保留期且三层安全门全部通过的本 Manager Worktree。"""
        if isinstance(cleanup_after_days, bool) or not isinstance(cleanup_after_days, int) or cleanup_after_days < 30:
            raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 32:
            raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
        current = time.time() if now is None else now
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
        try:
            repo = self._git.repository_root(Path(repository))
            common = self._git.common_git_dir(repo)
            metadata = _metadata_root(common)
            owners = metadata / "owners"
            _validate_plain_directory(owners, metadata)
            _validate_plain_directory(metadata / "locks", metadata)
            cutoff = float(current) - cleanup_after_days * 24 * 60 * 60
            candidate_paths: list[Path] = []
            for item in owners.iterdir():
                try:
                    is_old = item.lstat().st_mtime < cutoff
                except OSError:
                    is_old = True
                if is_old:
                    candidate_paths.append(item)
                    if len(candidate_paths) >= max_candidates:
                        break
            marker_paths = tuple(candidate_paths)
        except Exception:
            self._diagnostics.append("worktree_state_unknown")
            return ()

        removed: list[str] = []
        for marker in marker_paths:
            try:
                lease = _lease_from_marker(marker, repo, common)
                with self._lock:
                    if lease.task_id in self._leases or lease.task_id in self._occupied:
                        continue
                # Layer 1: canonical in-repository path with no link/reparse component.
                _validate_owned_worktree_path(lease)
                # Layer 2: owner marker and exactly one registration/branch/common-dir match.
                if not self._verify_registration(lease):
                    raise WorktreeError(WorktreeErrorCode.REGISTRATION_INVALID)
                # Layer 3: acquire cross-process locks, then repeat every mutable check.
                repo_lock = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()
                with _FileLock(metadata / "locks" / f"repo-{repo_lock}.lock", 0.0):
                    with _FileLock(_task_lock_path(lease), 0.0):
                        with self._lock:
                            if lease.task_id in self._leases or lease.task_id in self._occupied:
                                continue
                        _validate_owned_worktree_path(lease)
                        if not self._verify_registration(lease):
                            raise WorktreeError(WorktreeErrorCode.REGISTRATION_INVALID)
                        if not self._worktree_is_clean(lease):
                            raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
                        self._git._manager_remove_worktree(repo, lease.path)
                        if lease.path.exists() or _records_for_path(self._git.list_worktrees(repo), lease.path):
                            raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
                        # Delete only this already-validated single owner marker; never delete branches.
                        marker.unlink()
                        removed.append(lease.task_id)
            except Exception:
                self._diagnostics.append("worktree_preserved_changes")
                continue
        return tuple(removed)

    def _worktree_is_clean(self, lease: WorktreeLease) -> bool:
        """Any nonempty/unknown status, ignored entry, ahead commit or unsafe tree preserves."""
        try:
            if self._git.status(lease.path) or self._git.status(lease.path, include_ignored=True):
                return False
            if self._git.ignored_paths(lease.path):
                return False
            if self._git.upstream_ahead_count(lease.path) != 0:
                return False
            _validate_worktree_tree(lease)
            return True
        except Exception:
            return False

    def _create_locked(
        self,
        request: WorktreeRequest,
        repository_root: Path,
        common_dir: Path,
        metadata_root: Path,
    ) -> WorktreeLease:
        head = self._git.head(repository_root)
        branch = f"newcode/subagent/{request.task_id}"
        # Directory builder validates the manager-owned slug/task path before any worktree write.
        generated = build_worktree_path(repository_root, request.agent_slug, request.task_id)
        branch = validate_branch(branch, lambda value: self._git.check_branch_name(repository_root, value))
        if generated.branch != branch:
            raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
        if self._git.branch_exists(repository_root, branch):
            raise WorktreeError(WorktreeErrorCode.PATH_CONFLICT)
        if self._registration_count(repository_root, generated.checkout_path, branch):
            raise WorktreeError(WorktreeErrorCode.PATH_CONFLICT)

        lease = WorktreeLease(
            repository_root=repository_root,
            common_git_dir=common_dir,
            task_id=request.task_id,
            agent_slug=request.agent_slug,
            branch=branch,
            path=generated.checkout_path,
            base_commit=head,
            owner_token=self._owner_token,
            state=WorktreeState.VALIDATING,
        )
        marker = _marker_file(metadata_root, request.task_id)
        marker_created = False
        add_attempted = False
        with self._lock:
            self._leases[request.task_id] = lease
        try:
            lease = self._transition(lease, WorktreeState.CREATING)
            _ensure_checkout_parent(repository_root, generated.worktree_root, request.agent_slug)
            _ensure_directory_chain(metadata_root / "owners", metadata_root)
            _write_owner_marker(marker, _marker_document(lease))
            marker_created = True
            add_attempted = True
            self._git._manager_add_worktree(repository_root, branch, generated.checkout_path, head)
            if not self._verify_registration(lease):
                raise WorktreeError(WorktreeErrorCode.REGISTRATION_INVALID)
            ready = self._transition(lease, WorktreeState.READY)
            return ready
        except Exception as exc:
            rolled_back = False
            if marker_created or add_attempted:
                rolled_back = self._rollback_failed_create(lease, marker, marker_created)
            with self._lock:
                if rolled_back:
                    self._leases.pop(request.task_id, None)
                else:
                    failed_state = WorktreeState.CLEANUP_FAILED if add_attempted else WorktreeState.FAILED
                    self._leases[request.task_id] = replace(lease, state=failed_state)
                    self._diagnostics.append("worktree_cleanup_incomplete")
            if isinstance(exc, WorktreeError):
                raise
            raise WorktreeError(WorktreeErrorCode.GIT_FAILED) from exc

    def _verify_registration(self, lease: WorktreeLease) -> bool:
        marker = _marker_file(_metadata_root(lease.common_git_dir), lease.task_id)
        if not _marker_matches(marker, lease):
            return False
        records = self._git.list_worktrees(lease.repository_root)
        matching = _records_for_path(records, lease.path)
        if len(matching) != 1:
            return False
        fields = _record_fields(matching[0])
        if fields.get("branch") != f"refs/heads/{lease.branch}":
            return False
        branch_matches = sum(
            1 for record in records
            if _record_fields(record).get("branch") == f"refs/heads/{lease.branch}"
        )
        if branch_matches != 1:
            return False
        if self._git.repository_root(lease.path) != lease.path:
            return False
        if self._git.common_git_dir(lease.path) != lease.common_git_dir:
            return False
        if self._git.current_branch(lease.path) != lease.branch:
            return False
        if self._git.head(lease.path) != lease.base_commit:
            return False
        return True

    def _registration_count(self, repo: Path, target: Path, branch: str) -> int:
        records = self._git.list_worktrees(repo)
        return sum(
            1 for record in records
            if _record_path(record) == target.resolve(strict=False)
            or _record_fields(record).get("branch") == f"refs/heads/{branch}"
        )

    def _rollback_failed_create(self, lease: WorktreeLease, marker: Path, marker_created: bool) -> bool:
        """只回滚本 Manager 正创建、身份完整、干净且唯一注册的目标。"""
        try:
            if not marker_created:
                return False
            if not _marker_matches(marker, lease):
                return False
            records = self._git.list_worktrees(lease.repository_root)
            matching = _records_for_path(records, lease.path)
            if not matching:
                # 只有目标、branch 都不存在时才移除我们预先创建的 marker。
                if lease.path.exists() or self._git.branch_exists(lease.repository_root, lease.branch):
                    return False
                marker.unlink()
                return True
            if len(matching) != 1 or not self._verify_registration(lease):
                return False
            if self._git.status(lease.path) or self._git.status(lease.path, include_ignored=True) or self._git.ignored_paths(lease.path):
                return False
            self._git._manager_remove_worktree(lease.repository_root, lease.path)
            if lease.path.exists() or _records_for_path(self._git.list_worktrees(lease.repository_root), lease.path):
                return False
            marker.unlink()
            return True
        except Exception:
            return False

    def _transition(self, lease: WorktreeLease, new_state: WorktreeState) -> WorktreeLease:
        if new_state not in _TRANSITIONS[lease.state]:
            raise WorktreeError(WorktreeErrorCode.LEASE_INVALID)
        updated = replace(lease, state=new_state)
        with self._lock:
            if self._leases.get(lease.task_id) != lease:
                raise WorktreeError(WorktreeErrorCode.LEASE_INVALID)
            self._leases[lease.task_id] = updated
        return updated


class _FileLock:
    """OS advisory lock，锁文件留在 common Git dir 的 NewCode metadata 下。"""

    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self._file = None

    def __enter__(self):
        try:
            parent_info = self.path.parent.lstat()
            attrs = getattr(parent_info, "st_file_attributes", 0)
            if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                attrs = getattr(info, "st_file_attributes", 0)
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)
        except WorktreeError:
            raise
        except OSError as exc:
            raise WorktreeError(WorktreeErrorCode.OWNER_INVALID) from exc
        try:
            self._file = self.path.open("a+b")
        except OSError as exc:
            raise WorktreeError(WorktreeErrorCode.OWNER_INVALID) from exc
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"0")
            self._file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock_once()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise WorktreeError(WorktreeErrorCode.LOCK_TIMEOUT)
                time.sleep(LOCK_POLL_SECONDS)

    def _lock_once(self) -> None:
        assert self._file is not None
        self._file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def _metadata_root(common_git_dir: Path) -> Path:
    return common_git_dir / OWNER_METADATA_DIRNAME


def _ensure_metadata_root(common_git_dir: Path) -> Path:
    root = Path(common_git_dir).resolve(strict=True)
    target = _metadata_root(root)
    _ensure_directory_chain(target, root)
    return target


def _ensure_directory_chain(target: Path, boundary: Path) -> None:
    target = Path(os.path.abspath(target))
    boundary = Path(os.path.abspath(boundary))
    try:
        relative = target.relative_to(boundary)
    except ValueError as exc:
        raise WorktreeError(WorktreeErrorCode.OWNER_INVALID) from exc
    current = boundary
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorktreeError(WorktreeErrorCode.OWNER_INVALID) from exc
        try:
            info = current.lstat()
        except OSError as exc:
            raise WorktreeError(WorktreeErrorCode.OWNER_INVALID) from exc
        attrs = getattr(info, "st_file_attributes", 0)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)


def _ensure_checkout_parent(repository_root: Path, worktree_root: Path, agent_slug: str) -> None:
    _ensure_directory_chain(worktree_root / agent_slug, repository_root)


def _marker_file(metadata_root: Path, task_id: str) -> Path:
    key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return metadata_root / "owners" / f"{key}.json"


def _marker_document(lease: WorktreeLease) -> dict[str, object]:
    return {
        "version": OWNER_SCHEMA_VERSION,
        "owner_token": lease.owner_token,
        "pid": lease.owner_pid,
        "created_at": lease.created_at,
        "repository_identity": _repository_identity(lease.repository_root, lease.common_git_dir),
        "repository_root": str(lease.repository_root),
        "common_git_dir": str(lease.common_git_dir),
        "task_id": lease.task_id,
        "agent_slug": lease.agent_slug,
        "branch": lease.branch,
        "worktree_path": str(lease.path),
        "base_commit": lease.base_commit,
    }


def _write_owner_marker(path: Path, document: dict[str, object]) -> None:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > OWNER_MARKER_MAX_BYTES:
        raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError as exc:
        raise WorktreeError(WorktreeErrorCode.PATH_CONFLICT) from exc
    except OSError as exc:
        raise WorktreeError(WorktreeErrorCode.OWNER_INVALID) from exc


def _marker_matches(path: Path, lease: WorktreeLease) -> bool:
    try:
        info = path.lstat()
        attrs = getattr(info, "st_file_attributes", 0)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return False
        if info.st_size > OWNER_MARKER_MAX_BYTES:
            return False
        document = json.loads(path.read_text(encoding="utf-8"))
        expected = _marker_document(lease)
        return isinstance(document, dict) and document == expected
    except Exception:
        return False


def _task_lock_path(lease: WorktreeLease) -> Path:
    key = hashlib.sha256(lease.task_id.encode("utf-8")).hexdigest()
    return _metadata_root(lease.common_git_dir) / "locks" / f"task-{key}.lock"


def _lease_from_marker(marker: Path, repository_root: Path, common_git_dir: Path) -> WorktreeLease:
    info = marker.lstat()
    attrs = getattr(info, "st_file_attributes", 0)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or info.st_size > OWNER_MARKER_MAX_BYTES:
        raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)
    document = json.loads(marker.read_text(encoding="utf-8"))
    expected = {
        "version", "owner_token", "pid", "created_at", "repository_identity", "repository_root",
        "common_git_dir", "task_id", "agent_slug", "branch", "worktree_path", "base_commit",
    }
    if not isinstance(document, dict) or set(document) != expected:
        raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)
    task_id = document["task_id"]
    agent_slug = document["agent_slug"]
    from .paths import validate_agent_slug, validate_task_id

    validate_task_id(task_id)
    validate_agent_slug(agent_slug)
    if marker.name != f"{hashlib.sha256(task_id.encode('utf-8')).hexdigest()}.json":
        raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)
    if (
        document["version"] != OWNER_SCHEMA_VERSION
        or not isinstance(document["owner_token"], str)
        or len(document["owner_token"]) != 64
        or not isinstance(document["pid"], int)
        or isinstance(document["pid"], bool)
        or document["pid"] <= 0
        or not isinstance(document["created_at"], (int, float))
        or isinstance(document["created_at"], bool)
        or document["repository_identity"] != _repository_identity(repository_root, common_git_dir)
        or document["repository_root"] != str(repository_root)
        or document["common_git_dir"] != str(common_git_dir)
        or document["branch"] != f"newcode/subagent/{task_id}"
        or not isinstance(document["base_commit"], str)
        or len(document["base_commit"]) not in {40, 64}
        or any(char not in "0123456789abcdefABCDEF" for char in document["base_commit"])
    ):
        raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)
    expected_path = repository_root / ".newcode" / "worktrees" / agent_slug / task_id
    if document["worktree_path"] != str(expected_path):
        raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)
    return WorktreeLease(
        repository_root=repository_root,
        common_git_dir=common_git_dir,
        task_id=task_id,
        agent_slug=agent_slug,
        branch=document["branch"],
        path=expected_path,
        base_commit=document["base_commit"],
        owner_token=document["owner_token"],
        created_at=float(document["created_at"]),
        owner_pid=document["pid"],
        state=WorktreeState.COMPLETED,
    )


def _validate_plain_directory(path: Path, boundary: Path) -> None:
    try:
        path.absolute().relative_to(boundary.absolute())
        current = Path(path.anchor)
        for part in path.absolute().parts[1:]:
            current = current / part
            info = current.lstat()
            attrs = getattr(info, "st_file_attributes", 0)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise WorktreeError(WorktreeErrorCode.OWNER_INVALID)
    except WorktreeError:
        raise
    except Exception as exc:
        raise WorktreeError(WorktreeErrorCode.OWNER_INVALID) from exc


def _validate_owned_worktree_path(lease: WorktreeLease) -> None:
    try:
        repo = lease.repository_root.resolve(strict=True)
        target = lease.path.resolve(strict=True)
        expected = repo / ".newcode" / "worktrees" / lease.agent_slug / lease.task_id
        if repo != lease.repository_root or target != expected or not target.is_dir():
            raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
        current = repo
        for part in (".newcode", "worktrees", lease.agent_slug, lease.task_id):
            current = current / part
            info = current.lstat()
            attrs = getattr(info, "st_file_attributes", 0)
            if stat.S_ISLNK(info.st_mode) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
        target.relative_to(repo / ".newcode" / "worktrees")
    except WorktreeError:
        raise
    except Exception as exc:
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID) from exc


def _validate_worktree_tree(lease: WorktreeLease) -> None:
    """完整 no-follow 遍历：仅根 `.git` 普通 pointer file 是结构性例外。"""
    root = lease.path

    def walk_error(_error):
        raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)

    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False, onerror=walk_error):
        base = Path(directory)
        for name in tuple(dirnames):
            path = base / name
            info = path.lstat()
            attrs = getattr(info, "st_file_attributes", 0)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
        for name in filenames:
            path = base / name
            info = path.lstat()
            attrs = getattr(info, "st_file_attributes", 0)
            if stat.S_ISLNK(info.st_mode) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or info.st_nlink != 1:
                raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
            if path == root / ".git":
                if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                    raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
                raw = path.read_text(encoding="utf-8").strip()
                prefix, separator, value = raw.partition(": ")
                if prefix != "gitdir" or not separator or not value:
                    raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
                admin = Path(value)
                if not admin.is_absolute():
                    admin = (root / admin).resolve(strict=True)
                else:
                    admin = admin.resolve(strict=True)
                allowed_admin_root = lease.common_git_dir / "worktrees"
                admin.relative_to(allowed_admin_root.resolve(strict=True))
            elif not stat.S_ISREG(info.st_mode):
                raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)


def _repository_identity(repository_root: Path, common_git_dir: Path) -> str:
    return hashlib.sha256(f"{repository_root}\0{common_git_dir}".encode("utf-8")).hexdigest()


def _record_fields(record: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in record.splitlines():
        key, separator, value = line.partition(" ")
        if not separator or key in fields or any(ord(char) < 32 for char in value):
            raise WorktreeError(WorktreeErrorCode.GIT_OUTPUT_INVALID)
        fields[key] = value
    return fields


def _record_path(record: str) -> Path | None:
    value = _record_fields(record).get("worktree")
    if not value:
        return None
    try:
        return Path(value).resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _records_for_path(records: tuple[str, ...], path: Path) -> tuple[str, ...]:
    target = path.resolve(strict=False)
    return tuple(record for record in records if _record_path(record) == target)
