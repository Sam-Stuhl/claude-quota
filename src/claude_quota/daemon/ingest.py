"""Statusline ingest: the only source of quota truth.

The HTTP handler must return before doing any work (it sits on the critical
path of a status line render), so it only pushes the raw payload onto a queue.
This module owns the pure parser and the queue processor that writes to SQLite.

Beyond the quota percentages we capture the rest of the render state Claude
Code hands us (cost, context window, lines, model) and archive the full raw
payload so nothing is lost as the schema evolves.
"""

from __future__ import annotations

import json
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
    device: str | None = None
    session_name: str | None = None
    model_id: str | None = None
    cost_usd_total: float | None = None
    context_pct: float | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    exceeds_200k: int | None = None
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


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _intish(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def parse_statusline(
    payload: dict, now: int | None = None, device: str | None = None
) -> ParsedSample | None:
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
    model = payload.get("model") or {}
    cost = payload.get("cost") or {}
    ctx = payload.get("context_window") or {}

    rate_limits = payload.get("rate_limits")
    had_limits = isinstance(rate_limits, dict) and bool(rate_limits)
    if had_limits:
        five_pct, five_reset = _limit(rate_limits, "five_hour")
        seven_pct, seven_reset = _limit(rate_limits, "seven_day")
        if five_pct is None and seven_pct is None:
            had_limits = False
    else:
        five_pct = five_reset = seven_pct = seven_reset = None

    exceeds = payload.get("exceeds_200k_tokens")
    return ParsedSample(
        ts=now,
        session_id=session_id,
        five_h_pct=five_pct,
        five_h_reset=five_reset,
        seven_d_pct=seven_pct,
        seven_d_reset=seven_reset,
        cc_version=payload.get("version"),
        had_limits=had_limits,
        device=device,
        session_name=payload.get("session_name"),
        model_id=model.get("id"),
        cost_usd_total=_num(cost.get("total_cost_usd")),
        context_pct=_num(ctx.get("used_percentage")),
        lines_added=_intish(cost.get("total_lines_added")),
        lines_removed=_intish(cost.get("total_lines_removed")),
        exceeds_200k=(1 if exceeds else 0) if exceeds is not None else None,
        cwd=payload.get("cwd") or workspace.get("current_dir"),
        project_dir=workspace.get("project_dir"),
        git_worktree=workspace.get("git_worktree"),
    )


def _window(windows: list, key: str) -> tuple[float | None, int | None]:
    for w in windows:
        if isinstance(w, dict) and w.get("id") == key:
            reset = _num(w.get("resetsAt"))
            # happy reports resets in epoch milliseconds; we store seconds.
            return _num(w.get("utilization")), int(reset // 1000) if reset is not None else None
    return None, None


def parse_limits(payload: dict, now: int | None = None) -> ParsedSample | None:
    """Normalize an atlas-deck daemon's post into a ParsedSample.

    The body is ``{session_id, device, model, name?, cwd?, usageLimits}``,
    where usageLimits is happy's agent-state shape: ``{capturedAt (ms),
    windows: [{id: "five_hour" | "seven_day" | ..., utilization (0-100),
    resetsAt (ms)}]}``. Unknown window ids are ignored.
    """
    now = now or int(time.time())
    session_id = payload.get("session_id")
    limits = payload.get("usageLimits")
    if not session_id or not isinstance(session_id, str) or not isinstance(limits, dict):
        return None
    windows = limits.get("windows")
    if not isinstance(windows, list):
        return None
    five_pct, five_reset = _window(windows, "five_hour")
    seven_pct, seven_reset = _window(windows, "seven_day")
    captured = _num(limits.get("capturedAt"))
    return ParsedSample(
        ts=int(captured // 1000) if captured else now,
        session_id=session_id,
        five_h_pct=five_pct,
        five_h_reset=five_reset,
        seven_d_pct=seven_pct,
        seven_d_reset=seven_reset,
        cc_version=payload.get("version"),
        had_limits=five_pct is not None or seven_pct is not None,
        device=payload.get("device"),
        session_name=payload.get("name"),
        model_id=payload.get("model"),
        cwd=payload.get("cwd"),
    )


def store_sample(
    conn: sqlite3.Connection,
    s: ParsedSample,
    raw: str | None = None,
    entrypoint: str = "statusline",
) -> None:
    """Persist a parsed sample, register its session, and archive the raw JSON."""
    from .. import db

    db.upsert_session(
        conn,
        s.session_id,
        s.ts,
        name=s.session_name,
        device=s.device,
        cwd=s.cwd,
        project_dir=s.project_dir,
        git_worktree=s.git_worktree,
        entrypoint=entrypoint,
    )
    conn.execute(
        """INSERT INTO quota_sample
           (ts, session_id, device, five_h_pct, five_h_reset, seven_d_pct,
            seven_d_reset, cc_version, had_limits, model_id, cost_usd_total,
            context_pct, lines_added, lines_removed, exceeds_200k)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            s.ts, s.session_id, s.device, s.five_h_pct, s.five_h_reset,
            s.seven_d_pct, s.seven_d_reset, s.cc_version, 1 if s.had_limits else 0,
            s.model_id, s.cost_usd_total, s.context_pct, s.lines_added,
            s.lines_removed, s.exceeds_200k,
        ),
    )
    if raw is not None:
        conn.execute(
            "INSERT INTO raw_statusline (ts, session_id, device, payload) VALUES (?,?,?,?)",
            (s.ts, s.session_id, s.device, raw),
        )
    conn.commit()


async def processor(state: AppState) -> None:
    """Drain the ingest queue, parse, store, and notify SSE subscribers."""
    while True:
        raw, device = await state.ingest_queue.get()
        try:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            payload = json.loads(text)
            sample = parse_statusline(payload, device=device)
            if sample is not None:
                store_sample(state.conn, sample, raw=text)
                await state.notify_change()
        except Exception:
            # A malformed ingest must never take the processor down.
            pass
        finally:
            state.ingest_queue.task_done()
