"""Hook 异步动作的有界单 worker 生命周期。"""

from __future__ import annotations

from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Callable


class HookWorker:
    def __init__(self, *, capacity: int = 32, on_error: Callable[[], None] | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._queue: Queue[Callable[[], None]] = Queue(maxsize=capacity)
        self._stopping = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._on_error = on_error

    def submit(self, job: Callable[[], None]) -> bool:
        with self._lock:
            if self._stopping.is_set():
                return False
            try:
                self._queue.put_nowait(job)
            except Full:
                return False
            if self._thread is None:
                self._thread = Thread(target=self._run, name="newcode-hook-worker", daemon=True)
                self._thread.start()
            return True

    def shutdown(self, timeout_seconds: float = 1.0) -> bool:
        """停止接收，丢弃待执行任务，对运行任务只有限等待。"""

        with self._lock:
            self._stopping.set()
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except Empty:
                    break
            thread = self._thread
        if thread is not None:
            thread.join(max(0.0, timeout_seconds))
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                if not self._stopping.is_set():
                    job()
            except Exception:
                # 不记录异常原文；worker 绝不因单项异常退出。
                if self._on_error is not None:
                    try:
                        self._on_error()
                    except Exception:
                        pass
            finally:
                self._queue.task_done()
