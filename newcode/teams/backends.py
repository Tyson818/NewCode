"""固定后端协议、选择与受控 pane 启动；probe 不创建运行资源。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Protocol, runtime_checkable

from .paths import canonical_directory, reject_links
from .runtime import (
    ExecutionSnapshot, LaunchIdentity, LightweightRuntime, PipeListener, ShutdownReport, _endpoint,
)
from .types import Backend, MAX_MEMBERS, SHUTDOWN_SECONDS, TeamError, TeamScope, freeze, thaw


WORKER_MODULE = "newcode.teams.worker"
WORKER_PROTOCOL = "newcode.team.worker.v1"


@dataclass(frozen=True)
class BackendCapability:
    backend: Backend
    available: bool
    stage: str
    error_code: str | None = None


@runtime_checkable
class TeamBackend(Protocol):
    def probe(self) -> BackendCapability: ...
    def start(self, member: TeamScope, runtime_bootstrap=None) -> ExecutionSnapshot: ...
    def wake(self, member: TeamScope) -> ExecutionSnapshot: ...
    def request_stop(self, member: TeamScope) -> ExecutionSnapshot: ...
    def join_until(self, deadline: float) -> ShutdownReport: ...
    def close(self, *, deadline: float | None = None) -> None: ...


def select_backend(backends, *, explicit=None, team_backend=None, user_default=None):
    selected = next((value for value in (explicit, team_backend, user_default, Backend.LIGHTWEIGHT)
                     if value is not None), Backend.LIGHTWEIGHT)
    try:
        selected = Backend(selected)
    except (TypeError, ValueError):
        raise TeamError("team_arguments_invalid") from None
    backend = backends.get(selected)
    if backend is None:
        raise TeamError("team_backend_unavailable")
    try:
        result = backend.probe()
    except Exception:
        raise TeamError("team_backend_unavailable") from None
    if not result.available or result.backend is not selected:
        raise TeamError("team_backend_unavailable")
    return backend


class LightweightBackend:
    def __init__(self, worker, **limits):
        self._worker, self._limits = worker, limits
        self._runtime = None
        self._lock = threading.RLock()
        self._closed = False

    def probe(self):
        return BackendCapability(Backend.LIGHTWEIGHT, callable(self._worker), "thread",
                                 None if callable(self._worker) else "team_backend_unavailable")

    def start(self, member, runtime_bootstrap=None):
        with self._lock:
            if self._closed:
                raise TeamError("team_state_invalid")
            if self._runtime is None:
                self._runtime = LightweightRuntime(self._worker, **self._limits)
            runtime = self._runtime
        return runtime.start(member, runtime_bootstrap)

    def wake(self, member):
        if self._runtime is None:
            raise TeamError("team_scope_invalid")
        return self._runtime.wake(member)

    def status(self, member):
        if self._runtime is None:
            raise TeamError("team_scope_invalid")
        return self._runtime.status(member)

    def request_stop(self, member):
        if self._runtime is None:
            raise TeamError("team_scope_invalid")
        return self._runtime.request_stop(member)

    def close(self, *, deadline=None):
        with self._lock:
            self._closed = True
            if self._runtime is not None:
                self._runtime.close(deadline=deadline)

    def join_until(self, deadline):
        return self._runtime.join_until(deadline) if self._runtime is not None else ShutdownReport()

    def shutdown(self):
        self.close()
        return self.join_until(time.monotonic() + SHUTDOWN_SECONDS)


@dataclass(frozen=True)
class PaneBootstrap:
    workspace_root: Path
    common_git_dir: Path
    user_home: Path
    runtime_config: object = field(default_factory=dict, repr=False)

    def __post_init__(self):
        for key in ("workspace_root", "common_git_dir", "user_home"):
            object.__setattr__(self, key, canonical_directory(getattr(self, key)))
        if not isinstance(self.runtime_config, dict):
            raise TeamError("team_arguments_invalid")
        object.__setattr__(self, "runtime_config", freeze(self.runtime_config))

    def packet(self, scope):
        return {"type": "bootstrap", "scope": scope.to_dict(),
                "workspace_root": str(self.workspace_root), "common_git_dir": str(self.common_git_dir),
                "user_home": str(self.user_home), "runtime_config": thaw(self.runtime_config)}


@dataclass(frozen=True)
class PaneHandle:
    pane_id: str
    pid: int

    def __post_init__(self):
        if not re.fullmatch(r"%[0-9]+", self.pane_id) or type(self.pid) is not int or self.pid < 0:
            raise TeamError("team_backend_start_failed")


class PaneLaunchError(TeamError):
    def __init__(self, handle):
        super().__init__("team_backend_start_failed")
        self.handle = handle


def bootstrap_environment():
    # 不继承 Provider/MCP secret、PYTHONPATH、shell 配置或代理环境。
    return {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMUX", "LANG")
            if key in os.environ}


class TmuxDriver:
    """tmux 多参数直接 exec；启动后的 OS peer/argv 握手是额外准入门。"""

    def __init__(self, *, python=None, installation_root=None, tmux=None):
        self.python = Path(python or sys.executable).absolute()
        self.installation_root = Path(installation_root or Path(__file__).parents[2]).absolute()
        self.worker_origin = self.installation_root / "newcode" / "teams" / "worker.py"
        self.worker_image = None
        self.tmux = tmux or shutil.which("tmux")

    def _run(self, arguments, *, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TeamError("team_backend_start_failed")
        try:
            return subprocess.run(arguments, cwd=self.installation_root, env=bootstrap_environment(),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  shell=False, timeout=min(2.0, remaining), check=False)
        except (OSError, subprocess.SubprocessError):
            raise TeamError("team_backend_start_failed") from None

    def worker_check(self):
        canonical_directory(self.installation_root)
        reject_links(self.python)
        reject_links(self.worker_origin)
        if not self.python.is_file() or not self.worker_origin.is_file():
            raise TeamError("team_backend_unavailable")
        completed = self._run([str(self.python), "-I", "-B", "-m", WORKER_MODULE, "--check"],
                              deadline=time.monotonic() + 2.0)
        try:
            data = json.loads(completed.stdout) if completed.returncode == 0 and len(completed.stdout) <= 8192 else {}
            if (data.get("protocol") != WORKER_PROTOCOL or data.get("isolated") is not True
                    or Path(data.get("origin", "")).resolve() != self.worker_origin.resolve()
                    or Path(data.get("python", "")).resolve() != self.python.resolve()):
                raise ValueError
            base = Path(data["base_python"])
            reject_links(base)
            if not base.is_absolute() or not base.is_file():
                raise ValueError
            self.worker_image = base.resolve(strict=True)
        except (ValueError, TypeError, OSError):
            raise TeamError("team_backend_unavailable") from None

    def identity(self, handle):
        if self.worker_image is None:
            raise TeamError("team_backend_unavailable")
        return LaunchIdentity(handle.pid, self.python, self.worker_image)

    def probe(self):
        if not self.tmux:
            return BackendCapability(Backend.PANE, False, "tmux_missing", "team_backend_unavailable")
        try:
            path = Path(self.tmux)
            if not path.is_absolute():
                raise TeamError("team_backend_unavailable")
            reject_links(path)
            version = self._run([str(path), "-V"], deadline=time.monotonic() + 2.0)
            match = re.fullmatch(rb"tmux ([0-9]+)\.([0-9]+)[a-z]?\s*", version.stdout)
            if version.returncode or not match or tuple(map(int, match.groups())) < (3, 2):
                raise TeamError("team_backend_unavailable")
            self.worker_check()
            if not PipeListener.supported():
                return BackendCapability(Backend.PANE, False, "ipc_unavailable", "team_backend_unavailable")
            return BackendCapability(Backend.PANE, True, "startup_validation_required")
        except Exception:
            return BackendCapability(Backend.PANE, False, "probe_failed", "team_backend_unavailable")

    def launch(self, arguments, *, deadline):
        result = self._run(
            [str(self.tmux), "split-window", "-d", "-P", "-F", "#{pane_id}",
             "-c", str(self.installation_root), "--", str(self.python),
             "-I", "-B", "-m", WORKER_MODULE, *arguments], deadline=deadline)
        try:
            pane_id = result.stdout.decode("ascii").strip()
            if result.returncode or not re.fullmatch(r"%[0-9]+", pane_id):
                raise ValueError
        except (ValueError, UnicodeError):
            raise TeamError("team_backend_start_failed") from None
        handle = PaneHandle(pane_id, 0)
        try:
            query = self._run([str(self.tmux), "display-message", "-p", "-t", pane_id, "-F", "#{pane_pid}"],
                              deadline=deadline)
            pid = int(query.stdout)
            if query.returncode or pid <= 0:
                raise ValueError
            return PaneHandle(pane_id, pid)
        except Exception:
            raise PaneLaunchError(handle) from None

    def alive(self, handle, *, deadline):
        try:
            result = self._run([str(self.tmux), "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}"],
                               deadline=deadline)
            if result.returncode:
                return None
            records = {}
            for line in result.stdout.decode("ascii").splitlines():
                pane, pid = line.split(":")
                if not re.fullmatch(r"%[0-9]+", pane) or not pid.isdecimal() or pane in records:
                    return None
                records[pane] = int(pid)
            if handle.pane_id not in records:
                return False
            return True if handle.pid > 0 and records[handle.pane_id] == handle.pid else None
        except Exception:
            return None

    def cleanup(self, handle, *, deadline):
        alive = self.alive(handle, deadline=deadline)
        if alive is False:
            return True
        if alive is not True:
            return False
        try:
            result = self._run([str(self.tmux), "kill-pane", "-t", handle.pane_id], deadline=deadline)
            return result.returncode == 0 and self.alive(handle, deadline=deadline) is False
        except Exception:
            return False


@dataclass
class _Pane:
    scope: TeamScope
    handle: PaneHandle | None = None
    channel: object | None = field(default=None, repr=False)
    listener: object | None = field(default=None, repr=False)
    state: str = "starting"
    error_code: str | None = None


class PaneBackend:
    """可注入平台 driver 供本地测试；未通过真实 probe 时不启动 pane。"""

    def __init__(self, *, driver=None, listener_factory=PipeListener):
        self.driver = driver or TmuxDriver()
        self._listener_factory = listener_factory
        self._members = {}
        self._lock = threading.RLock()
        self._closed = False
        self._deadline = None
        self._closing = threading.Event()
        self._close_lock = threading.Lock()
        self._active_scopes = ()

    def probe(self):
        try:
            return self.driver.probe()
        except Exception:
            return BackendCapability(Backend.PANE, False, "probe_failed", "team_backend_unavailable")

    @staticmethod
    def _key(scope):
        if not isinstance(scope, TeamScope):
            raise TeamError("team_scope_invalid")
        return scope.team_id, scope.member_id

    def start(self, member, runtime_bootstrap=None):
        if not isinstance(runtime_bootstrap, PaneBootstrap):
            raise TeamError("team_arguments_invalid")
        with self._lock:
            key = self._key(member)
            if self._closed or self._closing.is_set():
                raise TeamError("team_state_invalid")
            if key in self._members and self._members[key].state != "stopped":
                raise TeamError("team_member_busy")
            if sum(record.state != "stopped" for record in self._members.values()) >= MAX_MEMBERS:
                raise TeamError("team_limit_exceeded")
            if not self.probe().available:
                raise TeamError("team_backend_unavailable")
            if self._closing.is_set():
                raise TeamError("team_state_invalid")
            record = _Pane(member)
            self._members[key] = record
            self._refresh_scopes()
            listener = None
            deadline = time.monotonic() + 2.0
            try:
                listener = self._listener_factory()
                record.listener = listener
                _endpoint(listener.endpoint)
                arguments = ("--team-id", member.team_id, "--member-id", member.member_id,
                             "--endpoint", listener.endpoint, "--generation", str(member.generation))
                record.handle = self.driver.launch(arguments, deadline=deadline)
                identity = self.driver.identity(record.handle) if hasattr(self.driver, "identity") else record.handle.pid
                record.channel = listener.accept(identity, deadline=deadline)
                hello = record.channel.receive(deadline=deadline)
                peer_pid = record.channel.peer_pid
                if (not isinstance(identity, LaunchIdentity) and peer_pid != record.handle.pid
                        or hello != {"type": "hello", "protocol": WORKER_PROTOCOL,
                                     "team_id": member.team_id, "member_id": member.member_id,
                                     "generation": member.generation, "pid": peer_pid,
                                     "origin": str(self.driver.worker_origin.resolve()),
                                     "isolated": True, "arguments": list(arguments)}):
                    raise TeamError("team_backend_start_failed")
                if self._closing.is_set():
                    raise TeamError("team_backend_start_failed")
                record.channel.send(runtime_bootstrap.packet(member), deadline=deadline)
                ready = record.channel.receive(deadline=deadline)
                if ready != {"type": "ready", "pid": peer_pid, "scope": member.to_dict()}:
                    raise TeamError("team_backend_start_failed")
                if self._closing.is_set():
                    raise TeamError("team_backend_start_failed")
                listener.close()
                record.listener = None
                record.state = "idle"
                return self._snapshot(record)
            except BaseException as exc:
                if isinstance(exc, PaneLaunchError):
                    record.handle = exc.handle
                channels_cleaned = self._cleanup_channels(record)
                cleaned = record.handle is None
                if record.handle is not None:
                    try:
                        cleaned = self.driver.cleanup(record.handle, deadline=time.monotonic() + 0.2)
                    except Exception:
                        cleaned = False
                if cleaned and channels_cleaned:
                    del self._members[key]
                    self._refresh_scopes()
                else:
                    record.state = "stopping"
                    record.error_code = "team_backend_start_failed"
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise TeamError("team_backend_start_failed") from None

    def wake(self, member):
        with self._lock:
            record = self._member(member)
            if self._closed or record.state not in {"idle", "running", "queued"}:
                raise TeamError("team_state_invalid")
            try:
                deadline = time.monotonic() + 0.2
                record.channel.send({"type": "wake", "scope": member.to_dict()}, deadline=deadline)
                response = record.channel.receive(deadline=deadline)
                if response.get("type") != "status" or response.get("state") not in {"running", "queued", "idle", "failed"}:
                    raise TeamError("team_worker_failed")
                record.state = response["state"]
                if record.state == "failed":
                    record.error_code = "team_worker_failed"
                return self._snapshot(record)
            except Exception:
                record.error_code = "team_worker_failed"
                raise TeamError("team_worker_failed") from None

    def request_stop(self, member):
        with self._lock:
            record = self._member(member)
            self._stop(record, self._deadline or time.monotonic() + 0.05)
            return self._snapshot(record)

    def _stop(self, record, deadline):
        if record.state == "stopped":
            return
        record.state = "stopping"
        if record.channel is not None:
            try:
                record.channel.send({"type": "stop", "scope": record.scope.to_dict()}, deadline=deadline)
            except Exception:
                record.error_code = "team_worker_failed"

    def close(self, *, deadline=None):
        self._closing.set()
        with self._close_lock:
            if self._deadline is None:
                self._deadline = min(time.monotonic() + SHUTDOWN_SECONDS,
                                     deadline if deadline is not None else float("inf"))
            elif deadline is not None:
                self._deadline = min(self._deadline, deadline)
        if not self._lock.acquire(timeout=max(0, self._deadline - time.monotonic())):
            return
        try:
            if not self._closed:
                self._closed = True
                for record in self._members.values():
                    self._stop(record, self._deadline)
        finally:
            self._lock.release()

    def join_until(self, deadline):
        end = min(deadline, self._deadline or time.monotonic() + SHUTDOWN_SECONDS)
        if not self._lock.acquire(timeout=max(0, end - time.monotonic())):
            pending = self._active_scopes
            return ShutdownReport(pending, "team_shutdown_timeout" if pending else None)
        try:
            while time.monotonic() < end:
                active = []
                for record in self._members.values():
                    if record.state == "stopped":
                        continue
                    try:
                        alive = self.driver.alive(record.handle, deadline=end) if record.handle else None
                    except Exception:
                        alive = None
                    if alive is False:
                        if self._cleanup_channels(record):
                            record.state = "stopped"
                            self._refresh_scopes()
                        else:
                            record.error_code = "team_worker_failed"
                            active.append(record.scope)
                    else:
                        active.append(record.scope)
                if not active:
                    return ShutdownReport()
                time.sleep(min(0.005, max(0, end - time.monotonic())))
            pending = tuple(record.scope for record in self._members.values() if record.state != "stopped")
            return ShutdownReport(pending, "team_shutdown_timeout" if pending else None)
        finally:
            self._lock.release()

    def shutdown(self):
        self.close()
        return self.join_until(self._deadline)

    def _member(self, scope):
        record = self._members.get(self._key(scope))
        if record is None or record.scope != scope:
            raise TeamError("team_scope_invalid")
        return record

    def _refresh_scopes(self):
        self._active_scopes = tuple(record.scope for record in self._members.values() if record.state != "stopped")

    @staticmethod
    def _cleanup_channels(record):
        cleaned = True
        for name in ("channel", "listener"):
            resource = getattr(record, name)
            if resource is not None:
                try:
                    resource.close()
                    setattr(record, name, None)
                except Exception:
                    cleaned = False
        return cleaned

    @staticmethod
    def _snapshot(record):
        return ExecutionSnapshot(record.scope, record.state,
                                 record.state in {"running", "stopping", "starting"},
                                 record.state == "queued", record.error_code)
