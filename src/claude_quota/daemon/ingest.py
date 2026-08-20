"""Statusline ingest: the only source of quota truth.

The HTTP handler must return before doing any work (it sits on the critical
path of a status line render), so it only pushes the raw payload onto a queue.
This module also owns the pure parser and the queue processor that writes to
SQLite.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .state import AppState


@dataclass
class ParsedSample:
    ts: int
    session_id: str
    five_h_pct: float | None
    five_h_reset: int | None
    seven_d_pct: float | None
    seven_d_reset: int | None
    cc_version: str | None
    had_limits: bool
    # Session metadata, if present, so we can register the session too.
    cwd: str | None = None
    project_dir: str | None = None
    git_worktree: str | None = None


def parse_reset(value: Any) -> int | None:
    """resets_at has appeared as a unix epoch int and an ISO 8601 string.

    Accept both; return unix seconds or None. We never compute a reset time
    locally, so an unparseable value becomes None rather than a guess.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        try:
            # Handle a trailing Z (UTC) which fromisoformat rejects pre-3.11.
            iso = s.replace("Z", "+00:00")
            return int(datetime.fromisoformat(iso).timestamp())
        except ValueError:
            return None
    return None


def _limit(rate_limits: dict, key: str) -> tuple[float | None, int | None]:
    block = rate_limits.get(key)
    if not isinstance(block, dict):
        return None, None
    pct = block.get("used_percentage")
    pct = float(pct) if isinstance(pct, (int, float)) else None
    reset = parse_reset(block.get("resets_at"))
    return pct, reset


def parse_statusline(payload: dict, now: int | None = None) -> ParsedSample | None:
    """Normalize a statusline JSON blob into a ParsedSample.

    rate_limits is optional on every ingest (absent for API-key auth, before
    the first API response, or intermittently after server changes). Its
    absence is recorded via had_limits, never faked.
    """
    now = now or int(time.time())
    session_id = payload.get("session_id")
    if not session_id or not isinstance(session_id, str):
        return None

    workspace = payload.get("workspace") or {}

    rate_limits = payload.get("rate_limits")
    had_limits = isinstance(rate_limits, dict) and bool(rate_limits)
    if had_limits:
        five_pct, five_reset = _limit(rate_limits, "five_hour")
        seven_pct, seven_reset = _limit(rate_limits, "seven_day")
        # If the block exists but carries no usable percentage, treat it as
        # absent for degraded-mode accounting.
        if five_pct is None and seven_pct is None:
            had_limits = False
    else:
        five_pct = five_reset = seven_pct = seven_reset = None

    return ParsedSample(
        ts=now,
        session_id=session_id,
        five_h_pct=five_pct,
        five_h_reset=five_reset,
        seven_d_pct=seven_pct,
        seven_d_reset=seven_reset,
        cc_version=payload.get("version"),
        had_limits=had_limits,
        cwd=payload.get("cwd") or workspace.get("current_dir"),
        project_dir=workspace.get("project_dir"),
        git_worktree=workspace.get("git_worktree"),
    )


def store_sample(conn: sqlite3.Connection, s: ParsedSample) -> None:
    """Persist a parsed sample and register/refresh its session."""
    from .. import db

    db.upsert_session(
        conn,
        s.session_id,
        s.ts,
        cwd=s.cwd,
        project_dir=s.project_dir,
        git_worktree=s.git_worktree,
        entrypoint="statusline",
    )
    conn.execute(
        """INSERT INTO quota_sample
           (ts, session_id, five_h_pct, five_h_reset, seven_d_pct, seven_d_reset,
            cc_version, had_limits)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            s.ts,
            s.session_id,
            s.five_h_pct,
            s.five_h_reset,
            s.seven_d_pct,
            s.seven_d_reset,
            s.cc_version,
            1 if s.had_limits else 0,
        ),
    )
    conn.commit()


async def processor(state: AppState) -> None:
    """Drain the ingest queue, parse, store, and notify SSE subscribers."""
    while True:
        payload = await state.ingest_queue.get()
        try:
            sample = parse_statusline(payload)
            if sample is not None:
                store_sample(state.conn, sample)
                await state.notify_change()
        except Exception:
            # A malformed ingest must never take the processor down.
            pass
        finally:
            state.ingest_queue.task_done()
