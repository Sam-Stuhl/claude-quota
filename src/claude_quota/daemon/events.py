"""SSE stream for /events: push {summary, sessions} when data changes.

Subscribers wake on the broadcaster and are then rate-limited to at most one
push per second so a burst of ingests does not turn into a burst of frames.
"""

from __future__ import annotations

import asyncio
import json

from . import sessions, summary
from .state import AppState


async def event_stream(state: AppState):
    last_seen = 0

    def frame() -> str:
        # Built in a worker thread from the shared cache, so a burst of OTLP
        # messages (each one wakes every subscriber) costs one compute, not
        # one per subscriber, and never blocks the event loop.
        payload = {
            "summary": state.read(("summary",), 5, summary.build),
            "sessions": state.read(("sessions",), 5, sessions.build),
        }
        return f"data: {json.dumps(payload)}\n\n"

    # Send an initial frame immediately so a fresh client is not blank.
    yield await asyncio.to_thread(frame)

    while True:
        last_seen = await state.broadcaster.wait(last_seen)
        yield await asyncio.to_thread(frame)
        # Cap the push rate; coalesce any changes that arrive in this window.
        await asyncio.sleep(5.0)
