"""追加式事件存储。

系统的全部事实都写入 ``events.jsonl``（每行一个事件，只增不改），
重启后按顺序重放即可恢复包括待复核队列在内的完整状态。
写操作通过 ``locked()`` 持有文件锁，保证 CLI 与服务进程并发追加时
不会出现交叉写。
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import RegistryError

EVENT_REVISION_SUBMITTED = "revision_submitted"
EVENT_REVISION_REVIEWED = "revision_reviewed"


class EventStore:
    def __init__(self, data_dir: str | os.PathLike[str]):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.data_dir / "events.jsonl"
        self.lock_path = self.data_dir / "events.lock"

    @contextmanager
    def locked(self) -> Iterator["EventStore"]:
        """持有排他文件锁的上下文；CLI 与 HTTP 服务的每次操作都在锁内完成。"""
        with open(self.lock_path, "a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield self
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def append(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        """追加一个事件并落盘。调用方必须处于 ``locked()`` 上下文中。"""
        event = {"seq": self._next_seq(), "type": event_type, "data": data}
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def read_all(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(self.events_path, "r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise RegistryError(
                        "store_corrupt",
                        f"事件日志第 {lineno} 行无法解析，存储可能已被破坏",
                        {"line": lineno},
                    ) from exc
        return events

    def _next_seq(self) -> int:
        if not self.events_path.exists():
            return 1
        with open(self.events_path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip()) + 1
