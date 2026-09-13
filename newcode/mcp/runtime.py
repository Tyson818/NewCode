from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import Future
import threading
from typing import Any, TypeVar


T = TypeVar("T")


class MCPRuntime:
    """在专用线程中持有唯一 event loop 的同步桥接器。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="newcode-mcp-runtime",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def run_sync(self, coroutine: Coroutine[Any, Any, T]) -> T:
        with self._lock:
            if self._closed:
                coroutine.close()
                raise RuntimeError("MCP runtime is shut down.")
            future: Future[T] = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result()

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()
