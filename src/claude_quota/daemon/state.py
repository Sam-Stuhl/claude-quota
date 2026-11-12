"""Shared daemon state and a tiny async broadcaster for SSE.

Everything runs in a single asyncio event loop (uvicorn's), so one SQLite
connection is shared across the ingest processor, the OTLP receivers, and the
API handlers. SQLite calls are blocking and short; because no coroutine awaits
between an ``execute`` and its ``commit``, writes do not interleave.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class Broadcaster:
    """Wakes all SSE subscribers when the underlying data changes.

    Subscribers pass the version they last saw; ``wait`` returns the new
    version once it advances. A monotonic counter avoids missed edges.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self.version = 0

    async def publish(self) -> None:
        async with self._cond:
            self.version += 1
            self._cond.notify_all()

    async def wait(self, last_seen: int) -> int:
        async with self._cond:
            await self._cond.wait_for(lambda: self.version != last_seen)
            return self.version


@dataclass
class OtlpLiveness:
    """Last-seen timestamps and counts, so ``doctor`` can prove OTel lands."""

    last_metric_ts: float = 0.0
    last_log_ts: float = 0.0
    metric_messages: int = 0
    log_messages: int = 0
    data_points: int = 0

    def note_metrics(self, points: int) -> None:
        self.last_metric_ts = time.time()
        self.metric_messages += 1
        self.data_points += points

    def note_logs(self, records: int) -> None:
        self.last_log_ts = time.time()
        self.log_messages += records


class TTLCache:
    """Compute-once-per-interval cache with single flight.

    Every dashboard client (and every SSE wake) asks for the same few answers.
    Computing each one once per ``ttl`` seconds, on one thread at a time, keeps
    a page open in three tabs from costing three times the database.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[object, tuple[float, object]] = {}
        self._key_locks: dict[object, threading.Lock] = {}

    def get(self, key: object, ttl: float, fn: Callable[[], object]) -> object:
        now = time.monotonic()
        hit = self._items.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        with self._lock:
            klock = self._key_locks.setdefault(key, threading.Lock())
        with klock:
            hit = self._items.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = fn()
            self._items[key] = (time.monotonic(), value)
            return value

    def drop(self, prefix: str) -> None:
        """Forget every cached answer whose key starts with ``prefix`` (after a write)."""
        for key in list(self._items):
            if isinstance(key, tuple) and key and key[0] == prefix:
                self._items.pop(key, None)


@dataclass
class AppState:
    conn: sqlite3.Connection
    # Reads run on their own connection, one query at a time (psycopg and
    # sqlite3 connections are not meant for interleaved cursors).
    read_conn: sqlite3.Connection | None = None
    read_lock: threading.Lock = field(default_factory=threading.Lock)
    cache: TTLCache = field(default_factory=TTLCache)
    ingest_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    broadcaster: Broadcaster = field(default_factory=Broadcaster)
    otlp: OtlpLiveness = field(default_factory=OtlpLiveness)
    started_at: float = field(default_factory=time.time)

    def read(self, key: object, ttl: float, build: Callable[[sqlite3.Connection], object]):
        """A cached read: ``build(conn)`` on the reader connection, at most once per ttl."""
        def run():
            with self.read_lock:
                return build(self.read_conn or self.conn)
        return self.cache.get(key, ttl, run)

    async def notify_change(self) -> None:
        await self.broadcaster.publish()
