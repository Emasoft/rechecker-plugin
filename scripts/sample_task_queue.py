#!/usr/bin/env python3
"""Sample task queue with priority scheduling and retry logic.

Test file for measuring rechecker token consumption on a ~4KB source file.
"""

import hashlib
import heapq
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class Priority(Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(order=True)
class Task:
    priority: int
    task_id: str = field(compare=False)
    name: str = field(compare=False)
    payload: dict = field(compare=False, default_factory=dict)
    status: TaskStatus = field(compare=False, default=TaskStatus.PENDING)
    retries: int = field(compare=False, default=0)
    max_retries: int = field(compare=False, default=3)
    result: Any = field(compare=False, default=None)
    error: str | None = field(compare=False, default=None)

    def checksum(self) -> str:
        content = f"{self.task_id}:{self.name}:{self.payload}"
        return hashlib.sha256(content.encode()).hexdigest()[:12]


class TaskQueue:
    def __init__(self, max_workers: int = 4):
        self._heap: list[Task] = []
        self._handlers: dict[str, Callable] = {}
        self._results: dict[str, Any] = {}
        self._completed = 0
        self._failed = 0

    def register(self, name: str, handler: Callable) -> None:
        self._handlers[name] = handler

    def submit(self, name: str, payload: dict | None = None,
               priority: Priority = Priority.NORMAL) -> str:
        task_id = hashlib.md5(
            f"{name}:{time.time()}".encode()
        ).hexdigest()[:10]
        task = Task(
            priority=-priority.value,
            task_id=task_id,
            name=name,
            payload=payload or {},
        )
        heapq.heappush(self._heap, task)
        return task_id

    def _run(self, task: Task) -> bool:
        handler = self._handlers.get(task.name)
        if handler is None:
            task.status = TaskStatus.FAILED
            task.error = f"No handler for '{task.name}'"
            self._failed += 1
            return False
        task.status = TaskStatus.RUNNING
        try:
            task.result = handler(task.payload)
            task.status = TaskStatus.COMPLETED
            self._results[task.task_id] = task.result
            self._completed += 1
            return True
        except Exception as e:
            task.error = str(e)
            if task.retries < task.max_retries:
                task.retries += 1
                heapq.heappush(self._heap, task)
                return True
            task.status = TaskStatus.FAILED
            self._failed += 1
            return False

    def process(self) -> list[dict]:
        out = []
        while self._heap:
            task = heapq.heappop(self._heap)
            self._run(task)
            out.append({
                "id": task.task_id, "name": task.name,
                "status": task.status.value, "retries": task.retries,
            })
        return out

    def stats(self) -> dict[str, int]:
        return {"completed": self._completed, "failed": self._failed}


if __name__ == "__main__":
    q = TaskQueue()
    q.register("add", lambda p: p["a"] + p["b"])
    q.register("greet", lambda p: f"Hello {p['name']}")
    q.submit("add", {"a": 10, "b": 20}, Priority.HIGH)
    q.submit("greet", {"name": "World"}, Priority.LOW)
    q.submit("missing", {}, Priority.CRITICAL)
    results = q.process()
    for r in results:
        print(f"  {r['name']}: {r['status']}")
    print(f"Stats: {q.stats()}")
