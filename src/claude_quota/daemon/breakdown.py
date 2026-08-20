"""Assemble /api/breakdown: cost grouped by model, agent, mcp, or skill."""

from __future__ import annotations

import sqlite3
import time

from . import attribution

_COLUMN = {
    "model": "model",
    "agent": "agent_name",
    "mcp": "mcp_server",
    "skill": "skill_name",
    "plugin": "plugin_name",
    "effort": "effort",
}

FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 86400


def _window_start(conn: sqlite3.Connection, window: str, now: int) -> int:
    if window == "today":
        # Local midnight.
        lt = time.localtime(now)
        midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        return midnight
    if window == "7d":
        return now - SEVEN_DAYS
    return now - FIVE_HOURS  # default 5h


def build(
    conn: sqlite3.Connection,
    by: str = "model",
    window: str = "5h",
    now: int | None = None,
) -> dict:
    now = now or int(time.time())
    start = _window_start(conn, window, now)
    if by == "device":
        # Device lives on the session row, so join it in.
        rows = conn.execute(
            """SELECT COALESCE(s.device, '(unknown)') AS key, SUM(b.cost_usd) AS cost,
                      SUM(b.tok_input) AS ti, SUM(b.tok_output) AS to_,
                      SUM(b.tok_cache_r) AS tcr, SUM(b.tok_cache_w) AS tcw
               FROM usage_bucket b LEFT JOIN session s ON s.session_id = b.session_id
               WHERE b.ts >= ? GROUP BY key ORDER BY cost DESC""",
            (start,),
        ).fetchall()
    else:
        col = _COLUMN.get(by, "model")
        rows = conn.execute(
            f"""SELECT {col} AS key, SUM(cost_usd) AS cost,
                       SUM(tok_input) AS ti, SUM(tok_output) AS to_,
                       SUM(tok_cache_r) AS tcr, SUM(tok_cache_w) AS tcw
                FROM usage_bucket WHERE ts >= ?
                GROUP BY {col} ORDER BY cost DESC""",
            (start,),
        ).fetchall()
    items = []
    for r in rows:
        key = r["key"] or "(none)"
        items.append(
            {
                "key": key,
                "cost_usd": round(float(r["cost"] or 0.0), 4),
                "tok_input": int(r["ti"] or 0),
                "tok_output": int(r["to_"] or 0),
                "tok_cache_read": int(r["tcr"] or 0),
                "tok_cache_write": int(r["tcw"] or 0),
            }
        )
    return {"now": now, "by": by, "window": window, "items": items}


def unattributed_note(conn: sqlite3.Connection, now: int | None = None) -> float:
    return attribution.compute(conn, now)["unattributed_pct"]
