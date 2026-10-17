"""Team 集成：只读 preview 与日志驱动的固定 B→S、M→I 算法。"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
import ctypes
import hashlib
import os
from pathlib import Path
import re
import stat
import threading

from newcode.permissions.types import PermissionDecisionValue
from newcode.worktrees.git import _integration_relative_path, _valid_object_id
from newcode.worktrees.manager import OWNER_METADATA_DIRNAME, _task_lock_path
from newcode.worktrees.types import WorktreeError

from .store import TeamStore, encode, sanitize
from .paths import _win, reject_links
from .tools import validate_arguments
from .types import TeamError, TeamManifest, TeamRole, MemberState, IntegrationRecord, RecordChange, new_id


MAX_FILE_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 16 * MAX_FILE_BYTES
MAX_PATHS = 4096
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PRIVATE_PARTS = frozenset({".git", ".newcode", ".env", "sessions", "memory", "context-artifacts",
    ".venv", "__pycache__", ".pytest_cache", "credentials", "secrets", ".ssh", ".aws"})
_PRIVATE_NAME = re.compile(r"(?i)(secret|credential|api[_-]?key|password|private[_-]?key|(?:^|[._-])tokens?(?:[._-]|$))")
_CREDENTIAL = re.compile(rb"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:api[_-]?key|password|secret|token|authorization|cookie)\s*[\"']?\s*[:=]|https?://[^\s/@]+:[^\s/@]+@|\bBearer\s+[A-Za-z0-9])")


class IntegrationBarrier:
    """跟踪实际 callback 生命周期，取消标签不能替代执行者静止证明。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self._frozen = False
        self.http_transport = None

    def wrap(self, callback):
        def execute(*args, **kwargs):
            with self._lock:
                if self._frozen:
                    raise TeamError("team_member_busy")
                self._active += 1
            try:
                return callback(*args, **kwargs)
            finally:
                with self._lock:
                    self._active -= 1
        return execute

    @contextmanager
    def hold(self):
        http_lock = None
        with self._lock:
            if self._active or self._frozen:
                raise TeamError("team_member_busy")
            self._frozen = True
        try:
            # HTTP 的调用超时不代表 transport 内部线程已退出。
            if self.http_transport is not None:
                http_lock = self.http_transport._active_request
                if not http_lock.acquire(blocking=False):
                    http_lock = None
                    raise TeamError("team_member_busy")
            yield
        finally:
            if http_lock is not None:
                http_lock.release()
            with self._lock:
                self._frozen = False


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    base_object_id: str | None
    staged_object_id: str | None
    mode: str | None
    staged_mode: str | None
    sha256: str | None
    binary: bool
    content: bytes | None = field(repr=False)

    def manifest(self):
        return {"path": self.path, "base_object_id": self.base_object_id,
            "staged_object_id": self.staged_object_id, "staged_mode": self.staged_mode,
            "mode": self.mode, "sha256": self.sha256, "binary": self.binary,
            "size": len(self.content) if self.content is not None else 0}


@dataclass(frozen=True)
class IntegrationPreview:
    base_commit: str
    target_branch: str = field(repr=False)
    target_head: str
    snapshot_digest: str
    files: tuple[SnapshotFile, ...] = field(repr=False)
    total_bytes: int
    no_code_changes: bool
    verification: str = "not_verified"
    _summary_branch: str = field(default="[REDACTED]", repr=False)
    _target_signature: str = field(default="", repr=False)

    def summary(self):
        # 只输出计数与基准；不把源码、秘密、路径或内部对象送给模型。
        return {"base_commit": self.base_commit, "target_branch": self._summary_branch,
            "target_head": self.target_head, "snapshot_digest": self.snapshot_digest,
            "file_count": len(self.files), "total_bytes": self.total_bytes,
            "no_code_changes": self.no_code_changes, "verification": self.verification}


class _ReadOnlyStore(TeamStore):
    """调用方已持有真实 store lock；重放日志只构造内存 view，不修复投影。"""

    def _project(self, team_id, view):
        pass

    def lock(self, team_id, *, timeout=1.0):
        return nullcontext()


class TeamIntegrationManager:
    def __init__(self, store, team_id, *, worktrees, runners, role, authorize, lead_barrier,
                 confirm_single_writer=None, fault=None):
        self.store, self.team_id, self.worktrees = store, team_id, worktrees
        self.git = worktrees._git
        # 只有当前可信运行时的 runner 可提供静止证明，未知/失联执行者拒绝。
        self.runners = dict(runners)
        self.role, self.authorize, self.lead_barrier = role, authorize, lead_barrier
        self._thread = threading.get_ident()
        self.confirm_single_writer = confirm_single_writer
        self.fault = fault or (lambda point: None)

    def integrate(self, **arguments):
        if not arguments:
            raise TeamError("team_coordinator_unavailable")
        arguments = {"operation": "integrate", **arguments}
        self._authorize(arguments)
        records = self.store.load_records(self.team_id, "integrations")
        prior = next((IntegrationRecord.from_dict(r) for r in records
                      if r["member_id"] == arguments["target_member_id"] and r["idempotency_key"] == arguments["idempotency_key"]), None)
        if prior is not None:
            if (prior.member_revision != arguments["expected_member_revision"]
                    or prior.target_head != arguments["expected_target_head"] or prior.snapshot_digest != arguments["snapshot_digest"]):
                raise TeamError("team_idempotency_conflict")
            actual_revision = prior.member_revision + (2 if prior.stage == "completed" else 1)
            return self._preview(prior.member_id, actual_revision,
                resume=lambda store, view, member, lease: self._resume(prior, view, member, lease),
                allow_integrated=prior.stage == "completed")
        if any(r["stage"] != "completed" for r in records):
            raise TeamError("team_integration_pending")
        return self._preview(arguments["target_member_id"], arguments["expected_member_revision"],
            continuation=lambda snapshot, view, lease: self._begin(snapshot, view, lease, arguments))

    def _authorize(self, arguments):
        validate_arguments("team_integrate", arguments, self.role)
        if threading.get_ident() != self._thread or self.role != "lead":
            raise TeamError("team_role_denied")
        try:
            allowed = callable(self.authorize) and self.authorize(dict(arguments)) is PermissionDecisionValue.ALLOW
        except Exception:
            allowed = False
        if not allowed:
            raise TeamError("permission_denied")
        if not callable(self.lead_barrier):
            raise TeamError("team_member_busy")

    def preview(self, target_member_id, expected_member_revision):
        arguments = {"operation": "preview", "target_member_id": target_member_id,
                     "expected_member_revision": expected_member_revision}
        self._authorize(arguments)
        try:
            return self._preview(target_member_id, expected_member_revision)
        except TeamError:
            raise
        except (WorktreeError, OSError, ValueError, RuntimeError, KeyError, TypeError):
            raise TeamError("team_snapshot_invalid") from None

    def _preview(self, member_id, revision, *, continuation=None, resume=None, allow_integrated=False):
        store = _ReadOnlyStore(self.store.paths, sensitive_values=self.store.sensitive_values)
        root, common = store.paths.workspace_root, store.paths.common_git_dir
        repo_key = hashlib.sha256(str(root).encode()).hexdigest()
        # 所有锁只打开已有普通文件，不创建文件、不改 owner 元数据或 journal。
        with ExitStack() as stack:
            stack.enter_context(self.lead_barrier())
            stack.enter_context(store.paths.guard(self.team_id))
            stack.enter_context(_existing_lock(store.paths.checked(self.team_id, "locks", "store.lock"), boundary=store.paths.root))
            if not self.worktrees._lock.acquire(blocking=False):
                raise TeamError("team_member_busy")
            stack.callback(self.worktrees._lock.release)
            stack.enter_context(_existing_lock(common / OWNER_METADATA_DIRNAME / "locks" / f"repo-{repo_key}.lock", boundary=common))
            view, _, _ = store._recover_locked(self.team_id)
            team = TeamManifest.from_dict(view["team"][self.team_id])
            if team.state.value not in {"ready", "active"}:
                raise TeamError("team_state_invalid")
            members = {m.member_id: m for m in team.roster if m.role is TeamRole.MEMBER}
            target = members.get(member_id)
            if target is None:
                raise TeamError("team_member_not_found")
            if target.revision != revision:
                raise TeamError("team_revision_conflict")
            if target.state is not MemberState.IDLE and not (allow_integrated and target.state is MemberState.INTEGRATED):
                raise TeamError("team_member_busy")
            for name, member in sorted(members.items()):
                if member.state is MemberState.INTEGRATED and name != member_id:
                    continue
                runner = self.runners.get(name)
                if runner is None or runner._owner is None:
                    if resume is None:
                        raise TeamError("team_member_busy")
                    stack.enter_context(_existing_lock(store.paths.checked(self.team_id, "locks", f"executor-{name}.lock"), boundary=store.paths.root))
                    candidate = self.worktrees.integration_member(store, self.team_id, member)
                    stack.enter_context(_existing_lock(_task_lock_path(candidate), boundary=common))
                else:
                    if (runner.store is not self.store or runner.scope.team_id != self.team_id
                            or runner.scope.member_id != name or runner.scope.generation != member.runtime_generation):
                        raise TeamError("team_member_busy")
                    scope = stack.enter_context(runner.quiescence())
                    if scope != runner.scope or runner._lease is None:
                        raise TeamError("team_member_busy")
                    try:
                        if member.state is MemberState.INTEGRATED:
                            candidate = self.worktrees.integration_member(store, self.team_id, member)
                        else:
                            self.worktrees.verify_team_member(store, scope, runner._lease)
                            candidate = runner._lease
                    except (WorktreeError, TeamError):
                        raise TeamError("team_worktree_owner_invalid") from None
                if name == member_id:
                    lease = candidate
            if target.base_commit != lease.base_commit:
                raise TeamError("team_worktree_owner_invalid")
            # 先拒绝可执行的属性驱动配置，再做 status/diff，避免检查本身执行代码。
            for cwd in (root, lease.path):
                if self.git.integration_config(cwd):
                    raise TeamError("team_snapshot_invalid")
            if resume is not None:
                return resume(store, view, target, lease)
            baseline = self._target(root, common)
            branch, head = baseline[:2]
            base = target.base_commit
            self.git.integration_tree(lease.path, base)  # 验证 B 存在且为 tree-ish。
            if self.git.head(lease.path) != base or not self.git.integration_ancestor(root, base, head):
                raise TeamError("team_worktree_owner_invalid")
            first = self._source(lease.path, common, base, baseline[4])
            second = self._source(lease.path, common, base, baseline[4])
            if first != second:
                raise TeamError("team_snapshot_invalid")
            if baseline != self._target(root, common):
                raise TeamError("team_target_changed")
            # 重新确认 owner/B/branch/registration；不依赖扫描前的单次验证。
            self.worktrees.verify_team_member(store, self.runners[member_id].scope, lease)
            files, source_signature, no_changes = first
            document = {"team": self.team_id, "workspace": store.paths.workspace_fingerprint,
                "member": member_id, "revision": revision, "generation": target.runtime_generation,
                "B": base, "T": branch, "M": head, "target": baseline[2],
                "index": source_signature[0], "files": [f.manifest() for f in files]}
            snapshot = IntegrationPreview(base, branch, head, hashlib.sha256(encode(document)).hexdigest(),
                files, sum(len(f.content or b"") for f in files), no_changes,
                _summary_branch=sanitize(branch, store.sensitive_values), _target_signature=baseline[2])
            return snapshot if continuation is None else continuation(snapshot, view, lease)

    def _save(self, record, *, member=None):
        """唯一持久事实仍是 Team transaction；投影/快照不是阶段日志。"""
        view, _, _ = self.store._recover_locked(self.team_id)
        changes = [RecordChange("integrations", record.integration_id, record.to_dict())]
        if member is not None:
            changes.append(RecordChange("members", member.member_id, member.to_dict()))
        self.fault("before_journal_" + record.stage)
        self.store._commit_locked(self.team_id, view["team"][self.team_id]["revision"], new_id("mutation"), tuple(changes), None)
        self.fault("after_journal_" + record.stage)
        return record

    def _step(self, record, stage, **values):
        return self._save(replace(record, stage=stage, revision=record.revision + 1, **values))

    def _confirm(self, record):
        bound = {"integration_id": record.integration_id, "B": record.base_commit,
                 "T": record.target_branch, "M": record.target_head, "snapshot_digest": record.snapshot_digest}
        try:
            allowed = callable(self.confirm_single_writer) and self.confirm_single_writer(bound) is True
        except Exception:
            allowed = False
        if not allowed:
            raise TeamError("team_single_writer_required")

    def _begin(self, snapshot, view, lease, arguments):
        if snapshot.target_head != arguments["expected_target_head"]:
            raise TeamError("team_target_changed")
        if snapshot.snapshot_digest != arguments["snapshot_digest"]:
            raise TeamError("team_snapshot_invalid")
        if snapshot.no_code_changes:
            return {**snapshot.summary(), "stage": "no_code_changes"}
        member = next(m for m in TeamManifest.from_dict(view["team"][self.team_id]).roster if m.member_id == arguments["target_member_id"])
        record = IntegrationRecord(new_id("integration"), self.team_id, member.member_id, member.revision,
            member.runtime_generation, snapshot.base_commit, snapshot.target_head, snapshot.target_branch,
            snapshot.snapshot_digest, arguments["idempotency_key"], snapshot._target_signature,
            tuple(f.manifest() for f in snapshot.files))
        self._confirm(record)
        self._check_target(record)
        current = self._source(lease.path, lease.common_git_dir, record.base_commit,
                               self._target(lease.repository_root, lease.common_git_dir)[4])[0]
        if current != snapshot.files:
            raise TeamError("team_snapshot_invalid")
        # 同一事务关闭 auto_wake；崩溃后旧队列也不能启动成员。
        self._save(record, member=replace(member, auto_wake=False, revision=member.revision + 1))
        parts = (self.team_id, "integrations", record.integration_id, "blobs")
        self.store.paths.mkdir(*parts)
        for item in snapshot.files:
            if item.content is not None:
                blob = (*parts, item.sha256 + ".bin")
                if self.store.paths.checked(*blob, missing=True).exists():
                    if self.store.read_bytes(blob, maximum=MAX_FILE_BYTES) != item.content:
                        raise TeamError("team_snapshot_invalid")
                else:
                    self.store.atomic_write(blob, item.content, replace=False)
        return self._run(record, lease)

    def _resume(self, record, view, member, lease):
        record = IntegrationRecord.from_dict(view["integrations"][record.integration_id])
        if (member.runtime_generation != record.generation or member.base_commit != record.base_commit
                or member.revision != record.member_revision + (2 if record.stage == "completed" else 1)):
            raise TeamError("team_revision_conflict")
        self._frozen(record)
        if record.stage in {"outcome_unknown", "conflict"}:
            # 只读查看真实资源；终态不自动重试或变更日志。
            self._observe(record)
            raise TeamError("team_integration_outcome_unknown" if record.stage == "outcome_unknown" else "team_integration_conflict")
        if record.stage == "completed":
            if not self._applied(record):
                raise TeamError("team_integration_outcome_unknown")
            return self._result(record)
        self._confirm(record)
        # 在 applying_target 之前恢复也要重验成员当前内容/阶段 index，
        # 绝不重新用可变 source 生成 commit。
        if record.stage not in {"applying_target", "safe_not_applied"}:
            self._check_member_source(record, lease)
        return self._run(record, lease)

    def _owned_paths(self, record):
        suffix = record.integration_id.removeprefix("integration-")
        return tuple(f".newcode/worktrees/integration/i{suffix}{kind}" for kind in ("s", "m"))

    def _check_target(self, record):
        root, common = self.store.paths.workspace_root, self.store.paths.common_git_dir
        target = self._target(root, common, ignore_owned=self._owned_paths(record))
        if target[:3] != (record.target_branch, record.target_head, record.target_signature):
            raise TeamError("team_target_changed")
        return target

    def _frozen(self, record):
        files = []
        for item in record.manifest:
            _safe_source_path(item["path"])
            data = None
            if item["sha256"] is not None:
                data = self.store.read_bytes((self.team_id, "integrations", record.integration_id, "blobs", item["sha256"] + ".bin"), maximum=MAX_FILE_BYTES)
                if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"] or _secret(data, self.store.sensitive_values):
                    raise TeamError("team_snapshot_invalid")
            files.append(SnapshotFile(item["path"], item["base_object_id"], item["staged_object_id"],
                item["mode"], item["staged_mode"], item["sha256"], item["binary"], data))
        return tuple(files)

    def _expected_source(self, record):
        entries = _entries(self.git.integration_tree(self.store.paths.workspace_root, record.base_commit))
        for item in self._frozen(record):
            if item.content is None:
                entries.pop(item.path, None)
            else:
                algorithm = hashlib.sha256 if len(record.base_commit) == 64 else hashlib.sha1
                oid = algorithm(b"blob " + str(len(item.content)).encode() + b"\0" + item.content).hexdigest()
                entries[item.path] = (item.mode, oid)
        return entries

    def _check_member_source(self, record, lease):
        target_ignored = self._target(self.store.paths.workspace_root, self.store.paths.common_git_dir,
                                      ignore_owned=self._owned_paths(record))[4]
        current = self._source(lease.path, lease.common_git_dir, record.base_commit, target_ignored)[0]
        if current != self._frozen(record):
            raise TeamError("team_snapshot_invalid")

    def _resource(self, record, kind, *, create=False):
        base = record.base_commit if kind == "source" else record.target_head
        return self.worktrees.integration_resource(self.store.paths.workspace_root, record.integration_id, kind, base, create=create)

    def _verify_commit(self, record, resource, kind, commit):
        parents, tree, message = self.git.integration_commit_info(resource.path, commit)
        expected_parents = (record.base_commit,) if kind == "source" else (record.target_head, record.source_commit)
        expected_tree = record.source_tree if kind == "source" else record.merge_tree
        if parents != expected_parents or tree != expected_tree or message != f"NewCode {record.integration_id} {kind}":
            raise TeamError("team_integration_outcome_unknown")
        if kind == "source" and _entries(self.git.integration_tree(resource.path, commit)) != self._expected_source(record):
            raise TeamError("team_snapshot_invalid")
        if self.git.status(resource.path):
            raise TeamError("team_integration_outcome_unknown")
        return commit

    def _run(self, record, member_lease):
        try:
            return self._run_stages(record, member_lease)
        except TeamError:
            raise
        except Exception:
            # 任何 Git/日志错误都不能引发盲重试；下一次显式请求重新读日志和 Git。
            raise TeamError("team_integration_pending") from None

    def _run_stages(self, record, member_lease):
        root = self.store.paths.workspace_root
        files = self._frozen(record)
        if record.stage in {"applying_target", "safe_not_applied"}:
            if self._applied(record):
                return self._finish(record)
            try:
                self._check_target(record)
            except Exception:
                self._step(record, "outcome_unknown")
                raise TeamError("team_integration_outcome_unknown") from None
            if record.stage == "applying_target":
                self._step(record, "safe_not_applied")
                raise TeamError("team_single_writer_required")
        else:
            self._check_target(record)
        if record.stage == "frozen":
            source = self._resource(record, "source", create=True)
            if self.git.head(source.path) != record.base_commit:
                raise TeamError("team_integration_outcome_unknown")
            # 仅首次准备时写临时目录；已有非基准 index/工作树不覆盖。
            if self.git.status(source.path):
                self._step(record, "outcome_unknown")
                raise TeamError("team_integration_outcome_unknown")
            for item in files:
                target = source.path / item.path
                with _directory_guard(target.parent, boundary=source.path, missing=True):
                    if item.content is None:
                        data, _, _ = _read_file(source.path, item.path, MAX_FILE_BYTES, missing=True)
                        if data is None:
                            raise TeamError("team_snapshot_invalid")
                        target.unlink()
                    else:
                        _safe_source_path(item.path)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        reject_links(target, missing=True)
                        if target.exists():
                            _plain(target.lstat())
                        with target.open("wb") as output:
                            output.write(item.content)
                            output.flush()
                            os.fsync(output.fileno())
                        if os.name != "nt":
                            target.chmod(0o755 if item.mode == "100755" else 0o644)
            self.fault("before_source_stage")
            tree = self.git._integration_stage(source.path, [f.path for f in files])
            if _entries(self.git.integration_index(source.path), index=True) != self._expected_source(record):
                raise TeamError("team_snapshot_invalid")
            record = self._step(record, "source_preparing", source_tree=tree)
        source = self._resource(record, "source")
        if record.stage == "source_preparing":
            head = self.git.head(source.path)
            if head == record.base_commit:
                if (_entries(self.git.integration_index(source.path), index=True)
                        != _entries(self.git.integration_tree(source.path, record.source_tree))):
                    raise TeamError("team_integration_outcome_unknown")
                self.fault("before_source_commit")
                self.git._integration_commit(source.path, record.integration_id, "source")
                self.fault("after_source_commit")
                head = self.git.head(source.path)
            self._verify_commit(record, source, "source", head)
            record = self._step(record, "source_committed", source_commit=head)
        self._verify_commit(record, source, "source", record.source_commit)
        if record.stage == "source_committed":
            self._check_target(record)
            self._resource(record, "merge", create=True)
            record = self._step(record, "merge_preparing")
        merge = self._resource(record, "merge")
        if record.stage == "merge_preparing":
            head = self.git.head(merge.path)
            if head == record.target_head:
                if record.merge_tree is None:
                    if self.git.status(merge.path):
                        self._step(record, "outcome_unknown")
                        raise TeamError("team_integration_outcome_unknown")
                    self.fault("before_merge")
                    if not self.git._integration_merge(merge.path, record.source_commit):
                        self._step(record, "conflict")
                        raise TeamError("team_integration_conflict")
                    self.fault("after_merge")
                    tree = self.git.integration_index_tree(merge.path)
                    record = self._step(record, "merge_preparing", merge_tree=tree)
                if (_entries(self.git.integration_index(merge.path), index=True)
                        != _entries(self.git.integration_tree(merge.path, record.merge_tree))):
                    raise TeamError("team_integration_outcome_unknown")
                self.fault("before_merge_commit")
                self.git._integration_commit(merge.path, record.integration_id, "merge")
                self.fault("after_merge_commit")
                head = self.git.head(merge.path)
            self._verify_commit(record, merge, "merge", head)
            record = self._step(record, "merged", integrated_commit=head)
        self._verify_commit(record, merge, "merge", record.integrated_commit)
        self._check_target(record)
        self._check_member_source(record, member_lease)
        if record.stage == "merged":
            record = self._step(record, "applying_target")
        elif record.stage == "safe_not_applied":
            record = self._step(record, "applying_target")
        self._check_target(record)
        self.fault("before_target_ff")
        try:
            self.git._integration_ff(root, record.integrated_commit)
        except Exception:
            if not self._applied(record):
                try:
                    self._check_target(record)
                except Exception:
                    self._step(record, "outcome_unknown")
                    raise TeamError("team_integration_outcome_unknown") from None
                self._step(record, "safe_not_applied")
                raise TeamError("team_single_writer_required") from None
        self.fault("after_target_ff")
        if not self._applied(record):
            self._step(record, "outcome_unknown")
            raise TeamError("team_integration_outcome_unknown")
        return self._finish(record)

    def _observe(self, record):
        # 无 Git 写；即使未知/冲突终态也不能只相信 JSON。
        root, common = self.store.paths.workspace_root, self.store.paths.common_git_dir
        self._target(root, common, ignore_owned=self._owned_paths(record))
        for kind in ("source", "merge"):
            try:
                self._resource(record, kind)
            except WorktreeError:
                pass

    def _applied(self, record):
        try:
            if record.integrated_commit is None:
                return False
            root, common = self.store.paths.workspace_root, self.store.paths.common_git_dir
            target = self._target(root, common, ignore_owned=self._owned_paths(record))
            if target[:2] != (record.target_branch, record.integrated_commit):
                return False
            entries = _entries(self.git.integration_tree(root, record.integrated_commit))
            if _entries(target[3], index=True) != entries:
                return False
            for path, (mode, oid) in entries.items():
                with _directory_guard(root / Path(path).parent, boundary=root):
                    actual, _, actual_mode = _read_file_locked(root, path, None, hash_only=True,
                        git_blob_algorithm="sha256" if len(oid) == 64 else "sha1")
                if actual != oid or os.name != "nt" and actual_mode != mode:
                    return False
            source, merge = self._resource(record, "source"), self._resource(record, "merge")
            self._verify_commit(record, source, "source", record.source_commit)
            self._verify_commit(record, merge, "merge", record.integrated_commit)
            return True
        except Exception:
            return False

    def _finish(self, record):
        view, _, _ = self.store._recover_locked(self.team_id)
        from .types import TeamMemberRecord
        member = TeamMemberRecord.from_dict(view["members"][record.member_id])
        if record.stage == "completed":
            return self._result(record)
        completed = replace(record, stage="completed", revision=record.revision + 1)
        self._save(completed, member=replace(member, state=MemberState.INTEGRATED, auto_wake=False, revision=member.revision + 1))
        return self._result(completed)

    @staticmethod
    def _result(record):
        return {"integration_id": record.integration_id, "stage": record.stage,
            "B": record.base_commit, "M": record.target_head, "S": record.source_commit,
            "I": record.integrated_commit, "verification": "not_verified"}

    def _target(self, root, common, *, ignore_owned=()):
        if self.git.repository_root(root) != root or self.git.common_git_dir(root) != common:
            raise TeamError("team_worktree_owner_invalid")
        branch, head = self.git.current_branch(root), self.git.head(root)
        index_raw = self.git.integration_index(root)
        index = _entries(index_raw, index=True)
        _attributes(self.git.integration_attributes(root, sorted(index)))
        if self.git.status(root):
            raise TeamError("team_target_dirty")
        ignored = _paths(self.git.ignored_paths(root), directories=True)
        ignored = tuple(p for p in ignored if not any(p == owned or p.startswith(owned + "/") for owned in ignore_owned))
        index_signature = self._index_signature(root, common)
        # 目标内容独立复核，不只相信 Git stat cache；不读取 ignored/secret 正文。
        signature = []
        for path in sorted(index):
            with _directory_guard(root / Path(path).parent, boundary=root):
                digest, identity, _ = _read_file_locked(root, path, None, hash_only=True)
            signature.append((path, identity, digest))
        digest = hashlib.sha256(encode([index_raw.hex(), index_signature, signature, ignored])).hexdigest()
        return branch, head, digest, index_raw, ignored

    def _index_signature(self, cwd, common):
        path = self.git.integration_index_path(cwd)
        # 不能先 resolve 后漏掉 index 自身的 symlink/reparse point。
        reject_links(path)
        canonical = path.resolve(strict=True)
        if not canonical.is_relative_to(common) or canonical.name != "index":
            raise TeamError("team_worktree_owner_invalid")
        # 不抢占外部 Git 正在进行的 index/ref/merge 操作；只读保留。
        for directory in {canonical.parent, common}:
            for name in ("index.lock", "HEAD.lock", "packed-refs.lock", "config.lock",
                         "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
                pending = directory / name
                if pending.exists() or pending.is_symlink():
                    raise TeamError("team_target_changed")
        data, identity, _ = _read_file(common, canonical.relative_to(common).as_posix(), MAX_FILE_BYTES)
        return hashlib.sha256(data).hexdigest(), identity

    def _source(self, root, common, base, target_ignored):
        tree_raw = self.git.integration_tree(root, base)
        index_raw = self.git.integration_index(root)
        tree, index = _entries(tree_raw), _entries(index_raw, index=True)
        untracked = _paths(self.git.integration_untracked(root))
        ignored = _paths(self.git.ignored_paths(root), directories=True)
        all_paths = sorted(set(tree) | set(index) | set(untracked))
        if len(all_paths) > MAX_PATHS or len({p.casefold() for p in all_paths}) != len(all_paths):
            raise TeamError("team_snapshot_invalid")
        _attributes(self.git.integration_attributes(root, all_paths))
        staged, working = self.git.integration_changes(root, base)
        changes = sorted(set(_paths(staged)) | set(_paths(working)) | set(untracked))
        if self.git.integration_ignored_changes(root, changes):
            raise TeamError("team_snapshot_invalid")
        files, signatures, total, no_changes = [], [], 0, True
        index_signature = self._index_signature(root, common)
        for path in changes:
            _safe_source_path(path)
            if any(path == ignored_path or path.startswith(ignored_path.rstrip("/") + "/")
                   or ignored_path.startswith(path + "/") for ignored_path in target_ignored):
                raise TeamError("team_snapshot_invalid")
            # 即使为 staged -f ignored 文件也不集成，避免复制运行时资料。
            if path in ignored:
                raise TeamError("team_snapshot_invalid")
            data, identity, mode = _read_file(root, path, MAX_FILE_BYTES, missing=True)
            if data is not None:
                total += len(data)
                if total > MAX_SNAPSHOT_BYTES or _secret(data, self.store.sensitive_values):
                    raise TeamError("team_snapshot_invalid")
            bmode, boid = tree.get(path, (None, None))
            imode, ioid = index.get(path, (None, None))
            if data is None:
                final_oid = None
            else:
                algorithm = hashlib.sha256 if len(base) == 64 else hashlib.sha1
                final_oid = algorithm(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            # Windows 不报告 executable bit，使用已验证的 index/base mode。
            mode = (imode or bmode or "100644") if data is not None and os.name == "nt" else mode
            no_changes = no_changes and boid == final_oid and bmode == mode
            files.append(SnapshotFile(path, boid, ioid, mode, imode,
                hashlib.sha256(data).hexdigest() if data is not None else None,
                data is not None and (b"\0" in data or not _utf8(data)), data))
            signatures.append((path, identity))
        return tuple(files), (index_raw.hex(), index_signature, signatures, ignored), no_changes


def _paths(raw, *, directories=False):
    if not raw:
        return ()
    if not raw.endswith(b"\0"):
        raise TeamError("team_snapshot_invalid")
    paths = tuple(part.decode("utf-8", errors="strict") for part in raw[:-1].split(b"\0"))
    if directories:
        paths = tuple(path[:-1] if path.endswith("/") else path for path in paths)
    if len(paths) > MAX_PATHS or len(set(paths)) != len(paths):
        raise TeamError("team_snapshot_invalid")
    for path in paths:
        _integration_relative_path(path)
    return paths


def _entries(raw, *, index=False):
    result = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, path_raw = record.split(b"\t", 1)
        fields = header.decode("ascii").split(" ")
        if index:
            mode, oid, stage = fields
            if stage != "0":
                raise TeamError("team_snapshot_invalid")
        else:
            mode, kind, oid = fields
            if kind != "blob":
                raise TeamError("team_snapshot_invalid")
        path = path_raw.decode("utf-8")
        _integration_relative_path(path)
        if mode not in {"100644", "100755"} or not _valid_object_id(oid) or path in result:
            raise TeamError("team_snapshot_invalid")
        result[path] = (mode, oid)
    if raw and not raw.endswith(b"\0") or len(result) > MAX_PATHS:
        raise TeamError("team_snapshot_invalid")
    if len({p.casefold() for p in result}) != len(result):
        raise TeamError("team_snapshot_invalid")
    return result


def _attributes(raw):
    if not raw:
        return
    if not raw.endswith(b"\0"):
        raise TeamError("team_snapshot_invalid")
    fields = raw[:-1].split(b"\0")
    if len(fields) % 3:
        raise TeamError("team_snapshot_invalid")
    for _, name, value in zip(fields[::3], fields[1::3], fields[2::3]):
        if not (name == b"text" and value in {b"set", b"unset", b"auto"}
                or name == b"eol" and value in {b"lf", b"crlf"}):
            raise TeamError("team_snapshot_invalid")


def _safe_source_path(path):
    _integration_relative_path(path)
    parts = path.casefold().split("/")
    if (any(p in _PRIVATE_PARTS or p.startswith(".env.") or _PRIVATE_NAME.search(p) for p in parts)
            or parts[-1] in {".gitattributes", ".gitmodules"}
            or parts[-1].endswith((".pem", ".key", ".p12", ".pfx", ".pyc", ".log"))):
        raise TeamError("team_snapshot_invalid")


def _secret(data, sensitive):
    return (any(value and value.encode("utf-8") in data for value in sensitive)
            or bool(_CREDENTIAL.search(data))
            or sanitize(data.decode("utf-8", errors="replace")) != data.decode("utf-8", errors="replace"))


def _utf8(data):
    try:
        data.decode("utf-8")
        return True
    except UnicodeError:
        return False


def _plain(info, *, directory=False):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1):
        raise TeamError("team_snapshot_invalid")


def _read_file(root, relative, maximum, *, missing=False):
    _integration_relative_path(relative)
    with _directory_guard(Path(root) / Path(relative).parent, boundary=Path(root), missing=missing):
        return _read_file_locked(root, relative, maximum, missing=missing)


def _read_file_locked(root, relative, maximum, *, missing=False, hash_only=False, git_blob_algorithm=None):
    """逐段 lstat + fd 身份核对；只读普通文件，前后 stat/hash 用于并发检测。"""
    _integration_relative_path(relative)
    root = Path(root)
    current = root
    _plain(current.lstat(), directory=True)
    for component in relative.split("/")[:-1]:
        current = current / component
        try:
            _plain(current.lstat(), directory=True)
        except FileNotFoundError:
            if missing:
                return None, None, None
            raise
    path = current / relative.split("/")[-1]
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None, None, None
        raise
    _plain(info)
    if (maximum is not None and info.st_size > maximum
            or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True))):
        raise TeamError("team_snapshot_invalid")
    def signature(s):
        # Windows Python 3.14 的 path-stat 与 fd-stat ctime 来源可能不同；
        # fd 交叉验证使用共同字段，ctime 只在两次 path-stat 之间比较。
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        _plain(os.fstat(stream.fileno()))
        if signature(info) != signature(os.fstat(stream.fileno())):
            raise TeamError("team_snapshot_invalid")
        if hash_only:
            # 目标 unchanged 文件不受快照的 1 MiB/16 MiB 限制；流式 hash，
            # 最多读取扫描开始时的文件大小，增长/截短时拒绝，不无限追读。
            digest = hashlib.new(git_blob_algorithm or "sha256")
            if git_blob_algorithm is not None:
                digest.update(b"blob " + str(info.st_size).encode() + b"\0")
            remaining = info.st_size
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise TeamError("team_target_changed")
                digest.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise TeamError("team_target_changed")
            data = digest.hexdigest()
        else:
            data = stream.read(maximum + 1)
        if (not hash_only and len(data) > maximum
                or signature(info) != signature(os.fstat(stream.fileno()))):
            raise TeamError("team_snapshot_invalid")
    after = path.lstat()
    if signature(info) != signature(after) or info.st_ctime_ns != after.st_ctime_ns:
        raise TeamError("team_snapshot_invalid")
    return data, (*signature(info), info.st_ctime_ns), "100755" if info.st_mode & 0o111 else "100644"


@contextmanager
def _directory_guard(path, *, boundary, missing=False):
    """沿路径持有 Windows 目录读句柄且不共享 DELETE，防止检查后换根。"""
    handles = []
    path = Path(path)
    boundary = Path(boundary)
    if not path.is_relative_to(boundary):
        raise TeamError("team_snapshot_invalid")
    reject_links(path, missing=missing)
    try:
        if os.name == "nt":
            kernel, _ = _win()
            current = boundary
            chain = [current]
            for component in path.relative_to(boundary).parts:
                current /= component
                chain.append(current)
            for current in chain:
                if not current.exists() and missing:
                    break
                _plain(current.lstat(), directory=True)
                handle = kernel.CreateFileW(str(current), 0x80000000, 0x3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise TeamError("team_snapshot_invalid")
                handles.append(handle)
            reject_links(path, missing=missing)
        yield
        reject_links(path, missing=missing)
    finally:
        if os.name == "nt":
            for handle in reversed(handles):
                kernel.CloseHandle(handle)


@contextmanager
def _existing_lock(path, *, boundary):
    with _directory_guard(Path(path).parent, boundary=boundary):
        with _lock_existing_file(path):
            yield


@contextmanager
def _lock_existing_file(path):
    """已有受控锁的内核互斥，不创建/截断/写 owner；无法证明则拒绝。"""
    fd = None
    try:
        path = Path(path)
        current = Path(path.anchor)
        for component in path.parts[1:-1]:
            current /= component
            _plain(current.lstat(), directory=True)
        info = path.lstat()
        _plain(info)
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        if not os.path.samestat(info, os.fstat(fd)) or info.st_size < 1:
            raise TeamError("team_store_busy")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            elif os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise TeamError("team_store_unavailable")
        except OSError:
            raise TeamError("team_store_busy") from None
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd is not None:
            os.close(fd)
