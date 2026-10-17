"""Phase 3 使用 callback/本地进程；不创建成员 Agent 或 Provider。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from newcode.teams.backends import (
    BackendCapability, LightweightBackend, PaneBackend, PaneBootstrap, PaneHandle,
    PaneLaunchError, TeamBackend, TmuxDriver, WORKER_MODULE, WORKER_PROTOCOL,
    bootstrap_environment, select_backend,
)
from newcode.teams.runtime import (
    ExecutorLeases, LaunchIdentity, PipeListener, ShutdownReport, StoreExecutorLeases,
    TeamRuntime, connect_pipe, verify_pipe_acl,
)
from newcode.teams.store import TeamStore
from newcode.teams.types import Backend, TeamError, TeamScope, new_id
from newcode.teams.worker import parse_arguments
from test_teams_paths import make_paths


def scope(name="alice", *, team_id=None, generation=1):
    return TeamScope(team_id or new_id("team"), name, "20261016-010101-ab12", generation)


def eventually(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "等待受控fixture超过限时"


class ProbeOnly:
    def __init__(self, backend, available=True):
        self.backend, self.available, self.calls = backend, available, 0

    def probe(self):
        self.calls += 1
        return BackendCapability(self.backend, self.available, "fixture",
                                 None if self.available else "team_backend_unavailable")


@pytest.mark.parametrize(("explicit", "team", "user", "selected"), [
    ("pane", "lightweight", "lightweight", Backend.PANE),
    (None, "pane", "lightweight", Backend.PANE),
    (None, None, "pane", Backend.PANE),
    (None, None, None, Backend.LIGHTWEIGHT),
])
def test_backend_precedence_probes_only_selected(explicit, team, user, selected):
    backends = {backend: ProbeOnly(backend) for backend in Backend}
    assert select_backend(backends, explicit=explicit, team_backend=team, user_default=user) is backends[selected]
    assert backends[selected].calls == 1
    assert sum(item.calls for item in backends.values()) == 1


def test_explicit_unavailable_does_not_fallback_or_start_threads():
    light = LightweightBackend(lambda _context: None)
    unavailable = ProbeOnly(Backend.PANE, False)
    before = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(TeamError, match="team_backend_unavailable"):
        select_backend({Backend.PANE: unavailable, Backend.LIGHTWEIGHT: light}, explicit="pane")
    assert light.probe().available
    assert {thread.ident for thread in threading.enumerate()} == before
    assert isinstance(light, TeamBackend)
    light.close()


@pytest.mark.parametrize("limits", [{"max_workers": 5}, {"max_members": 9},
                                  {"queue_capacity": 17}, {"activation_timeout": 301},
                                  {"activation_timeout": "invalid"}])
def test_invalid_backend_limits_fail_before_thread_creation(limits):
    before = {thread.ident for thread in threading.enumerate()}
    backend = LightweightBackend(lambda _context: None, **limits)
    with pytest.raises(TeamError, match="team_arguments_invalid"):
        backend.start(scope())
    assert {thread.ident for thread in threading.enumerate()} == before
    backend.close()


def test_probe_timeout_and_bootstrap_environment_are_safe(monkeypatch):
    driver = TmuxDriver(tmux=sys.executable)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key-not-for-argv")
    monkeypatch.setenv("HTTP_PROXY", "http://private-credential@proxy")
    monkeypatch.setenv("PYTHONPATH", "untrusted-member-root")
    assert not {"DEEPSEEK_API_KEY", "HTTP_PROXY", "PYTHONPATH"} & set(bootstrap_environment())

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private-command-secret", 0.01, stderr=b"private-error")

    monkeypatch.setattr(subprocess, "run", timeout)
    result = driver.probe()
    assert not result.available and result.error_code == "team_backend_unavailable"
    assert "private" not in repr(result)
    with pytest.raises(TeamError, match="team_backend_start_failed") as error:
        driver._run([sys.executable], deadline=time.monotonic() + 1)
    assert "private" not in str(error.value)


def test_partial_thread_start_failure_releases_only_new_threads(monkeypatch):
    original = threading.Thread.start
    started = []

    def fail_second(thread):
        if thread.name == "newcode-team-1":
            raise RuntimeError("private-thread-start-error")
        original(thread)
        if thread.name.startswith("newcode-team"):
            started.append(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_second)
    backend = LightweightBackend(lambda _context: pytest.fail("启动失败时零callback"))
    with pytest.raises(TeamError, match="team_backend_start_failed"):
        backend.start(scope())
    assert started and all(not thread.is_alive() for thread in started)
    assert not backend.shutdown().pending


def test_idle_members_release_worker_slots_for_other_members():
    seen = []
    lock = threading.Lock()

    def callback(context):
        with lock:
            seen.append(context.scope.member_id)

    backend = LightweightBackend(callback, max_workers=2)
    team = new_id("team")
    try:
        members = [scope(f"member{i}", team_id=team) for i in range(8)]
        for member in members:
            backend.start(member)
            eventually(lambda: backend.status(member).state == "idle")
        assert seen == [member.member_id for member in members]
        with pytest.raises(TeamError, match="team_limit_exceeded"):
            backend.start(scope("ninth", team_id=team))
        assert all(not backend.status(member).executing for member in members)
        assert len(backend._runtime._threads) == 2
    finally:
        assert not backend.shutdown().pending


def test_running_wakes_coalesce_and_are_serial_and_fifo():
    entered, release = threading.Event(), threading.Event()
    calls, active, maximum = [], 0, 0
    lock = threading.Lock()
    team = new_id("team")
    alice, bob = scope("alice", team_id=team), scope("bob", team_id=team)

    def callback(context):
        nonlocal active, maximum
        with lock:
            calls.append(context.scope.member_id)
            active += 1
            maximum = max(maximum, active)
        if context.scope.member_id == "alice" and len(calls) == 1:
            entered.set()
            assert release.wait(3)
        with lock:
            active -= 1

    backend = LightweightBackend(callback, max_workers=1)
    try:
        backend.start(alice)
        assert entered.wait(2)
        backend.start(bob)
        for _ in range(20):
            assert backend.wake(alice).wake_pending
        release.set()
        eventually(lambda: len(calls) == 3 and backend.status(alice).state == "idle")
        assert calls == ["alice", "bob", "alice"] and maximum == 1
    finally:
        release.set()
        backend.shutdown()


def test_queue_capacity_duplicate_wakes_and_cancel_queued_member():
    entered, release = threading.Event(), threading.Event()
    team = new_id("team")
    alice, bob, charlie = [scope(name, team_id=team) for name in ("alice", "bob", "charlie")]

    def callback(context):
        if context.scope == alice:
            entered.set()
            release.wait(3)

    backend = LightweightBackend(callback, max_workers=1, queue_capacity=1)
    try:
        backend.start(alice)
        assert entered.wait(2)
        backend.start(bob)
        for _ in range(4):
            assert backend.wake(bob).state == "queued"
        with pytest.raises(TeamError, match="team_limit_exceeded"):
            backend.start(charlie)
        with pytest.raises(TeamError, match="team_limit_exceeded"):
            backend.wake(alice)
        assert backend.request_stop(bob).state in {"stopping", "stopped"}
        eventually(lambda: backend.status(bob).state == "stopped")
        backend.start(charlie)
        release.set()
        eventually(lambda: backend.status(charlie).state == "idle")
    finally:
        release.set()
        backend.shutdown()


def test_cancel_does_not_release_executor_until_callback_really_returns():
    entered, release = threading.Event(), threading.Event()
    member = scope()
    newer = replace(member, generation=2)
    leases = ExecutorLeases()

    def callback(context):
        entered.set()
        release.wait(4)

    first = LightweightBackend(callback, leases=leases)
    second = LightweightBackend(lambda _context: None, leases=leases)
    try:
        first.start(member)
        assert entered.wait(2)
        stopped = first.request_stop(member)
        assert stopped.state == "stopping" and stopped.executing
        with pytest.raises(TeamError, match="team_member_busy"):
            second.start(newer)
        release.set()
        eventually(lambda: first.status(member).state == "stopped")
        second.start(newer)
        eventually(lambda: second.status(newer).state == "idle")
        with pytest.raises(TeamError, match="team_scope_invalid"):
            second.wake(member)
    finally:
        release.set()
        first.shutdown()
        second.shutdown()


def test_exception_isolated_and_failed_member_not_automatically_restarted():
    team = new_id("team")
    alice, bob = scope("alice", team_id=team), scope("bob", team_id=team)

    def callback(context):
        if context.scope == alice:
            raise RuntimeError("private-provider-secret")

    backend = LightweightBackend(callback, max_workers=1)
    try:
        backend.start(alice)
        backend.start(bob)
        eventually(lambda: backend.status(alice).state == "failed" and backend.status(bob).state == "idle")
        assert backend.status(alice).error_code == "team_worker_failed"
        assert "private-provider-secret" not in repr(backend.status(alice))
        with pytest.raises(TeamError, match="team_state_invalid"):
            backend.wake(alice)
    finally:
        backend.shutdown()


def test_activation_timeout_requests_cancel_but_keeps_inflight_ownership():
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    member = scope()

    def callback(context):
        entered.set()
        assert context.cancel_event.wait(2)
        cancelled.set()
        release.wait(3)

    backend = LightweightBackend(callback, activation_timeout=0.03)
    try:
        backend.start(member)
        assert entered.wait(2) and cancelled.wait(2)
        snapshot = backend.status(member)
        assert snapshot.executing and snapshot.state == "stopping"
        with pytest.raises(TeamError, match="team_member_busy"):
            backend.start(member)
        release.set()
        eventually(lambda: not backend.status(member).executing)
    finally:
        release.set()
        backend.shutdown()


def test_shutdown_has_one_global_second_and_reports_still_running():
    release = threading.Event()
    entered = []
    lock = threading.Lock()
    team = new_id("team")

    def callback(context):
        with lock:
            entered.append(context.scope)
        release.wait(5)

    backend = LightweightBackend(callback)
    members = [scope(f"worker{i}", team_id=team) for i in range(4)]
    try:
        for member in members:
            backend.start(member)
        eventually(lambda: len(entered) == 4)
        begin = time.monotonic()
        report = backend.shutdown()
        elapsed = time.monotonic() - begin
        assert report.error_code == "team_shutdown_timeout" and set(report.pending) == set(members)
        assert 0.85 <= elapsed < 1.3
        assert all(backend.status(member).executing for member in members)
        begin = time.monotonic()
        assert backend.shutdown().pending
        assert time.monotonic() - begin < 0.1
    finally:
        release.set()
        eventually(lambda: all(not backend.status(member).executing for member in members))


def test_shutdown_during_slow_lease_acquisition_is_bounded_and_never_starts_callback():
    entered, release = threading.Event(), threading.Event()
    registry = ExecutorLeases()
    calls = []
    member = scope()

    class SlowLease:
        def acquire(self, current):
            entered.set()
            assert release.wait(4)
            return registry.acquire(current)

    backend = LightweightBackend(lambda context: calls.append(context.scope), leases=SlowLease())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(backend.start, member)
        try:
            assert entered.wait(2)
            begin = time.monotonic()
            result = backend.shutdown()
            assert time.monotonic() - begin < 1.3
            assert result.pending == (member,) and result.error_code == "team_shutdown_timeout"
            assert backend.status(member).state == "starting"
        finally:
            release.set()
        with pytest.raises(TeamError, match="team_state_invalid"):
            future.result(timeout=3)
    eventually(lambda: backend.status(member).state == "stopped")
    assert calls == []


def test_shutdown_does_not_block_on_lease_release_or_falsely_report_stopped():
    closing, release = threading.Event(), threading.Event()
    member = scope()

    class Lease:
        def __exit__(self, *_):
            closing.set()
            assert release.wait(4)

    class Leases:
        def acquire(self, _scope):
            return Lease()

    backend = LightweightBackend(lambda _context: None, leases=Leases())
    try:
        backend.start(member)
        eventually(lambda: backend.status(member).state == "idle")
        backend.request_stop(member)
        assert closing.wait(2)
        begin = time.monotonic()
        result = backend.shutdown()
        assert time.monotonic() - begin < 1.3
        assert result.pending == (member,) and backend.status(member).state == "stopping"
    finally:
        release.set()
        eventually(lambda: backend.status(member).state == "stopped")


def test_isolated_worker_module_check_actually_runs_without_creating_files(tmp_path):
    before = list(tmp_path.iterdir())
    result = subprocess.run([sys.executable, "-I", "-B", "-m", WORKER_MODULE, "--check"],
                            cwd=tmp_path, env=bootstrap_environment(), capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0 and not result.stderr
    value = json.loads(result.stdout)
    assert value["protocol"] == WORKER_PROTOCOL and value["isolated"] is True
    assert Path(value["origin"]).resolve() == Path("newcode/teams/worker.py").resolve()
    assert list(tmp_path.iterdir()) == before
    TmuxDriver().worker_check()


def test_worker_invalid_arguments_are_safe_and_not_shell_interpreted(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    malicious = f"alice; echo injected > {sentinel}"
    result = subprocess.run([sys.executable, "-I", "-B", "-m", WORKER_MODULE,
                             "--member-id", malicious], capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "injected" not in result.stderr and "Traceback" not in result.stderr
    assert not sentinel.exists()
    with pytest.raises(TeamError):
        parse_arguments(["--check", "--generation", "1"])


def test_tmux_launch_uses_individual_arguments_without_shell_strings(tmp_path):
    driver = TmuxDriver(python=tmp_path / "python space;$'exe", installation_root=tmp_path,
                        tmux=str(tmp_path / "tmux"))
    calls = []
    arguments = ("--team-id", new_id("team"), "--member-id", "alice",
                 "--endpoint", r"\\.\pipe\newcode-team-" + "a" * 32, "--generation", "1")

    def run(argv, *, deadline):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"%12\n" if len(calls) == 1 else b"123\n")

    driver._run = run
    handle = driver.launch(arguments, deadline=time.monotonic() + 1)
    assert handle == PaneHandle("%12", 123)
    assert calls[0][-len(arguments):] == list(arguments)
    assert calls[0][calls[0].index("--") + 1] == str(driver.python)
    assert not any(item in {"sh", "-c", "cmd", "/c", "send-keys"} for item in calls[0][10:])


class FakeDriver:
    worker_origin = Path("newcode/teams/worker.py").resolve()

    def __init__(self, stage=None, cleanup_ok=True):
        self.stage, self.cleanup_ok = stage, cleanup_ok
        self.calls, self.handles, self.cleaned = [], {}, []

    def probe(self):
        return BackendCapability(Backend.PANE, self.stage != "probe", "fixture",
                                 "team_backend_unavailable" if self.stage == "probe" else None)

    def launch(self, arguments, *, deadline):
        self.calls.append(arguments)
        handle = PaneHandle(f"%{len(self.calls)}", 1000 + len(self.calls))
        self.handles[handle.pane_id] = True
        if self.stage == "launch":
            raise PaneLaunchError(handle)
        return handle

    def alive(self, handle, *, deadline):
        return self.handles.get(handle.pane_id, False)

    def cleanup(self, handle, *, deadline):
        self.cleaned.append(handle.pane_id)
        if self.cleanup_ok:
            self.handles[handle.pane_id] = False
        return self.cleanup_ok


class FakeChannel:
    def __init__(self, driver, expected_pid):
        self.driver, self.peer_pid = driver, expected_pid
        self.receives, self.sent, self.closed = 0, [], False
        if driver.stage == "peer":
            self.peer_pid += 1

    def receive(self, *, deadline):
        self.receives += 1
        args = self.driver.calls[-1]
        options = parse_arguments(list(args))
        if self.receives == 1:
            result = {"type": "hello", "protocol": WORKER_PROTOCOL, "team_id": options.team_id,
                      "member_id": options.member_id, "generation": options.generation,
                      "pid": self.peer_pid, "origin": str(self.driver.worker_origin),
                      "isolated": True, "arguments": list(args)}
            if self.driver.stage == "hello":
                result["arguments"] = ["changed"]
            if self.driver.stage == "generation":
                result["generation"] += 1
            return result
        if self.driver.stage in {"ready", "lease"}:
            return {"type": "error", "error_code": "team_member_busy"}
        return {"type": "ready", "pid": self.peer_pid, "scope": self.sent[-1]["scope"]}

    def send(self, packet, *, deadline):
        self.sent.append(packet)
        if packet["type"] == "stop":
            self.driver.handles[f"%{self.peer_pid - 1000}"] = False

    def close(self):
        self.closed = True


class FakeListener:
    def __init__(self, driver):
        self.driver, self.closed, self.channel = driver, False, None
        self.endpoint = r"\\.\pipe\newcode-team-" + "a" * 32

    def accept(self, pid, *, deadline):
        if self.driver.stage == "accept":
            raise TeamError("team_backend_start_failed")
        self.channel = FakeChannel(self.driver, pid)
        return self.channel

    def close(self):
        self.closed = True


def fake_pane(driver):
    listeners = []

    def factory():
        if driver.stage == "listener":
            raise TeamError("team_backend_start_failed")
        listener = FakeListener(driver)
        listeners.append(listener)
        return listener
    return PaneBackend(driver=driver, listener_factory=factory), listeners


@pytest.mark.parametrize("stage", ["listener", "launch", "accept", "peer", "hello", "generation", "ready", "lease"])
def test_pane_start_failure_cleans_only_current_resources(tmp_path, stage):
    driver = FakeDriver()
    backend, listeners = fake_pane(driver)
    paths = make_paths(tmp_path)
    bootstrap = PaneBootstrap(paths.workspace_root, paths.common_git_dir, paths.home,
                              {"api_key": "fixture-runtime-secret"})
    first = scope()
    backend.start(first, bootstrap)
    driver.stage = stage
    second = scope("bob", team_id=first.team_id)
    with pytest.raises(TeamError, match="team_backend_start_failed"):
        backend.start(second, bootstrap)
    assert driver.handles["%1"] is True and "%1" not in driver.cleaned
    assert all("fixture-runtime-secret" not in repr(args) for args in driver.calls)
    assert all(listener.closed for listener in listeners)
    if stage in {"peer", "hello", "generation"}:
        assert listeners[-1].channel.sent == []  # 未验证peer前零secret发送
    driver.stage = None
    assert not backend.shutdown().pending


def test_pane_cleanup_unknown_preserves_ownership_and_blocks_takeover(tmp_path):
    paths = make_paths(tmp_path)
    driver = FakeDriver(stage="hello", cleanup_ok=False)
    backend, _ = fake_pane(driver)
    member = scope()
    bootstrap = PaneBootstrap(paths.workspace_root, paths.common_git_dir, paths.home)
    with pytest.raises(TeamError, match="team_backend_start_failed"):
        backend.start(member, bootstrap)
    with pytest.raises(TeamError, match="team_member_busy"):
        backend.start(replace(member, generation=2), bootstrap)
    assert driver.handles["%1"]
    report = backend.shutdown()
    assert report.pending == (member,) and report.error_code == "team_shutdown_timeout"


def test_pane_shutdown_during_startup_lock_wait_uses_same_global_deadline(tmp_path):
    entered, release = threading.Event(), threading.Event()
    paths = make_paths(tmp_path)
    driver = FakeDriver()

    class SlowListener(FakeListener):
        def accept(self, pid, *, deadline):
            entered.set()
            assert release.wait(4)
            return super().accept(pid, deadline=deadline)

    backend = PaneBackend(driver=driver, listener_factory=lambda: SlowListener(driver))
    member = scope()
    bootstrap = PaneBootstrap(paths.workspace_root, paths.common_git_dir, paths.home)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(backend.start, member, bootstrap)
        try:
            assert entered.wait(2)
            begin = time.monotonic()
            result = backend.shutdown()
            assert time.monotonic() - begin < 1.3
            assert result.pending == (member,) and result.error_code == "team_shutdown_timeout"
        finally:
            release.set()
        with pytest.raises(TeamError, match="team_backend_start_failed"):
            future.result(timeout=3)
    assert driver.cleaned == ["%1"] and not driver.handles["%1"]


def test_pane_channel_cleanup_exception_still_cleans_owned_pane_and_preserves_unknown(tmp_path):
    paths = make_paths(tmp_path)
    driver = FakeDriver(stage="hello")

    class BadChannel(FakeChannel):
        def close(self):
            raise RuntimeError("private-cleanup-secret")

    class Listener(FakeListener):
        def accept(self, pid, *, deadline):
            self.channel = BadChannel(driver, pid)
            return self.channel

    backend = PaneBackend(driver=driver, listener_factory=lambda: Listener(driver))
    member = scope()
    bootstrap = PaneBootstrap(paths.workspace_root, paths.common_git_dir, paths.home)
    with pytest.raises(TeamError, match="team_backend_start_failed") as error:
        backend.start(member, bootstrap)
    assert "private-cleanup-secret" not in str(error.value)
    assert driver.cleaned == ["%1"] and not driver.handles["%1"]
    with pytest.raises(TeamError, match="team_member_busy"):
        backend.start(member, bootstrap)
    result = backend.shutdown()
    assert result.pending == (member,) and result.error_code == "team_shutdown_timeout"


def test_mixed_backends_share_one_shutdown_second_and_failure_does_not_stop_following_cleanup(tmp_path):
    entered, release = threading.Event(), threading.Event()
    light_member, pane_member = scope(), scope("bob")

    def callback(_context):
        entered.set()
        release.wait(5)

    light = LightweightBackend(callback)
    driver = FakeDriver()
    pane, listeners = fake_pane(driver)
    paths = make_paths(tmp_path)
    pane.start(pane_member, PaneBootstrap(paths.workspace_root, paths.common_git_dir, paths.home))
    # fixture模拟不响应stop的pane；未退出前仍属于原执行者。
    listeners[0].channel.send = lambda packet, **kwargs: None
    light.start(light_member)
    assert entered.wait(2)
    runtime = TeamRuntime([light, pane])
    try:
        begin = time.monotonic()
        report = runtime.shutdown()
        assert time.monotonic() - begin < 1.3
        assert set(report.pending) == {light_member, pane_member}
        assert report.error_code == "team_shutdown_timeout"
        begin = time.monotonic()
        runtime.shutdown()
        assert time.monotonic() - begin < 0.1
    finally:
        release.set()
        eventually(lambda: light.status(light_member).state == "stopped")
        driver.handles["%1"] = False

    calls = []

    class Broken:
        def close(self, *, deadline):
            calls.append(("broken", deadline))
            raise RuntimeError("private cleanup details")

        def join_until(self, deadline):
            raise RuntimeError("private join details")

    class Good:
        def close(self, *, deadline):
            calls.append(("good", deadline))

        def join_until(self, deadline):
            return ShutdownReport()

    result = TeamRuntime([Broken(), Good()]).shutdown()
    assert result.error_code == "team_worker_failed"
    assert calls[0][1] == calls[1][1] and [name for name, _ in calls] == ["broken", "good"]


def test_production_pane_probe_without_tmux_is_unavailable_without_resource_creation(monkeypatch):
    driver = TmuxDriver()
    driver.tmux = None
    monkeypatch.setattr(driver, "launch", lambda *_args, **_kw: pytest.fail("probe不得创建pane"))
    assert driver.probe() == BackendCapability(Backend.PANE, False, "tmux_missing", "team_backend_unavailable")


WINDOWS_IPC = pytest.mark.skipif(os.name != "nt", reason="本用例验证Windows实际named-pipe ACL/PID；非Windows未验证")


def ipc_child(endpoint, script):
    return subprocess.Popen([sys.executable, "-I", "-B", "-c", script, endpoint],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=bootstrap_environment())


def child_identity(child):
    return LaunchIdentity(child.pid, Path(sys.executable), Path(sys._base_executable))


@WINDOWS_IPC
def test_actual_named_pipe_acl_peer_pid_and_in_memory_secret():
    listener = PipeListener()
    verify_pipe_acl(listener._handle)
    child = ipc_child(listener.endpoint, """
import hashlib, sys, time
from newcode.teams.runtime import connect_pipe
channel = connect_pipe(sys.argv[1], deadline=time.monotonic()+3)
channel.send({"type":"hello"}, deadline=time.monotonic()+3)
packet = channel.receive(deadline=time.monotonic()+3)
channel.send({"digest":hashlib.sha256(packet["value"].encode()).hexdigest()}, deadline=time.monotonic()+3)
channel.close()
""")
    channel = None
    try:
        channel = listener.accept(child_identity(child), deadline=time.monotonic() + 3)
        assert child_identity(child).matches(channel.peer_pid)
        assert channel.receive(deadline=time.monotonic() + 3) == {"type": "hello"}
        marker = "fake-bootstrap-secret-only-in-ipc"
        with pytest.raises(TeamError, match="team_limit_exceeded"):
            channel.send({"value": "a" * 65536}, deadline=time.monotonic() + 1)
        channel.send({"value": marker}, deadline=time.monotonic() + 3)
        assert channel.receive(deadline=time.monotonic() + 3) == {"digest": hashlib.sha256(marker.encode()).hexdigest()}
        stdout, stderr = child.communicate(timeout=4)
        assert child.returncode == 0 and not stdout and not stderr
    finally:
        if channel:
            channel.close()
        listener.close()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=3)


@WINDOWS_IPC
def test_actual_named_pipe_wrong_pid_rejected_and_accept_timeout_is_bounded():
    listener = PipeListener()
    child = ipc_child(listener.endpoint, """
import sys, time
from newcode.teams.runtime import connect_pipe
c = connect_pipe(sys.argv[1], deadline=time.monotonic()+3)
time.sleep(0.05)
c.close()
""")
    try:
        with pytest.raises(TeamError, match="team_backend_start_failed"):
            listener.accept(os.getpid(), deadline=time.monotonic() + 3)
        child.communicate(timeout=4)
    finally:
        listener.close()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=3)
    listener = PipeListener()
    begin = time.monotonic()
    try:
        with pytest.raises(TeamError):
            listener.accept(os.getpid(), deadline=begin + 0.03)
        assert time.monotonic() - begin < 0.3
    finally:
        listener.close()


def actual_worker(paths, team_id, *, generation=1):
    listener = PipeListener()
    member = scope("lead", team_id=team_id, generation=generation)
    args = ["--team-id", team_id, "--member-id", "lead", "--endpoint", listener.endpoint,
            "--generation", str(generation)]
    child = subprocess.Popen([sys.executable, "-I", "-B", "-m", WORKER_MODULE, *args],
                             cwd=paths.workspace_root, env=bootstrap_environment(),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return listener, member, args, child


@WINDOWS_IPC
def test_actual_worker_bootstrap_holds_native_lease_until_stopped(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("worker bootstrap")
    listener, member, args, child = actual_worker(paths, team.team_id)
    channel = None
    try:
        channel = listener.accept(child_identity(child), deadline=time.monotonic() + 3)
        hello = channel.receive(deadline=time.monotonic() + 3)
        assert hello["arguments"] == args and hello["isolated"] is True and hello["pid"] == channel.peer_pid
        bootstrap = PaneBootstrap(paths.workspace_root, paths.common_git_dir, paths.home,
                                  {"api_key": "fixture-process-secret"})
        channel.send(bootstrap.packet(member), deadline=time.monotonic() + 3)
        assert channel.receive(deadline=time.monotonic() + 3) == {
            "type": "ready", "pid": channel.peer_pid, "scope": member.to_dict()}
        with pytest.raises(TeamError, match="team_member_busy"):
            StoreExecutorLeases(store).acquire(replace(member, generation=2))
        calls = []
        light = LightweightBackend(lambda context: calls.append(context.scope),
                                   leases=StoreExecutorLeases(store))
        try:
            with pytest.raises(TeamError, match="team_member_busy"):
                light.start(replace(member, generation=2))
            assert calls == []
        finally:
            assert not light.shutdown().pending
        channel.send({"type": "wake", "scope": member.to_dict()}, deadline=time.monotonic() + 1)
        assert channel.receive(deadline=time.monotonic() + 1) == {
            "type": "status", "state": "failed", "error_code": "team_backend_unavailable"}
        channel.send({"type": "stop", "scope": member.to_dict()}, deadline=time.monotonic() + 1)
        stdout, stderr = child.communicate(timeout=4)
        assert child.returncode == 0 and not stdout and not stderr
        lease = StoreExecutorLeases(store).acquire(member)
        lease.__exit__(None, None, None)
        for path in paths.root.rglob("*"):
            if path.is_file():
                assert b"fixture-process-secret" not in path.read_bytes()
    finally:
        if channel:
            channel.close()
        listener.close()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=3)


@WINDOWS_IPC
def test_actual_duplicate_worker_cannot_take_existing_executor_lock(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("duplicate worker")
    member = scope("lead", team_id=team.team_id)
    owned = StoreExecutorLeases(store).acquire(member)
    os.lseek(owned.fd, 1, os.SEEK_SET)
    owner_before = os.read(owned.fd, 2048)
    listener, child_scope, args, child = actual_worker(paths, team.team_id, generation=2)
    channel = None
    try:
        channel = listener.accept(child_identity(child), deadline=time.monotonic() + 3)
        channel.receive(deadline=time.monotonic() + 3)
        channel.send(PaneBootstrap(paths.workspace_root, paths.common_git_dir, paths.home).packet(child_scope),
                     deadline=time.monotonic() + 3)
        assert channel.receive(deadline=time.monotonic() + 3) == {
            "type": "error", "error_code": "team_member_busy"}
        stdout, stderr = child.communicate(timeout=4)
        assert child.returncode == 1 and "Traceback" not in stderr and not stdout
        os.lseek(owned.fd, 1, os.SEEK_SET)
        assert os.read(owned.fd, 2048) == owner_before
        with pytest.raises(TeamError, match="team_member_busy"):
            StoreExecutorLeases(store).acquire(child_scope)
    finally:
        if channel:
            channel.close()
        listener.close()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=3)
        owned.__exit__(None, None, None)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="环境未安装tmux：真实pane/direct-exec尚未验证；不自动安装")
def test_real_tmux_pane_bootstrap_only_when_secure_platform_available(tmp_path):
    driver = TmuxDriver()
    capability = driver.probe()
    if not capability.available:
        pytest.skip(f"真实pane不可用：{capability.stage}；安全IPC/可信安装未满足")
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("real pane fixture")
    member = scope("lead", team_id=team.team_id)
    backend = PaneBackend(driver=driver)
    try:
        result = backend.start(member, PaneBootstrap(paths.workspace_root, paths.common_git_dir, paths.home))
        assert result.state == "idle"
    finally:
        assert not backend.shutdown().pending
