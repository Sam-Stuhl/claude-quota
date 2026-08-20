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
    # Send an initial frame immediately so a fresh client is not blank.
    payload = {
        "summary": summary.build(state.conn),
        "sessions": sessions.build(state.conn),
    }
    yield f"data: {json.dumps(payload)}\n\n"

    while True:
        last_seen = await state.broadcaster.wait(last_seen)
        payload = {
            "summary": summary.build(state.conn),
            "sessions": sessions.build(state.conn),
        }
        yield f"data: {json.dumps(payload)}\n\n"
        # Cap the push rate; coalesce any changes that arrive in this second.
        await asyncio.sleep(1.0)
