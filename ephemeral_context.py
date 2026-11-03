"""Small bounded in-memory contexts; no attachment bytes are retained."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable


class ExpiringContext:
    def __init__(self, ttl_seconds: float, max_entries: int = 256, *, clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._items: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        for key, (expires, _) in list(self._items.items()):
            if expires <= now:
                self._items.pop(key, None)

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._items[key] = (now + self.ttl_seconds, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def get(self, key: Hashable) -> Any:
        with self._lock:
            self._expire(self._clock())
            item = self._items.get(key)
            return item[1] if item else None

    def pop(self, key: Hashable) -> Any:
        with self._lock:
            self._expire(self._clock())
            item = self._items.pop(key, None)
            return item[1] if item else None

    def discard_chat(self, chat_id: int) -> None:
        """Clear per-user entries for an entire conversation after /new."""
        with self._lock:
            for key in list(self._items):
                if isinstance(key, tuple) and key and key[0] == chat_id:
                    self._items.pop(key, None)
