from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Hashable, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class CacheEntry:
    revision: Hashable
    value: Any
    stored_at: float


class ReadCache:
    """Small process-local cache with per-key single-flight loading."""

    def __init__(self, *, max_entries: int = 64) -> None:
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._key_locks: dict[str, threading.Lock] = {}

    def get(self, key: str, revision: Hashable, *, max_age: float | None = None) -> T | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.revision != revision:
                return None
            if max_age is not None and now - entry.stored_at > max(0.0, float(max_age)):
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry.value

    def set(self, key: str, revision: Hashable, value: T) -> T:
        with self._lock:
            self._entries[key] = CacheEntry(revision=revision, value=value, stored_at=time.monotonic())
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return value

    def get_or_load(
        self,
        key: str,
        revision: Hashable,
        loader: Callable[[], T],
        *,
        max_age: float | None = None,
    ) -> T:
        cached = self.get(key, revision, max_age=max_age)
        if cached is not None:
            return cached
        key_lock = self._key_lock(key)
        with key_lock:
            cached = self.get(key, revision, max_age=max_age)
            if cached is not None:
                return cached
            return self.set(key, revision, loader())

    def invalidate(self, *prefixes: str) -> None:
        with self._lock:
            if not prefixes:
                self._entries.clear()
                return
            for key in tuple(self._entries):
                if any(key == prefix or key.startswith(f"{prefix}:") for prefix in prefixes):
                    self._entries.pop(key, None)

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock
