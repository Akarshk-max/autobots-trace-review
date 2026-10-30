import asyncio
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Generic, Hashable, Optional, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass
class _CacheItem(Generic[V]):
    expires_at: float
    value: V


class InMemoryTTLCache(Generic[K, V]):

    def __init__(self, *, ttl_seconds: int, max_items: int = 2048) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        if max_items <= 0:
            raise ValueError("max_items must be > 0")

        self._ttl_seconds = int(ttl_seconds)
        self._max_items = int(max_items)
        self._lock = asyncio.Lock()
        self._items: "OrderedDict[K, _CacheItem[V]]" = OrderedDict()

    def _now(self) -> float:
        return time.monotonic()

    def _is_expired(self, item: _CacheItem[V], now: float) -> bool:
        return item.expires_at <= now

    def _prune_expired_no_lock(self, now: float) -> None:
        # OrderedDict isn't ordered by expiry, so we scan.
        expired_keys = [k for k, v in self._items.items() if self._is_expired(v, now)]
        for k in expired_keys:
            self._items.pop(k, None)

    def _enforce_size_no_lock(self) -> None:
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)  # LRU eviction

    async def get(self, key: K) -> Optional[V]:
        async with self._lock:
            now = self._now()
            item = self._items.get(key)
            if item is None:
                return None
            if self._is_expired(item, now):
                self._items.pop(key, None)
                return None

            # Refresh LRU
            self._items.move_to_end(key, last=True)
            return item.value

    async def set(self, key: K, value: V) -> None:
        async with self._lock:
            now = self._now()
            self._prune_expired_no_lock(now)

            self._items[key] = _CacheItem(expires_at=now + self._ttl_seconds, value=value)
            self._items.move_to_end(key, last=True)
            self._enforce_size_no_lock()

    async def invalidate_where(self, predicate: Callable[[K], bool]) -> int:
        async with self._lock:
            keys = [k for k in self._items.keys() if predicate(k)]
            for k in keys:
                self._items.pop(k, None)
            return len(keys)

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()
    def _approx_size_bytes_no_lock(self, *, sample_items: int = 200) -> tuple[int, int]:
        total_items = len(self._items)
        if total_items == 0:
            return (sys.getsizeof(self._items), 0)

        n = min(int(sample_items), total_items)
        keys = list(self._items.keys())[:n]

        sample_bytes = sys.getsizeof(self._items)
        for k in keys:
            item = self._items.get(k)
            sample_bytes += sys.getsizeof(k)
            if item is not None:
                sample_bytes += sys.getsizeof(item)
                sample_bytes += sys.getsizeof(item.value)

        if n == total_items:
            return (sample_bytes, n)

        scaled = int(sample_bytes * (total_items / max(n, 1)))
        return (scaled, n)

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            now = self._now()
            self._prune_expired_no_lock(now)
            approx_size_bytes, sample_items = self._approx_size_bytes_no_lock(sample_items=200)
            return {
                "ttl_seconds": self._ttl_seconds,
                "max_items": self._max_items,
                "items": len(self._items),
                "approx_size_bytes": approx_size_bytes,
                "approx_size_sample_items": sample_items,
            }