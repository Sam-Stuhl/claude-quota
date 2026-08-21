"""Assemble /api/sessions: the live session leaderboard.

Live means active or recently active. A session is marked idle after 5 minutes
without activity and drops off the list after 30, but its usage stays in the
window total via the summary's attribution.
"""

from __future__ import annotations

import sqlite3
import time

from .. import config
from . import attribution


def _burn_per_hour(conn: sqlite3.Connection, session_id: str, now: int) -> float:
    """Recent cost rate for a session, in USD/hour over the last 20 minutes."""
    since = now - 20 * 60
    row = conn.execute(
        "SELECT SUM(cost_usd) AS c FROM usage_bucket WHERE session_id = ? AND ts >= ?",
        (session_id, since),
    ).fetchone()
    cost = float(row["c"] or 0.0) if row else 0.0
    return cost / (20 / 60)


def build(conn: sqlite3.Connection, now: int | None = None) -> dict:
    now = now or int(time.time())
    summary = attribution.compute(conn, now)
    total_est = sum(s["est_pct"] for s in summary["sessions"]) or 1.0

    live = []
    for s in summary["sessions"]:
        age = now - s["last_active"]
        if age > config.SESSION_DROP_SECONDS:
            continue
        share = round(100.0 * s["est_pct"] / total_est, 1)
        live.append(
            {
                **s,
                "share_of_sessions_pct": share,
                "burn_usd_per_hour": round(_burn_per_hour(conn, s["session_id"], now), 4),
                "last_active_ago_s": age,
            }
        )
    return {
        "now": now,
        "sessions": live,
        "unattributed_pct": summary["unattributed_pct"],
        "degraded": summary["degraded"],
    }
