from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from newcode.subagents.manager import (
    MAX_QUEUED_TASKS,
    MAX_SESSION_TASKS,
    MAX_TASK_RECORDS,
    MAX_WORKERS,
    SubAgentManager,
)
from newcode.subagents.types import (
    SubAgentManagerError,
    TaskExecution,
    TaskState,
    WorkerResult,
)


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_manager_limits_workers_queue_and_per_session_tasks_atomically():
    assert MAX_WORKERS == 2
    assert MAX_QUEUED_TASKS == 4
    assert MAX_SESSION_TASKS == 6
    entered: list[str] = []
    active = 0
    peak = 0
    gate = threading.Event()
    lock = threading.Lock()

    def worker(context):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            entered.append(context.task_input)
        gate.wait(2)
        with lock:
            active -= 1
        return WorkerResult(summary=context.task_input)

    manager = SubAgentManager(worker)
    try:
        scope = manager.open_session("limited-session")
        tasks = [manager.start(scope, f"task-{index}") for index in range(MAX_WORKERS)]
        assert _wait_for(lambda: len(entered) == MAX_WORKERS)
        tasks.extend(manager.start(scope, f"task-{index}") for index in range(MAX_WORKERS, MAX_SESSION_TASKS))
        with pytest.raises(SubAgentManagerError, match="subagent_concurrency_limit"):
            manager.start(scope, "seventh")
        assert sum(manager.status(scope, item.task_id).state is TaskState.QUEUED for item in tasks) == 4
        gate.set()
        for item in tasks:
            outcome = manager.wait(scope, item.task_id, 2)
            assert outcome.result is not None
    finally:
        gate.set()
        manager.shutdown()
    assert peak == MAX_WORKERS


def test_queue_full_is_distinct_from_session_limit():
    gate = threading.Event()
    entered = 0
    both_entered = threading.Event()
    lock = threading.Lock()

    def worker(_context):
        nonlocal entered
        with lock:
            entered += 1
            if entered == MAX_WORKERS:
                both_entered.set()
        gate.wait(2)
        return WorkerResult(summary="done")

    manager = SubAgentManager(worker)
    scopes = [manager.open_session(f"queue-{index}") for index in range(7)]
    try:
        tasks = []
        tasks.append(manager.start(scopes[0], "running one"))
        tasks.append(manager.start(scopes[1], "running two"))
        assert both_entered.wait(1)
        for index in range(2, 6):
            tasks.append(manager.start(scopes[index], f"queued {index}"))
        with pytest.raises(SubAgentManagerError, match="subagent_queue_full"):
            manager.start(scopes[6], "overflow")
        gate.set()
        for index, item in enumerate(tasks):
            assert manager.wait(scopes[index], item.task_id, 2).result is not None
    finally:
        gate.set()
        manager.shutdown()


def test_background_can_be_entered_explicitly_manually_and_after_wait_timeout():
    release = {name: threading.Event() for name in ("explicit", "manual", "automatic")}
    entered = {name: threading.Event() for name in release}

    def worker(context):
        entered[context.task_input].set()
        release[context.task_input].wait(2)
        return WorkerResult(summary=context.task_input)

    manager = SubAgentManager(worker)
    scope = manager.open_session("background-paths")
    try:
        explicit = manager.start(scope, "explicit", execution=TaskExecution.BACKGROUND)
        assert entered["explicit"].wait(1)

        manual = manager.start(scope, "manual")
        assert entered["manual"].wait(1)
        assert manager.background(scope, manual.task_id).state is TaskState.BACKGROUND

        automatic = manager.start(scope, "automatic")
        timed_wait = manager.wait(scope, automatic.task_id, 0.01)
        assert timed_wait.result is None and timed_wait.timed_out
        assert timed_wait.snapshot.execution is TaskExecution.BACKGROUND

        release["manual"].set()
        release["explicit"].set()
        release["automatic"].set()
        assert _wait_for(lambda: all(manager.status(scope, task.task_id).state is TaskState.COMPLETED for task in (explicit, manual, automatic)))
        notices = manager.drain_notifications(scope)
        assert {item.task_id for item in notices} == {explicit.task_id, manual.task_id, automatic.task_id}
        assert manager.drain_notifications(scope) == ()
    finally:
        for event in release.values():
            event.set()
        manager.shutdown()


def test_background_notifications_follow_completion_order_and_collect_is_mutually_exclusive():
    releases = {name: threading.Event() for name in ("slow", "fast", "collected")}
    entered = {name: threading.Event() for name in releases}

    def worker(context):
        entered[context.task_input].set()
        releases[context.task_input].wait(2)
        return WorkerResult(summary=context.task_input)

    manager = SubAgentManager(worker)
    scope = manager.open_session("notice-order")
    try:
        slow = manager.start(scope, "slow", execution="background")
        fast = manager.start(scope, "fast", execution="background")
        assert entered["slow"].wait(1)
        assert entered["fast"].wait(1)
        claimed = manager.start(scope, "collected", execution="background")

        releases["fast"].set()
        assert _wait_for(lambda: manager.status(scope, fast.task_id).state is TaskState.COMPLETED)
        assert entered["collected"].wait(1)
        releases["collected"].set()
        assert _wait_for(lambda: manager.status(scope, claimed.task_id).state is TaskState.COMPLETED)
        releases["slow"].set()
        assert _wait_for(lambda: manager.status(scope, slow.task_id).state is TaskState.COMPLETED)

        direct = manager.collect(scope, claimed.task_id)
        assert direct.summary == "collected"
        notices = manager.drain_notifications(scope)
        assert [result.task_id for result in notices] == [fast.task_id, slow.task_id]
        assert [result.completion_sequence for result in notices] == sorted(result.completion_sequence for result in notices)
        with pytest.raises(SubAgentManagerError, match="subagent_result_already_collected"):
            manager.collect(scope, fast.task_id)
        assert manager.drain_notifications(scope) == ()
    finally:
        for event in releases.values():
            event.set()
        manager.shutdown()


def test_close_and_reopen_same_session_id_increments_generation_and_discards_old_notice():
    gate = threading.Event()
    entered = threading.Event()

    def worker(_context):
        entered.set()
        gate.wait(2)
        return WorkerResult(summary="must be discarded")

    manager = SubAgentManager(worker)
    try:
        old_scope = manager.open_session("reused-session")
        task = manager.start(old_scope, "old", execution="background")
        assert entered.wait(1)
        manager.close_session(old_scope)
        new_scope = manager.open_session("reused-session")
        assert new_scope.generation != old_scope.generation
        gate.set()
        assert _wait_for(lambda: manager._tasks[task.task_id].state is TaskState.CANCELLED)
        assert manager.drain_notifications(new_scope) == ()
        with pytest.raises(SubAgentManagerError, match="subagent_parent_session_closed"):
            manager.status(old_scope, task.task_id)
    finally:
        gate.set()
        manager.shutdown()


def test_cancel_wins_or_completion_wins_without_overwriting_terminal_state():
    entered = threading.Event()
    release = threading.Event()

    def worker(_context):
        entered.set()
        release.wait(2)
        return WorkerResult(summary="complete")

    manager = SubAgentManager(worker)
    scope = manager.open_session("cancel-race")
    try:
        task = manager.start(scope, "race")
        assert entered.wait(1)
        barrier = threading.Barrier(3)

        def complete():
            barrier.wait()
            release.set()

        def cancel():
            barrier.wait()
            return manager.cancel(scope, task.task_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(complete)
            cancel_future = pool.submit(cancel)
            barrier.wait()
            future.result(timeout=1)
            cancel_future.result(timeout=1)

        final = manager.status(scope, task.task_id)
        assert final.state in {TaskState.COMPLETED, TaskState.CANCELLED}
        result = manager.collect(scope, task.task_id)
        assert result.state is final.state
        with pytest.raises(SubAgentManagerError, match="subagent_result_already_collected"):
            manager.collect(scope, task.task_id)
    finally:
        release.set()
        manager.shutdown()


def test_timeout_deadline_uses_fake_clock_and_late_worker_result_is_ignored():
    class Clock:
        now = 10.0

        def __call__(self):
            return self.now

    clock = Clock()
    entered, release = threading.Event(), threading.Event()

    def worker(_context):
        entered.set()
        release.wait(2)
        return WorkerResult(summary="late result")

    manager = SubAgentManager(worker, clock=clock)
    scope = manager.open_session("deadline")
    try:
        task = manager.start(scope, "deadline task")
        assert entered.wait(1)
        clock.now += 300
        manager.check_deadlines()
        assert manager.status(scope, task.task_id).state is TaskState.TIMED_OUT
        release.set()
        result = manager.collect(scope, task.task_id)
        assert result.state is TaskState.TIMED_OUT
        assert result.summary == ""
    finally:
        release.set()
        manager.shutdown()


def test_worker_exception_is_isolated_and_diagnostics_are_stable():
    def worker(context):
        if context.task_input == "boom":
            raise RuntimeError("secret path / private exception")
        return WorkerResult(summary="ok")

    manager = SubAgentManager(worker)
    scope = manager.open_session("worker-error")
    try:
        failed = manager.start(scope, "boom")
        good = manager.start(scope, "good")
        failed_result = manager.wait(scope, failed.task_id, 2).result
        good_result = manager.wait(scope, good.task_id, 2).result
        assert failed_result is not None and failed_result.error_code == "subagent_provider_error"
        assert "secret path" not in repr(failed_result)
        assert good_result is not None and good_result.summary == "ok"
    finally:
        manager.shutdown()


def test_task_store_never_evicts_unclaimed_results_and_evicts_oldest_claimed_record():
    manager = SubAgentManager(lambda context: WorkerResult(summary=context.task_input))
    scopes = []
    tasks = []
    try:
        for index in range(MAX_TASK_RECORDS):
            scope = manager.open_session(f"record-{index}")
            scopes.append(scope)
            task = manager.start(scope, str(index), execution="background")
            tasks.append(task)
            assert _wait_for(lambda task_id=task.task_id: manager._tasks[task_id].state is TaskState.COMPLETED)

        scope = manager.open_session("record-overflow")
        with pytest.raises(SubAgentManagerError, match="subagent_task_store_full"):
            manager.start(scope, "must fail")

        oldest_result = manager.collect(scopes[0], tasks[0].task_id)
        assert oldest_result.summary == "0"
        replacement_scope = manager.open_session("record-replacement")
        replacement = manager.start(replacement_scope, "replacement")
        assert replacement.task_id
        with pytest.raises(SubAgentManagerError, match="subagent_not_found"):
            manager.status(scopes[0], tasks[0].task_id)
        assert len(manager._tasks) == MAX_TASK_RECORDS
    finally:
        manager.shutdown()


def test_shutdown_is_idempotent_bounded_and_cancels_scope_even_after_worker_exception():
    entered, release = threading.Event(), threading.Event()

    def worker(_context):
        entered.set()
        release.wait(2)
        raise RuntimeError("private failure")

    manager = SubAgentManager(worker)
    scope = manager.open_session("shutdown")
    task = manager.start(scope, "blocked")
    assert entered.wait(1)
    started = time.monotonic()
    manager.shutdown(wait_timeout=0.1)
    elapsed = time.monotonic() - started
    manager.shutdown(wait_timeout=0.1)
    assert elapsed < 0.5
    with pytest.raises(SubAgentManagerError, match="subagent_parent_session_closed"):
        manager.status(scope, task.task_id)
    release.set()
