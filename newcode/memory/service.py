"""自然完成后异步执行的受控自动记忆服务。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import json
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any

from newcode.session import ChatMessage

from .store import MemoryStore, MemoryStoreError, redact_memory_text
from .types import MemoryCategory, MemoryScope


MAX_QUEUE_SIZE = 8
MAX_SNAPSHOT_CHARACTERS = 6_000


@dataclass(frozen=True)
class MemoryGenerationRequest:
    """对生成器的窄请求边界，显式禁止工具和文件读取。"""

    prompt: str
    tools: tuple[object, ...] = ()
    allow_tool_calls: bool = False
    allow_file_reads: bool = False


@dataclass(frozen=True)
class MemoryDecision:
    action: str
    scope: MemoryScope | None = None
    category: MemoryCategory | None = None
    content: str | None = None
    tags: tuple[str, ...] = ()
    note_id: str | None = None


class MemoryService:
    """单 worker、有界队列；单任务只进行一次受控生成请求。"""

    def __init__(
        self,
        store: MemoryStore,
        generator: Callable[[MemoryGenerationRequest], str],
        *,
        sensitive_values: Iterable[str] = (),
        queue_size: int = MAX_QUEUE_SIZE,
    ) -> None:
        self.store = store
        self.generator = generator
        self.sensitive_values = tuple(sensitive_values)
        self._queue: Queue[str | None] = Queue(maxsize=queue_size)
        self._stopping = Event()
        self._lock = Lock()
        self._accepting = True
        self._thread = Thread(target=self._run, name="newcode-memory", daemon=True)
        self._thread.start()

    def submit(self, messages: Iterable[ChatMessage]) -> bool:
        """仅排队，绝不等待生成或磁盘写入。"""

        snapshot = build_memory_snapshot(messages, self.sensitive_values)
        if not snapshot:
            return False
        with self._lock:
            if not self._accepting:
                return False
            try:
                self._queue.put_nowait(snapshot)
            except Full:
                return False
        return True

    def shutdown(self, timeout_seconds: float = 0.2) -> bool:
        """停止接收新任务；有界等待，未结束的 daemon worker 可被安全放弃。"""

        with self._lock:
            if not self._accepting:
                return not self._thread.is_alive()
            self._accepting = False
            self._stopping.set()
            try:
                self._queue.put_nowait(None)
            except Full:
                pass
        self._thread.join(max(0.0, timeout_seconds))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            try:
                snapshot = self._queue.get(timeout=0.05)
            except Empty:
                if self._stopping.is_set():
                    return
                continue
            try:
                if snapshot is None or self._stopping.is_set():
                    return
                self._process(snapshot)
            finally:
                self._queue.task_done()

    def _process(self, snapshot: str) -> None:
        request = MemoryGenerationRequest(prompt=_memory_prompt(snapshot))
        try:
            response = self.generator(request)
            decision = parse_memory_decision(response)
            apply_memory_decision(self.store, decision)
        except (MemoryStoreError, ValueError, TypeError, json.JSONDecodeError):
            return
        except Exception:
            return


def build_memory_snapshot(
    messages: Iterable[ChatMessage],
    sensitive_values: Iterable[str] = (),
) -> str:
    """仅保留有限 user/assistant 文本，彻底排除工具输出与完整错误。"""

    safe_values = tuple(sensitive_values)
    selected: list[str] = []
    used = 0
    for message in reversed(tuple(messages)):
        if message.role not in ("user", "assistant") or not message.content:
            continue
        content = redact_memory_text(message.content, safe_values)
        line = f"{message.role}: {content}"
        remaining = MAX_SNAPSHOT_CHARACTERS - used
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[:remaining]
        selected.append(line)
        used += len(line) + 1
    return "\n".join(reversed(selected))


def parse_memory_decision(response: str) -> MemoryDecision:
    """只接受无 Markdown 包装、字段精确的单个 JSON 对象。"""

    if not isinstance(response, str) or not response.strip() or response != response.strip():
        raise ValueError("memory_generation_failed")
    value = json.loads(response)
    if not isinstance(value, dict) or not isinstance(value.get("action"), str):
        raise ValueError("memory_generation_failed")
    action = value["action"]
    if action == "ignore":
        if set(value) != {"action"}:
            raise ValueError("memory_generation_failed")
        return MemoryDecision(action="ignore")
    required = {"action", "scope", "category", "content", "tags"}
    if action == "create":
        if set(value) != required:
            raise ValueError("memory_generation_failed")
        note_id = None
    elif action in ("update", "merge"):
        if set(value) != required | {"note_id"}:
            raise ValueError("memory_generation_failed")
        note_id = value.get("note_id")
        if not isinstance(note_id, str):
            raise ValueError("memory_generation_failed")
    else:
        raise ValueError("memory_generation_failed")
    try:
        scope = MemoryScope(value["scope"])
        category = MemoryCategory(value["category"])
    except (TypeError, ValueError) as exc:
        raise ValueError("memory_generation_failed") from exc
    content = value.get("content")
    tags = value.get("tags")
    if not isinstance(content, str) or not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("memory_generation_failed")
    return MemoryDecision(action, scope, category, content, tuple(tags), note_id)


def apply_memory_decision(store: MemoryStore, decision: MemoryDecision) -> None:
    """只执行 LLM 选择的动作，不补充本地语义去重规则。"""

    if decision.action == "ignore":
        return
    if decision.scope is None or decision.category is None or decision.content is None:
        raise ValueError("memory_generation_failed")
    if decision.action == "create":
        store.create(decision.scope, decision.category, decision.content, tags=decision.tags)
        return
    if decision.note_id is None:
        raise ValueError("memory_generation_failed")
    if decision.action == "update":
        store.update(
            decision.scope,
            decision.note_id,
            decision.content,
            category=decision.category,
            tags=decision.tags,
        )
        return
    if decision.action == "merge":
        store.merge(decision.scope, decision.note_id, decision.content)
        return
    raise ValueError("memory_generation_failed")


def _memory_prompt(snapshot: str) -> str:
    return (
        "你是 NewCode 自动记忆去重器。只根据下列脱敏快照作出一次判断。"
        "不得调用工具、读取文件、访问网络或请求更多上下文。"
        "只输出一个无 Markdown 包装的严格 JSON 对象："
        "ignore 为 {\"action\":\"ignore\"}；其他动作为 create/update/merge，"
        "必须给出 scope、category、content、tags，update/merge 另给 note_id。\n\n"
        f"<snapshot>\n{snapshot}\n</snapshot>"
    )
