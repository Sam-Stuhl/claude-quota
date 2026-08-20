"""Shared daemon state and a tiny async broadcaster for SSE.

Everything runs in a single asyncio event loop (uvicorn's), so one SQLite
connection is shared across the ingest processor, the OTLP receivers, and the
API handlers. SQLite calls are blocking and short; because no coroutine awaits
between an ``execute`` and its ``commit``, writes do not interleave.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
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


@dataclass
class AppState:
    conn: sqlite3.Connection
    ingest_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    broadcaster: Broadcaster = field(default_factory=Broadcaster)
    otlp: OtlpLiveness = field(default_factory=OtlpLiveness)
    started_at: float = field(default_factory=time.time)

    async def notify_change(self) -> None:
        await self.broadcaster.publish()
