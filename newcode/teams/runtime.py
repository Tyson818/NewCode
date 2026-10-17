"""Team 后端的有界调度与执行者租约；不创建 Agent 或 Provider。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from contextlib import AbstractContextManager
import secrets
import threading
import time
from typing import Callable
import ctypes
from ctypes import wintypes as wt
import json
import os
import re
import struct
from pathlib import Path

from .types import (
    MAX_MEMBERS, MAX_RUNNING_MEMBERS, MAX_QUEUED_MEMBERS, MAX_MEMBER_SECONDS,
    SHUTDOWN_SECONDS, TeamError, TeamScope,
)


@dataclass(frozen=True)
class ExecutionSnapshot:
    scope: TeamScope
    state: str
    executing: bool = False
    wake_pending: bool = False
    error_code: str | None = None


@dataclass(frozen=True)
class ShutdownReport:
    pending: tuple[TeamScope, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class ActivationContext:
    scope: TeamScope
    cancel_event: threading.Event = field(repr=False)
    deadline: float
    bootstrap: object = field(default=None, repr=False)


class _Lease(AbstractContextManager):
    def __init__(self, release):
        self._release = release

    def __exit__(self, *_):
        if self._release is not None:
            release, self._release = self._release, None
            release()


class ExecutorLeases:
    """进程内所有后端共享的成员 owner，generation 变化不允许重叠接管。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._owners = {}

    def acquire(self, scope: TeamScope):
        key = (scope.team_id, scope.member_id)
        with self._lock:
            if key in self._owners:
                raise TeamError("team_member_busy")
            token = secrets.token_hex(16)
            self._owners[key] = token

        def release():
            with self._lock:
                if self._owners.get(key) == token:
                    del self._owners[key]
        return _Lease(release)


_PROCESS_LEASES = ExecutorLeases()


class StoreExecutorLeases:
    """复用 Phase 1 的真实跨进程锁；锁持有到执行者实际退出。"""

    def __init__(self, store):
        self.store = store

    def acquire(self, scope: TeamScope):
        from .store import TeamFileLock
        manifest = self.store.load_team(scope.team_id)
        if scope.member_id not in {member.member_id for member in manifest.roster}:
            raise TeamError("team_scope_invalid")
        lease = TeamFileLock(
            self.store.paths, (scope.team_id, "locks", f"executor-{scope.member_id}.lock"),
            scope=f"{scope.team_id}:{scope.member_id}", timeout=0,
        )
        try:
            lease.__enter__()
        except TeamError as exc:
            if exc.code == "team_store_busy":
                raise TeamError("team_member_busy") from None
            raise
        return lease


@dataclass
class _Member:
    scope: TeamScope
    bootstrap: object = field(repr=False)
    lease: object = field(repr=False)
    state: str = "queued"
    executing: bool = False
    pending: bool = False
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    deadline: float = 0.0
    stop_requested: bool = False
    error_code: str | None = None
    release_started: bool = False


class LightweightRuntime:
    """每次 activation 占一个 worker；idle 只保留 owner，不占执行线程。"""

    def __init__(self, worker: Callable[[ActivationContext], None], *,
                 max_workers: int = MAX_RUNNING_MEMBERS, max_members: int = MAX_MEMBERS,
                 queue_capacity: int = MAX_QUEUED_MEMBERS,
                 activation_timeout: float = MAX_MEMBER_SECONDS, leases=None):
        for value, maximum in ((max_workers, MAX_RUNNING_MEMBERS), (max_members, MAX_MEMBERS),
                               (queue_capacity, MAX_QUEUED_MEMBERS)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise TeamError("team_arguments_invalid")
        if (not callable(worker) or isinstance(activation_timeout, bool)
                or not isinstance(activation_timeout, (int, float))
                or not 0 < activation_timeout <= MAX_MEMBER_SECONDS):
            raise TeamError("team_arguments_invalid")
        self._callback = worker
        self._leases = leases or _PROCESS_LEASES
        self._max_members, self._queue_capacity = max_members, queue_capacity
        self._activation_timeout = activation_timeout
        self._condition = threading.Condition(threading.RLock())
        self._members: dict[tuple[str, str], _Member] = {}
        self._starting: dict[tuple[str, str], TeamScope] = {}
        self._start_cancelled = set()
        self._queue = deque()
        self._release_queue = deque()
        self._closed = False
        self._shutdown_deadline = None
        self._threads = [threading.Thread(target=self._run, name=f"newcode-team-{i}", daemon=True)
                         for i in range(max_workers)]
        self._monitor = threading.Thread(target=self._watch_deadlines, name="newcode-team-deadlines", daemon=True)
        self._releaser = threading.Thread(target=self._release_loop, name="newcode-team-lease-release", daemon=True)
        started = []
        try:
            for thread in (*self._threads, self._monitor, self._releaser):
                thread.start()
                started.append(thread)
        except BaseException:
            with self._condition:
                self._closed = True
                self._shutdown_deadline = time.monotonic() + SHUTDOWN_SECONDS
                self._condition.notify_all()
            for thread in started:
                thread.join(max(0, self._shutdown_deadline - time.monotonic()))
            raise TeamError("team_backend_start_failed") from None

    @staticmethod
    def _key(scope):
        if not isinstance(scope, TeamScope):
            raise TeamError("team_scope_invalid")
        return scope.team_id, scope.member_id

    def start(self, scope: TeamScope, bootstrap=None) -> ExecutionSnapshot:
        key = self._key(scope)
        with self._condition:
            if self._closed:
                raise TeamError("team_state_invalid")
            old = self._members.get(key)
            if key in self._starting or old is not None and old.state != "stopped":
                raise TeamError("team_member_busy")
            if sum(member.state != "stopped" for member in self._members.values()) + len(self._starting) >= self._max_members:
                raise TeamError("team_limit_exceeded")
            self._ensure_queue_space()
            self._starting[key] = scope
        lease = None
        try:
            # 真实平台租约获取可阻塞；不持有调度锁，shutdown能限时观察starting。
            lease = self._leases.acquire(scope)
            with self._condition:
                if self._closed or key in self._start_cancelled:
                    member = _Member(scope, None, lease, state="stopping", stop_requested=True)
                    self._members[key] = member
                    self._release_locked(member)
                    raise TeamError("team_state_invalid")
                member = _Member(scope, bootstrap, lease)
                self._members[key] = member
                self._queue.append(key)
                self._condition.notify_all()
                return self._snapshot(member)
        finally:
            with self._condition:
                self._starting.pop(key, None)
                self._start_cancelled.discard(key)
                self._condition.notify_all()

    def wake(self, scope: TeamScope) -> ExecutionSnapshot:
        with self._condition:
            member = self._member(scope)
            if self._closed or member.stop_requested or member.state in {"failed", "stopped"}:
                raise TeamError("team_state_invalid")
            if member.state == "queued" or member.pending:
                return self._snapshot(member)
            self._ensure_queue_space()
            if member.executing:
                member.pending = True
            else:
                member.state = "queued"
                self._queue.append(self._key(scope))
            self._condition.notify_all()
            return self._snapshot(member)

    def request_stop(self, scope: TeamScope) -> ExecutionSnapshot:
        with self._condition:
            key = self._key(scope)
            if self._starting.get(key) == scope:
                self._start_cancelled.add(key)
                return ExecutionSnapshot(scope, "stopping")
            member = self._member(scope)
            self._stop_locked(member)
            self._condition.notify_all()
            return self._snapshot(member)

    def status(self, scope: TeamScope) -> ExecutionSnapshot:
        with self._condition:
            if self._starting.get(self._key(scope)) == scope:
                return ExecutionSnapshot(scope, "stopping" if self._key(scope) in self._start_cancelled else "starting")
            return self._snapshot(self._member(scope))

    def close(self, *, deadline=None) -> None:
        with self._condition:
            if not self._closed:
                self._closed = True
                self._shutdown_deadline = min(time.monotonic() + SHUTDOWN_SECONDS,
                                              deadline if deadline is not None else float("inf"))
                for member in self._members.values():
                    self._stop_locked(member)
                self._condition.notify_all()
            elif deadline is not None:
                self._shutdown_deadline = min(self._shutdown_deadline, deadline)

    def join_until(self, deadline: float) -> ShutdownReport:
        with self._condition:
            end = min(deadline, time.monotonic() + SHUTDOWN_SECONDS)
            if self._shutdown_deadline is not None:
                end = min(end, self._shutdown_deadline)
            while self._pending_locked():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            pending = self._pending_locked()
            return ShutdownReport(pending, "team_shutdown_timeout" if pending else None)

    def shutdown(self) -> ShutdownReport:
        self.close()
        return self.join_until(self._shutdown_deadline)

    def _member(self, scope):
        member = self._members.get(self._key(scope))
        if member is None or member.scope != scope:
            raise TeamError("team_scope_invalid")
        return member

    def _ensure_queue_space(self):
        if len(self._queue) + sum(member.pending for member in self._members.values()) + len(self._starting) >= self._queue_capacity:
            raise TeamError("team_limit_exceeded")

    def _pending_locked(self):
        return (*self._starting.values(), *(member.scope for member in self._members.values()
                if member.executing or member.stop_requested and member.state != "stopped"))

    def _stop_locked(self, member):
        member.stop_requested = True
        member.pending = False
        member.cancel.set()
        key = self._key(member.scope)
        if key in self._queue:
            self._queue.remove(key)
        if member.executing:
            member.state = "stopping"
        elif member.state != "stopped":
            self._release_locked(member)

    def _release_locked(self, member):
        if member.release_started:
            return
        member.release_started = True
        member.state = "stopping"
        self._release_queue.append(member)
        self._condition.notify_all()

    def _release_loop(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._release_queue or (
                    self._closed and not self._starting
                    and not any(member.executing for member in self._members.values())))
                if not self._release_queue:
                    return
                member = self._release_queue.popleft()
            failed = False
            try:
                member.lease.__exit__(None, None, None)
            except BaseException:
                failed = True
            with self._condition:
                # owner释放无法证明时保留busy，不能重新接管。
                member.state = "failed" if failed else "stopped"
                if failed:
                    member.error_code = "team_worker_failed"
                self._condition.notify_all()

    @staticmethod
    def _snapshot(member):
        return ExecutionSnapshot(member.scope, member.state, member.executing,
                                 member.pending or member.state == "queued", member.error_code)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._queue)
                if self._closed and not self._queue:
                    return
                key = self._queue.popleft()
                member = self._members[key]
                member.state = "running"
                member.executing = True
                member.deadline = time.monotonic() + self._activation_timeout
                context = ActivationContext(member.scope, member.cancel, member.deadline, member.bootstrap)
            failed = False
            try:
                result = self._callback(context)
                if result is not None:
                    raise TeamError("team_worker_failed")
            except BaseException:
                failed = True
            finally:
                with self._condition:
                    member.executing = False
                    if failed:
                        member.error_code = "team_worker_failed"
                    if member.stop_requested or self._closed:
                        self._release_locked(member)
                    elif failed:
                        member.state = "failed"
                        member.pending = False
                    elif member.pending:
                        member.pending = False
                        member.state = "queued"
                        self._queue.append(key)
                    else:
                        member.state = "idle"
                    self._condition.notify_all()

    def _watch_deadlines(self):
        with self._condition:
            while not self._closed:
                now = time.monotonic()
                for member in self._members.values():
                    if member.executing and not member.stop_requested and now >= member.deadline:
                        member.error_code = "team_worker_failed"
                        self._stop_locked(member)
                self._condition.wait(0.02)


class TeamRuntime:
    """仅协调后端关闭，多个后端共享一个截止时间；尚不接入成员/CLI。"""

    def __init__(self, backends):
        self._backends = tuple(dict.fromkeys(backends))
        self._lock = threading.Lock()
        self._deadline = None

    def shutdown(self):
        with self._lock:
            if self._deadline is None:
                self._deadline = time.monotonic() + SHUTDOWN_SECONDS
            deadline = self._deadline
        failed = False
        pending = []
        for backend in self._backends:
            try:
                backend.close(deadline=deadline)
            except Exception:
                failed = True
        for backend in self._backends:
            try:
                result = backend.join_until(deadline)
                pending.extend(result.pending)
                failed |= result.error_code is not None
            except Exception:
                failed = True
        unique = tuple(dict.fromkeys(pending))
        error = "team_shutdown_timeout" if unique else "team_worker_failed" if failed else None
        return ShutdownReport(unique, error)


MAX_IPC_BYTES = 64 * 1024


def _pipe_api():
    """仅本地 Windows named pipe；其它平台无验证实现时报告 unavailable。"""
    if os.name != "nt":
        raise TeamError("team_backend_unavailable")
    from .paths import _win
    kernel, security = _win()
    kernel.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD,
                                      wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    kernel.CreateNamedPipeW.restype = wt.HANDLE
    kernel.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
    kernel.DisconnectNamedPipe.argtypes = [wt.HANDLE]
    kernel.GetNamedPipeClientProcessId.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel.GetNamedPipeServerProcessId.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel.SetNamedPipeHandleState.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD), ctypes.c_void_p, ctypes.c_void_p]
    kernel.PeekNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
                                    ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    kernel.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    kernel.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    return kernel, security


def _endpoint(value):
    if not isinstance(value, str) or not re.fullmatch(r"\\\\\.\\pipe\\newcode-team-[0-9a-f]{32}", value):
        raise TeamError("team_arguments_invalid")
    return value


def _peer_pid(handle, *, server):
    from .paths import current_user_sid, _sid_text
    kernel, security = _pipe_api()
    pid = wt.DWORD()
    query = kernel.GetNamedPipeClientProcessId if server else kernel.GetNamedPipeServerProcessId
    if not query(handle, ctypes.byref(pid)) or not pid.value:
        raise TeamError("team_backend_start_failed")
    process = kernel.OpenProcess(0x1000, False, pid.value)
    if not process:
        raise TeamError("team_backend_start_failed")
    token = wt.HANDLE()
    try:
        if not security.OpenProcessToken(process, 0x8, ctypes.byref(token)):
            raise TeamError("team_backend_start_failed")
        size = wt.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not size.value or not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise TeamError("team_backend_start_failed")
        sid = ctypes.c_void_p.from_buffer(buffer).value
        if _sid_text(security, kernel, sid) != current_user_sid():
            raise TeamError("team_backend_start_failed")
        return pid.value
    finally:
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def verify_pipe_acl(handle):
    """检查真实 pipe owner/DACL，拒绝宽松或未知 ACE。"""
    from .paths import current_user_sid, _sid_text
    kernel, security = _pipe_api()
    security.GetSecurityInfo.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
                                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)]
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if security.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None,
                                ctypes.byref(acl), None, ctypes.byref(descriptor)) != 0:
        raise TeamError("team_backend_unavailable")
    try:
        user = current_user_sid()
        if not owner.value or not acl.value or _sid_text(security, kernel, owner) != user:
            raise TeamError("team_backend_unavailable")
        class AclSize(ctypes.Structure):
            _fields_ = [("count", wt.DWORD), ("used", wt.DWORD), ("free", wt.DWORD)]
        size = AclSize()
        if not security.GetAclInformation(acl, ctypes.byref(size), ctypes.sizeof(size), 2) or size.count != 1:
            raise TeamError("team_backend_unavailable")
        ace = ctypes.c_void_p()
        if not security.GetAce(acl, 0, ctypes.byref(ace)):
            raise TeamError("team_backend_unavailable")
        if ctypes.c_ubyte.from_address(ace.value).value != 0 or _sid_text(security, kernel, ace.value + 8) != user:
            raise TeamError("team_backend_unavailable")
    finally:
        kernel.LocalFree(descriptor)


def _process_details(pid):
    """用内核句柄读取映像/创建时间，用快照读取直接父进程。"""
    kernel, _ = _pipe_api()
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    kernel.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wt.HANDLE
    class Entry(ctypes.Structure):
        _fields_ = [("size", wt.DWORD), ("usage", wt.DWORD), ("pid", wt.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wt.DWORD), ("threads", wt.DWORD),
                    ("parent", wt.DWORD), ("priority", wt.LONG), ("flags", wt.DWORD),
                    ("image", wt.WCHAR * 260)]
    kernel.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(Entry)]
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        raise TeamError("team_backend_start_failed")
    snapshot = None
    try:
        image = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(image))
        created, exited, system, user = (ctypes.c_ulonglong() for _ in range(4))
        code = wt.DWORD()
        if (not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size))
                or not kernel.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                                              ctypes.byref(system), ctypes.byref(user))
                or not kernel.GetExitCodeProcess(process, ctypes.byref(code)) or code.value != 259):
            raise TeamError("team_backend_start_failed")
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            snapshot = None
            raise TeamError("team_backend_start_failed")
        entry = Entry()
        entry.size = ctypes.sizeof(Entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.pid == pid:
                return Path(image.value).resolve(strict=True), created.value, entry.parent
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        raise TeamError("team_backend_start_failed")
    finally:
        if snapshot is not None:
            kernel.CloseHandle(snapshot)
        kernel.CloseHandle(process)


@dataclass(frozen=True)
class LaunchIdentity:
    """允许验证过的 Windows venv redirector 的一个直接解释器子进程。"""

    pid: int
    launcher_image: Path
    worker_image: Path

    def matches(self, actual_pid):
        actual_image, created, parent = _process_details(actual_pid)
        launcher_image = self.launcher_image.resolve(strict=True)
        worker_image = self.worker_image.resolve(strict=True)
        if actual_pid == self.pid:
            return actual_image == launcher_image
        expected_image, expected_created, _ = _process_details(self.pid)
        return (parent == self.pid and expected_image == launcher_image
                and actual_image == worker_image and created >= expected_created)


class PipeChannel:
    """大小受限的 JSON 帧；peer PID 来自内核而非 JSON 身份声明。"""

    def __init__(self, handle, peer_pid):
        self._handle, self.peer_pid = handle, peer_pid
        self._send_lock = threading.Lock()
        self._receive_lock = threading.Lock()

    def close(self):
        if self._handle is not None:
            kernel, _ = _pipe_api()
            if not kernel.CloseHandle(self._handle):
                raise TeamError("team_worker_failed")
            self._handle = None

    def send(self, value, *, deadline):
        from .store import encode
        data = encode(value)
        if len(data) > MAX_IPC_BYTES:
            raise TeamError("team_limit_exceeded")
        frame = struct.pack("!I", len(data)) + data
        kernel, _ = _pipe_api()
        with self._send_lock:
            offset = 0
            while offset < len(frame):
                if self._handle is None or time.monotonic() >= deadline:
                    raise TeamError("team_backend_start_failed")
                buffer = ctypes.create_string_buffer(frame[offset:])
                written = wt.DWORD()
                if not kernel.WriteFile(self._handle, buffer, len(frame) - offset, ctypes.byref(written), None):
                    if ctypes.get_last_error() not in {232, 231, 536}:
                        raise TeamError("team_backend_start_failed")
                if written.value:
                    offset += written.value
                else:
                    time.sleep(min(0.005, max(0, deadline - time.monotonic())))

    def receive(self, *, deadline):
        def unique(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        with self._receive_lock:
            size = struct.unpack("!I", self._read(4, deadline))[0]
            if not 0 < size <= MAX_IPC_BYTES:
                raise TeamError("team_backend_start_failed")
            try:
                value = json.loads(self._read(size, deadline), object_pairs_hook=unique,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(value, dict):
                    raise ValueError
                return value
            except (ValueError, UnicodeError, RecursionError):
                raise TeamError("team_backend_start_failed") from None

    def _read(self, size, deadline):
        kernel, _ = _pipe_api()
        result = bytearray()
        while len(result) < size:
            if self._handle is None or time.monotonic() >= deadline:
                raise TeamError("team_backend_start_failed")
            available = wt.DWORD()
            if not kernel.PeekNamedPipe(self._handle, None, 0, None, ctypes.byref(available), None):
                raise TeamError("team_backend_start_failed")
            if not available.value:
                time.sleep(min(0.005, max(0, deadline - time.monotonic())))
                continue
            count = min(available.value, size - len(result))
            buffer = ctypes.create_string_buffer(count)
            read = wt.DWORD()
            if not kernel.ReadFile(self._handle, buffer, count, ctypes.byref(read), None):
                raise TeamError("team_backend_start_failed")
            result.extend(buffer.raw[:read.value])
        return bytes(result)


class PipeListener:
    def __init__(self):
        from .paths import current_user_sid
        kernel, security = _pipe_api()
        self.endpoint = "\\\\.\\pipe\\newcode-team-" + secrets.token_hex(16)
        self._handle = None
        descriptor = ctypes.c_void_p()
        sddl = f"O:{current_user_sid()}D:P(A;;GA;;;{current_user_sid()})"
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise TeamError("team_backend_start_failed")
        class Attributes(ctypes.Structure):
            _fields_ = [("length", wt.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wt.BOOL)]
        try:
            attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
            # FIRST_PIPE_INSTANCE + 本地单实例、NOWAIT；不继承句柄。
            handle = kernel.CreateNamedPipeW(self.endpoint, 3 | 0x80000, 1 | 8, 1,
                                            128 * 1024, 128 * 1024, 0, ctypes.byref(attributes))
            if handle == ctypes.c_void_p(-1).value:
                raise TeamError("team_backend_start_failed")
            self._handle = handle
            try:
                verify_pipe_acl(handle)
            except BaseException:
                self.close()
                raise
        finally:
            kernel.LocalFree(descriptor)

    @staticmethod
    def supported():
        try:
            _pipe_api()
            return True
        except (TeamError, OSError, AttributeError):
            return False

    def accept(self, expected_pid: int, *, deadline):
        kernel, _ = _pipe_api()
        while self._handle is not None and time.monotonic() < deadline:
            if kernel.ConnectNamedPipe(self._handle, None) or ctypes.get_last_error() == 535:
                handle, self._handle = self._handle, None
                try:
                    actual_pid = _peer_pid(handle, server=True)
                    matches = expected_pid.matches(actual_pid) if isinstance(expected_pid, LaunchIdentity) else actual_pid == expected_pid
                    if not matches:
                        raise TeamError("team_backend_start_failed")
                    return PipeChannel(handle, actual_pid)
                except BaseException:
                    kernel.CloseHandle(handle)
                    raise
            if ctypes.get_last_error() not in {536, 232}:
                raise TeamError("team_backend_start_failed")
            time.sleep(min(0.005, max(0, deadline - time.monotonic())))
        raise TeamError("team_backend_start_failed")

    def close(self):
        if self._handle is not None:
            kernel, _ = _pipe_api()
            if not kernel.CloseHandle(self._handle):
                raise TeamError("team_worker_failed")
            self._handle = None


def connect_pipe(endpoint, *, deadline):
    kernel, _ = _pipe_api()
    _endpoint(endpoint)
    while time.monotonic() < deadline:
        handle = kernel.CreateFileW(endpoint, 0xC0000000, 0, None, 3, 0, None)
        if handle != ctypes.c_void_p(-1).value:
            try:
                mode = wt.DWORD(1)
                if not kernel.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
                    raise TeamError("team_backend_start_failed")
                return PipeChannel(handle, _peer_pid(handle, server=False))
            except BaseException:
                kernel.CloseHandle(handle)
                raise
        if ctypes.get_last_error() not in {2, 231}:
            raise TeamError("team_backend_start_failed")
        time.sleep(min(0.005, max(0, deadline - time.monotonic())))
    raise TeamError("team_backend_start_failed")


class TeamAttachment:
    """CLI 主线程拥有的 attachment；worker 只收到独立 runner 与不可变策略。

    admission 只由 AgentLoop 在 Permission/scheduler 之后短暂建立，不能作为
    session allow 持久化。分离 attachment 不等于已经停止未退出的执行者。
    """
    def __init__(self, *, store, config, coordinator=None, runner_factory=None,
                 integration_worktrees=None, lead_barrier=None, single_writer_confirm=None):
        from .coordinator import CoordinatorPolicy
        self.store, self.config = store, config
        self.coordinator = coordinator or CoordinatorPolicy()
        self.runner_factory = runner_factory
        self.integration_worktrees = integration_worktrees
        self.lead_barrier = lead_barrier
        self.single_writer_confirm = single_writer_confirm
        self.role = "ordinary"
        self.team_id = None
        self.generation = 0
        self._owner = threading.get_ident()
        self._lock = threading.RLock()
        self._admission = threading.local()
        self._handles = {}
        self._retired = []
        self._closers = {}
        self._upper = self._current = None
        self._session = None
        self._mailbox_key = secrets.token_bytes(32)

    def checkpoint_observations(self, session):
        """Lead read 的交付证明先持久化，read 本身不 ack。"""
        from .member import MemberCheckpoints, _has_delivery
        from newcode.persistence import is_valid_session_id
        if self.role != "lead" or self.team_id is None or not is_valid_session_id(session.session_id):
            return
        self._session = None
        known = self.store.load_records(self.team_id, "messages")
        observed = tuple(m["message_id"] for m in known if m["recipient"] == "lead" and _has_delivery(session.messages, m))
        MemberCheckpoints(self.store, self.team_id, "lead", generation=self.generation).checkpoint(
            session, processed_message_ids=observed)
        self._session = session

    def command(self, arguments):
        self._main_thread()
        if not self.config.enabled:
            raise TeamError("team_coordinator_unavailable")
        op = arguments[0]
        with self._lock:
            if op == "status":
                return self.summary()
            if op == "close":
                self.detach()
                return self.summary()
            if self.role != "ordinary":
                raise TeamError("team_state_invalid")
            if self._retired:
                raise TeamError("team_member_busy")
            if op == "open":
                # load_team 校验 canonical workspace/common-dir 指纹。
                manifest = self.store.load_team(arguments[1])
                self.team_id = manifest.team_id
            self.role = "lead"
            self.generation += 1
            self._upper = self._current = None
            return self.summary()

    def _main_thread(self):
        if threading.get_ident() != self._owner:
            raise TeamError("team_role_denied")

    def summary(self):
        result = {"role": self.role, "team_id": self.team_id, "generation": self.generation,
                  "coordinator": self.coordinator.enabled, "pending": len(self._retired)}
        if self.team_id is not None:
            manifest = self.store.load_team(self.team_id)
            result.update(revision=manifest.revision, state=manifest.state.value,
                members=len(manifest.roster)-1, max_members=self.config.max_members,
                max_running_members=self.config.max_running_members)
        return result

    def filter_tools(self, names):
        from .tools import TEAM_TOOLS
        allowed = self.coordinator.filter(names)
        if self.role != "lead":
            allowed -= TEAM_TOOLS
        return frozenset(allowed)

    def publish(self, names, mode):
        """P0/Pt 只收窄；审批状态由独立可信 journal 读取，不能恢复 Pt。"""
        from dataclasses import replace
        from .types import PlanState
        self._main_thread()
        self._mode = mode
        with self._lock:
            names = frozenset(names)
            if getattr(mode, "value", mode) == "plan":
                names &= {"read_file", "find_files", "search_code", "team_status"}
            self._upper = names if self._upper is None else self._upper
            self._current = names & self._upper if self._current is None else self._current & names
            for runner, _ in self._handles.values():
                old = runner._policy
                allowed = old.current_tools & self._current
                # Pt 先发布；不能让磁盘/锁错误延迟父级收窄。
                runner.publish_policy(replace(old, revision=old.revision + 1, current_tools=allowed))
                runner.mode = mode
                try:
                    plans = self.store.load_records(self.team_id, "plans")
                except TeamError:
                    current = runner._policy
                    runner.publish_policy(replace(current, revision=current.revision + 1, plan_state=PlanState.PENDING))
                    continue
                own = [p for p in plans if p["member_id"] == runner.scope.member_id and p["state"] != "superseded"]
                revision = max((p["revision"] for p in own), default=0)
                approved = bool(own and all(p["state"] == "approved" for p in own))
                runner.publish_policy(replace(old, revision=old.revision + 2, current_tools=allowed,
                    plan_state=PlanState.APPROVED if approved else PlanState.PENDING, plan_revision=revision))
                runner.mode = mode

    def publish_permissions(self, manager):
        from .member import MemberPermissions
        from newcode.permissions.types import PermissionDecisionValue
        self._main_thread()
        snapshot = MemberPermissions(mode=manager.mode, local_rules=manager.local_project_rules.rules,
            project_rules=manager.project_rules.rules, user_rules=manager.user_global_rules.rules,
            rule_load_errors=manager.rule_load_errors,
            session_denies=tuple(rule for rule in manager.session_rules.rules if rule.action is PermissionDecisionValue.DENY))
        self._permission_snapshot = snapshot
        for runner, _ in self._handles.values():
            runner.publish_permissions(snapshot)

    def admission(self, name, arguments):
        from contextlib import contextmanager
        @contextmanager
        def admitted():
            if name != "team_status":
                self._main_thread()
            with self._lock:
                previous = getattr(self._admission, "call", None)
                self._admission.call = (name, json.dumps(arguments, sort_keys=True))
                try:
                    yield
                finally:
                    self._admission.call = previous
        return admitted()

    def invoke(self, name, arguments, context):
        from .store import sanitize, encode
        from .tools import validate_arguments
        if name != "team_status":
            self._main_thread()
        if getattr(self._admission, "call", None) != (name, json.dumps(arguments, sort_keys=True)):
            raise TeamError("permission_denied")
        if not self.config.enabled or self.role != "lead":
            raise TeamError("team_role_denied")
        if self._current is None or name not in self._current:
            raise TeamError("disallowed_tool")
        if context.workspace_root != self.store.paths.workspace_root:
            raise TeamError("team_workspace_mismatch")
        validate_arguments(name, arguments, self.role)
        result = self._dispatch(name, arguments)
        if self.team_id is not None:
            try:
                result = {**result, "team_revision": self.store.load_team(self.team_id).revision}
            except TeamError:
                # 已提交的业务操作不因补充状态查询锁竞争而伪装失败/重复执行。
                result = {**result, "team_revision": None, "team_revision_status": "unavailable"}
        safe = sanitize(result, self.store.sensitive_values)
        if len(encode(safe)) > 16384:
            raise TeamError("team_limit_exceeded")
        return safe

    def _dispatch(self, name, args):
        from .tasks import TaskService
        from .approvals import ApprovalService
        from .mailbox import MailboxService
        op = args["operation"]
        if name == "team_status" and op == "status":
            if self.team_id is not None and self.store.load_team(self.team_id).state.value not in {"ready", "active", "closed"}:
                raise TeamError("team_state_invalid")
            return self.summary()
        if name == "team_manage" and op == "create":
            if self.team_id is not None:
                raise TeamError("team_state_invalid")
            from .types import Backend
            backend = Backend(args.get("backend", self.config.backend_default))
            # pane 尚未接通真实成员 runner，不把 probe/mock 当作可运行证据。
            if backend is not Backend.LIGHTWEIGHT:
                raise TeamError("team_backend_unavailable")
            manifest = self.store.create_team(args["team_name"], backend=backend)
            self.team_id = manifest.team_id
            return {"team_id": self.team_id, "revision": manifest.revision}
        if self.team_id is None:
            raise TeamError("team_scope_invalid")
        manifest = self.store.load_team(self.team_id)
        if name == "team_status":
            if manifest.state.value not in {"ready", "active", "closed"}:
                raise TeamError("team_state_invalid")
            return self._status(args, manifest)
        if manifest.state.value not in {"ready", "active"}:
            raise TeamError("team_state_invalid")
        if name == "team_integrate":
            # Coordinator 与普通 Lead 共用 T21 的受控集成管理器；缺少任一能力时拒绝。
            if self.integration_worktrees is None or self.lead_barrier is None:
                raise TeamError("team_coordinator_unavailable")
            from .integration import TeamIntegrationManager
            from newcode.permissions.types import PermissionDecisionValue
            admission = (name, json.dumps(args, sort_keys=True))
            def authorize(request):
                if (getattr(self._admission, "call", None) == admission and request == args
                        and self.role == "lead" and name in (self._current or ())):
                    return PermissionDecisionValue.ALLOW
                return PermissionDecisionValue.DENY
            manager = TeamIntegrationManager(self.store, self.team_id,
                worktrees=self.integration_worktrees,
                runners={key: handle[0] for key, handle in self._handles.items()},
                role=self.role, authorize=authorize, lead_barrier=self.lead_barrier,
                confirm_single_writer=self.single_writer_confirm)
            arguments = {k: v for k, v in args.items() if k != "operation"}
            return manager.preview(**arguments).summary() if op == "preview" else manager.integrate(**arguments)
        if name == "team_tasks":
            service = TaskService(self.store, self.team_id)
            a = {k: v for k, v in args.items() if k != "operation"}
            if op == "create":
                a["dependencies"] = tuple(a["dependencies"])
                return service.create("lead", **a).to_dict()
            if "expected_task_revision" in a:
                a["expected_revision"] = a.pop("expected_task_revision")
            with self._require_quiescent_task(a["task_id"]):
                if op == "update":
                    a["changes"] = {("state" if k == "status" else k): v for k, v in a["changes"].items()}
                result = getattr(service, op)("lead", **a)
            value = result.to_dict() if hasattr(result, "to_dict") else {"deleted": True}
            if op == "complete":
                value["verification"] = "not_verified"
            return value
        if name == "team_message":
            mailbox = MailboxService(self.store, self.team_id, "lead", wake=self._wake, cursor_key=self._mailbox_key)
            if op == "read":
                page = mailbox.read(**{k: v for k, v in args.items() if k != "operation"})
                # read 不自动 ack；Lead 尚未将 message ID checkpoint 时 ack 必须失败。
                return {"messages": [m.to_dict() for m in page.messages], "cursor": page.next_cursor}
            if op == "ack":
                from .member import MemberCheckpoints
                if self._session is None:
                    raise TeamError("team_ack_checkpoint_required")
                return {"acked": mailbox.ack(args["message_id"], session_id=self._session.session_id,
                    checkpoints=MemberCheckpoints(self.store, self.team_id, "lead", generation=self.generation))}
            result = getattr(mailbox, op)(**{k: v for k, v in args.items() if k != "operation"})
            return {"message_ids": [m.message_id for m in result.messages], "wake_failed": result.wake_failed}
        if name == "team_manage":
            if op in {"approve_plan", "reject_plan"}:
                method = "approve" if op == "approve_plan" else "reject"
                result = getattr(ApprovalService(self.store, self.team_id, "lead"), method)(
                    args["plan_id"], args["expected_plan_revision"], reason=args.get("reason", ""))
                # 下一轮与执行前再次读取 journal；批准不写 Permission/session allow。
                if self._current is not None:
                    self.publish(self._current, self._mode)
                self._wake(result.member_id)
                return result.to_dict()
            if op == "spawn":
                return self._spawn(args)
            if op == "archive":
                return self._archive(args)
            return self._member_operation(op, args)
        raise TeamError("team_arguments_invalid")

    def _status(self, args, manifest):
        from .tasks import TaskService
        from .mailbox import MailboxService
        op = args["operation"]
        if op == "plan":
            plans = self.store.load_records(self.team_id, "plans")
            result = next((p for p in plans if p["plan_id"] == args["plan_id"]), None)
            if result is None:
                raise TeamError("team_plan_not_found")
            return result
        if op == "inbox":
            page = MailboxService(self.store, self.team_id, "lead", cursor_key=self._mailbox_key).read(
                limit=args.get("limit", 20), cursor=args.get("cursor"))
            return {"messages": [{k: v for k, v in m.to_dict().items() if k in
                    {"message_id", "sender_member_id", "sequence", "created_at", "summary", "kind"}}
                    for m in page.messages], "cursor": page.next_cursor}
        if op == "tasks" and "task_id" in args:
            return TaskService(self.store, self.team_id).get("lead", args["task_id"]).to_dict()
        values = ([{"member_id": m.member_id, "role": m.role.value, "state": m.state.value,
                    "revision": m.revision, "requires_plan_approval": m.requires_plan_approval} for m in manifest.roster]
                  if op == "roster" else [t.to_dict() for t in TaskService(self.store, self.team_id).list("lead")])
        return self._page(values, args, manifest.revision)

    def _page(self, values, args, revision):
        import hashlib
        import hmac
        limit = args.get("limit", 20)
        query = f"{self.team_id}:{self.generation}:{args['operation']}:{limit}:{revision}"
        if not hasattr(self, "_cursor_key"):
            self._cursor_key = secrets.token_bytes(32)
        offset = 0
        expiry = int(time.time()) + 300
        if args.get("cursor"):
            try:
                raw, signature = args["cursor"].split(".")
                payload = f"{query}:{raw}"
                if not hmac.compare_digest(signature, hmac.new(self._cursor_key, payload.encode(), hashlib.sha256).hexdigest()):
                    raise ValueError()
                raw_offset, raw_expiry = raw.split(":")
                offset, expiry = int(raw_offset), int(raw_expiry)
                if not 0 <= offset <= len(values) or expiry < time.time():
                    raise ValueError()
            except (ValueError, TypeError):
                raise TeamError("team_cursor_invalid") from None
        from .store import encode
        page = []
        for value in values[offset:offset + limit]:
            if len(encode({"items": [*page, value]})) > 15000:
                break
            page.append(value)
        if offset < len(values) and not page:
            raise TeamError("team_limit_exceeded")
        following = offset + len(page)
        cursor = None
        if following < len(values):
            raw = f"{following}:{expiry}"
            cursor = raw + "." + hmac.new(self._cursor_key, f"{query}:{raw}".encode(), hashlib.sha256).hexdigest()
        return {"items": page, "cursor": cursor}

    def _require_quiescent_task(self, task_id):
        from contextlib import contextmanager
        from .tasks import TaskService
        @contextmanager
        def guard():
            task = TaskService(self.store, self.team_id).get("lead", task_id)
            if task.assignee:
                with self._quiescent_member(task.assignee):
                    yield
            else:
                yield
        return guard()

    def _quiescent_member(self, member_id):
        from contextlib import contextmanager
        @contextmanager
        def guard():
            if member_id in self._handles:
                with self._handles[member_id][0].quiescence():
                    yield
            else:
                member = next((m for m in self.store.load_team(self.team_id).roster if m.member_id == member_id), None)
                if member is None or member.role.value != "member":
                    raise TeamError("team_member_not_found")
                sid = member.checkpoint_session_id or "20000101-000000-aaaa"
                scope = TeamScope(self.team_id, member_id, sid, member.runtime_generation + 1)
                with StoreExecutorLeases(self.store).acquire(scope):
                    yield
        return guard()

    def _spawn(self, args):
        from dataclasses import replace
        from .types import Backend, TeamMemberRecord, TeamRole, RecordChange, new_id
        from .tasks import TaskService
        if self.runner_factory is None:
            raise TeamError("team_backend_unavailable")
        backend = Backend(args.get("backend", self.config.backend_default))
        if backend is not Backend.LIGHTWEIGHT:
            raise TeamError("team_backend_unavailable")
        approval = args.get("require_plan_approval", self.config.require_plan_approval)
        if self.config.require_plan_approval and not approval:
            raise TeamError("team_role_denied")
        # Factory 预检不创建 Worktree/Provider；非法定义在 roster mutation 前失败。
        prepared = self.runner_factory.prepare(args["agent_name"])
        with self.store.lock(self.team_id):
            view, _, _ = self.store._recover_locked(self.team_id)
            manifest = view["team"][self.team_id]
            if manifest["revision"] != args["expected_team_revision"]:
                raise TeamError("team_revision_conflict")
            if len(view["members"]) - 1 >= self.config.max_members:
                raise TeamError("team_limit_exceeded")
            if any(m["name"] == args["member_name"] for m in view["members"].values()):
                raise TeamError("team_name_conflict")
            task = TaskService(self.store, self.team_id)._get(view, args["task_id"])
            if task.state.value != "open" or task.assignee is not None:
                raise TeamError("team_task_conflict")
            TaskService._ensure_dependencies_complete(view, task)
            member = TeamMemberRecord("member-" + secrets.token_hex(8), args["member_name"], role=TeamRole.MEMBER,
                agent_name=args["agent_name"], requires_plan_approval=approval, auto_wake=True, backend=backend)
            assigned = replace(task, assignee=member.member_id, state="assigned", revision=task.revision + 1)
            self.store._commit_locked(self.team_id, manifest["revision"], new_id("mutation"), (
                RecordChange("members", member.member_id, member.to_dict()),
                RecordChange("tasks", task.task_id, assigned.to_dict())), None)
        runner, backend_instance = self.runner_factory.create(self, member, prepared)
        if hasattr(self, "_permission_snapshot"):
            runner.publish_permissions(self._permission_snapshot)
        self._handles[member.member_id] = (runner, backend_instance)
        try:
            backend_instance.start(runner.scope)
        except Exception:
            runner.close()
            # roster/成果保留供安全诊断和显式恢复，绝不退回 shared。
            raise TeamError("team_backend_start_failed") from None
        return {"member_id": member.member_id, "state": "starting"}

    def _member_operation(self, operation, args):
        if operation == "resume" and any(
                r["member_id"] == args["target_member_id"]
                for r in self.store.load_records(self.team_id, "integrations")):
            raise TeamError("team_state_invalid")
        member = next((m for m in self.store.load_team(self.team_id).roster
                       if m.member_id == args["target_member_id"]), None)
        if member is None:
            raise TeamError("team_member_not_found")
        if member.revision != args["expected_member_revision"]:
            raise TeamError("team_revision_conflict")
        states = {"pause": {"running", "awaiting_approval", "pausing", "paused"},
                  "resume": {"idle", "paused", "failed", "interrupted"},
                  "stop": {"created", "starting", "running", "awaiting_approval", "pausing", "paused", "resuming", "idle", "failed", "interrupted", "stopping"}}
        if member.role.value != "member" or member.state.value not in states[operation]:
            raise TeamError("team_state_invalid")
        if args.get("task_id"):
            from .tasks import TaskService
            task = TaskService(self.store, self.team_id).get("lead", args["task_id"])
            if task.assignee != member.member_id:
                raise TeamError("team_task_conflict")
            TaskService(self.store, self.team_id).is_ready(task.task_id)
        handle = self._handles.get(member.member_id)
        if handle is not None and handle[0]._closed and handle[0]._owner is None:
            self._handles.pop(member.member_id)
            handle = None
        if handle is None:
            if operation != "resume" or self.runner_factory is None:
                raise TeamError("team_backend_unavailable")
            prepared = self.runner_factory.prepare(member.agent_name)
            runner, backend = self.runner_factory.create(self, member, prepared)
            if hasattr(self, "_permission_snapshot"):
                runner.publish_permissions(self._permission_snapshot)
            self._handles[member.member_id] = (runner, backend)
            try:
                runner.resume(expected_revision=args["expected_member_revision"])
                backend.start(runner.scope)
            except Exception:
                runner.close()
                raise TeamError("team_backend_start_failed") from None
            return {"member_id": member.member_id, "state": "resuming"}
        runner, backend = handle
        if operation == "pause":
            runner.pause(expected_revision=args["expected_member_revision"])
        elif operation == "stop":
            runner.stop(expected_revision=args["expected_member_revision"])
            backend.request_stop(runner.scope)
            # SDK client.close 也不能拖住主终端；未真实退出的 runner 保留 native lease。
            def finish_stop():
                try:
                    runner.close()
                except Exception:
                    pass
            threading.Thread(target=finish_stop, name="newcode-team-stop", daemon=True).start()
            return {"member_id": member.member_id, "state": "stopping", "stopped": False}
        elif operation == "resume":
            runner.resume(expected_revision=args["expected_member_revision"])
            backend.wake(runner.scope)
        current = next(m for m in self.store.load_team(self.team_id).roster if m.member_id == member.member_id)
        return {"member_id": member.member_id, "state": current.state.value, "revision": current.revision}

    def _archive(self, args):
        from dataclasses import replace
        from .types import RecordChange, new_id
        from contextlib import ExitStack
        with ExitStack() as stack:
            team = self.store.load_team(self.team_id)
            for member in team.roster:
                if member.role.value == "member":
                    stack.enter_context(self._quiescent_member(member.member_id))
            if team.revision != args["expected_team_revision"]:
                raise TeamError("team_revision_conflict")
            # 与已批准状态机一致：先 closing，再 closed；中断后可明确恢复。
            updated = replace(team, state="closing", revision=team.revision + 1)
            self.store.commit_transaction(self.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
                changes=(RecordChange("team", self.team_id, updated.to_dict()),))
            updated = replace(updated, state="closed", revision=updated.revision + 1)
            self.store.commit_transaction(self.team_id, expected_revision=team.revision + 1, mutation_id=new_id("mutation"),
                changes=(RecordChange("team", self.team_id, updated.to_dict()),))
        return {"state": "closed"}

    def _wake(self, recipient):
        handle = self._handles.get(recipient)
        if handle:
            runner, backend = handle
            runner.wake(backend.wake)

    def detach(self):
        self._main_thread()
        self.generation += 1
        self.role, self.team_id = "ordinary", None
        self._session = None
        self._upper = self._current = None
        deadline = time.monotonic() + SHUTDOWN_SECONDS
        handles = [*self._retired, *self._handles.values()]
        self._handles.clear()
        jobs = []
        for runner, backend in handles:
            key = id(runner)
            job = self._closers.get(key)
            if job is None or (job["done"].is_set() and not job["closed"]):
                job = {"done": threading.Event(), "closed": False}
                def cleanup(runner=runner, backend=backend, job=job):
                    try:
                        try:
                            backend.request_stop(runner.scope)
                        finally:
                            backend.close(deadline=deadline)
                        backend.join_until(deadline)
                        job["closed"] = bool(runner.close())
                    except Exception:
                        pass
                    finally:
                        job["done"].set()
                thread = threading.Thread(target=cleanup, name="newcode-team-detach", daemon=True)
                job["thread"] = thread
                self._closers[key] = job
                thread.start()
            jobs.append((runner, backend, job))
        for _, _, job in jobs:
            job["thread"].join(max(0, deadline - time.monotonic()))
        self._retired = [(runner, backend) for runner, backend, job in jobs
                         if not job["done"].is_set() or not job["closed"]]
        self._closers = {id(runner): job for runner, backend, job in jobs
                         if not job["done"].is_set() or not job["closed"]}
        reset = getattr(self.runner_factory, "reset_backend", None)
        if callable(reset):
            reset()
        return len(self._retired)


class TeamRunnerFactory:
    """CLI 注入的成员构造端口；只使用 Phase 4 已有隔离 runner。"""
    def __init__(self, *, registry, catalog, provider_factory, worktree_manager, permissions, default_model):
        self.registry, self.catalog = registry, catalog
        self.provider_factory, self.worktrees = provider_factory, worktree_manager
        self.permissions, self.default_model = permissions, default_model
        self._runners = {}
        self._backend = None

    def prepare(self, agent_name):
        from newcode.subagents.types import AgentIsolation
        definition = next((d for d in self.catalog.definitions if d.name == agent_name), None)
        if definition is None or definition.isolation is not AgentIsolation.WORKTREE:
            raise TeamError("team_definition_invalid")
        if self.provider_factory is None or self.worktrees is None:
            raise TeamError("team_backend_unavailable")
        if (definition.model or self.default_model) not in frozenset(self.provider_factory.available_models):
            raise TeamError("team_backend_unavailable")
        return definition

    def reset_backend(self):
        self._backend = None

    def create(self, attachment, member, definition):
        from datetime import datetime
        from .member import TeamMemberRunner
        from .backends import LightweightBackend
        from .types import TeamPolicySnapshot
        # 同一 attachment 一个调度器，不为每个成员新建满额线程池。
        if self._backend is None:
            self._backend = LightweightBackend(lambda activation: self._runners[activation.scope](activation),
                max_workers=attachment.config.max_running_members, max_members=attachment.config.max_members)
        sid = datetime.now().strftime("%Y%m%d-%H%M%S-") + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
        sid = member.checkpoint_session_id or sid
        scope = TeamScope(attachment.team_id, member.member_id, sid, member.runtime_generation + 1)
        startup = frozenset(attachment._upper or ())
        current = startup & frozenset(attachment._current or ())
        policy = TeamPolicySnapshot(scope, 1, startup, current)
        runner = TeamMemberRunner(store=attachment.store, scope=scope, definition=definition, registry=self.registry,
            provider_factory=self.provider_factory, default_model=self.default_model, policy=policy,
            worktree_manager=self.worktrees, permissions=self.permissions, enable_team_tools=True)
        self._runners = {s: r for s, r in self._runners.items() if not r._closed or r._owner is not None}
        self._runners[scope] = runner
        return runner, self._backend


class MemberTeamRuntime:
    """成员工具在既有服务持锁的验证点重验 At，不嵌套获取文件锁。"""
    role = "member"

    def __init__(self, runner):
        self.runner = runner
        self._admission = threading.local()
        self.team_id, self.generation = runner.scope.team_id, runner.scope.generation
        self._cursor_key = self._mailbox_key = secrets.token_bytes(32)

    def publish(self, names, mode):
        # worker 不发布父策略；其每轮/工具读取 runner 已有不可变快照。
        self.runner.mode = mode

    def filter_tools(self, names):
        runner = self.runner
        with runner._policy_lock, runner.store.lock(runner.scope.team_id):
            member, view, _ = runner._member_locked()
            return frozenset(names) & runner._allowed_locked(member, view)

    def admission(self, name, arguments):
        from contextlib import contextmanager
        @contextmanager
        def admitted():
            previous = getattr(self._admission, "call", None)
            self._admission.call = (name, json.dumps(arguments, sort_keys=True))
            try:
                yield
            finally:
                self._admission.call = previous
        return admitted()

    def _check(self, view, name, op):
        from .types import TeamMemberRecord
        runner = self.runner
        runner._check_budget()
        member = TeamMemberRecord.from_dict(view["members"][runner.scope.member_id])
        if name not in runner._allowed_locked(member, view):
            raise TeamError("disallowed_tool")
        if name == "team_tasks" and op != "submit_plan" and not runner._approval_valid(member, view):
            raise TeamError("team_approval_required")
        return member

    def invoke(self, name, arguments, context):
        from .tools import validate_arguments
        from .tasks import TaskService
        from .approvals import ApprovalService
        from .mailbox import MailboxService
        from .store import sanitize, encode
        runner = self.runner
        op = validate_arguments(name, arguments, "member")
        if getattr(self._admission, "call", None) != (name, json.dumps(arguments, sort_keys=True)):
            raise TeamError("permission_denied")
        actor, team_id = runner.scope.member_id, runner.scope.team_id
        runtime = self

        class Tasks(TaskService):
            def _actor(self, view, member_id):
                runtime._check(view, name, op)
                return super()._actor(view, member_id)

        class Mailbox(MailboxService):
            def _require_actor(self, view):
                runtime._check(view, name, op)
                return super()._require_actor(view)

        class Plans(ApprovalService):
            def _actor(self, view, expected_role):
                runtime._check(view, name, op)
                return super()._actor(view, expected_role)

        with runner._policy_lock:
            from newcode.tools.types import ToolCall, ToolFailure
            try:
                runner.check_parent_permissions(ToolCall("team-check", name, dict(arguments)), runner.registry.get(name))
            except ToolFailure:
                raise TeamError("permission_denied") from None
            args = {k: v for k, v in arguments.items() if k != "operation"}
            if name == "team_tasks":
                if op == "submit_plan":
                    value = Plans(runner.store, team_id, actor).submit(**args).to_dict()
                else:
                    args["expected_revision"] = args.pop("expected_task_revision")
                    if op == "update":
                        args["changes"] = {"state": args["changes"]["status"]}
                    value = getattr(Tasks(runner.store, team_id), op)(actor, **args).to_dict()
                    if op == "complete":
                        value["verification"] = "not_verified"
            elif name == "team_message":
                service = Mailbox(runner.store, team_id, actor, cursor_key=self._mailbox_key)
                if op == "ack":
                    value = {"acked": service.ack(args["message_id"], session_id=runner.scope.session_id,
                                                  checkpoints=runner.checkpoints)}
                elif op == "read":
                    page = service.read(**args)
                    value = {"messages": [m.to_dict() for m in page.messages], "cursor": page.next_cursor}
                else:
                    result = getattr(service, op)(**args)
                    value = {"message_ids": [m.message_id for m in result.messages], "wake_failed": result.wake_failed}
            elif name == "team_status":
                with runner.store.lock(team_id):
                    _, view, _ = runner._member_locked()
                    self._check(view, name, op)
                    if op == "status":
                        value = {"role": "member", "team_id": team_id, "generation": runner.scope.generation}
                    elif op == "plan":
                        value = view.get("plans", {}).get(args["plan_id"])
                        if value is None:
                            raise TeamError("team_plan_not_found")
                        if value["member_id"] != actor:
                            raise TeamError("team_plan_access_denied")
                    else:
                        # read-only 同一 journal 快照；内容/身份不超出固定投影。
                        collection = {"roster": "members", "tasks": "tasks", "inbox": "messages"}[op]
                        values = list(view.get(collection, {}).values())
                        if op == "inbox":
                            values = [{k: v for k, v in m.items() if k in {"message_id", "sequence", "summary", "kind"}}
                                      for m in values if m["recipient"] == actor]
                        elif op == "roster":
                            values = [{k: v for k, v in m.items() if k in {"member_id", "role", "state", "revision"}}
                                      for m in values]
                        elif "task_id" in args:
                            values = [m for m in values if m["task_id"] == args["task_id"]]
                            if not values:
                                raise TeamError("team_task_not_found")
                        value = TeamAttachment._page(self, values, arguments, view["team"][team_id]["revision"])
            else:
                raise TeamError("team_role_denied")
        try:
            value = {**value, "team_revision": runner.store.load_team(team_id).revision}
        except TeamError:
            value = {**value, "team_revision": None, "team_revision_status": "unavailable"}
        safe = sanitize(value, runner.store.sensitive_values)
        if len(encode(safe)) > 16384:
            raise TeamError("team_limit_exceeded")
        return safe
