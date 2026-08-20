"""Window history: close out passed 5-hour windows and serve /api/history.

Windows are account-level and defined by resets_at, so we key on it rather than
inferring boundaries from session start. A window is closed once its reset time
has passed; we then record its peak, whether it hit the cap, total cost, and
session count. window_history is kept forever (it is tiny).
"""

from __future__ import annotations

import sqlite3
import time

FIVE_HOURS = 5 * 3600


def close_windows(conn: sqlite3.Connection, now: int | None = None) -> int:
    """Record any passed 5h windows not yet in window_history. Returns count."""
    now = now or int(time.time())
    resets = conn.execute(
        """SELECT DISTINCT five_h_reset AS r FROM quota_sample
           WHERE five_h_reset IS NOT NULL AND five_h_reset <= ?
             AND five_h_reset NOT IN (SELECT reset_at FROM window_history)""",
        (now,),
    ).fetchall()
    n = 0
    for row in resets:
        reset = int(row["r"])
        start = reset - FIVE_HOURS
        peak = conn.execute(
            """SELECT MAX(five_h_pct) AS p FROM quota_sample
               WHERE five_h_reset = ? AND five_h_pct IS NOT NULL""",
            (reset,),
        ).fetchone()
        peak_pct = float(peak["p"]) if peak and peak["p"] is not None else 0.0
        cost_row = conn.execute(
            "SELECT SUM(cost_usd) AS c, COUNT(DISTINCT session_id) AS n "
            "FROM usage_bucket WHERE ts > ? AND ts <= ?",
            (start, reset),
        ).fetchone()
        total_cost = float(cost_row["c"] or 0.0)
        sessions = int(cost_row["n"] or 0)
        conn.execute(
            """INSERT OR IGNORE INTO window_history
               (reset_at, peak_pct, hit_cap, total_cost, session_count)
               VALUES (?, ?, ?, ?, ?)""",
            (reset, peak_pct, 1 if peak_pct >= 100 else 0, total_cost, sessions),
        )
        n += 1
    conn.commit()
    return n


def build(conn: sqlite3.Connection, days: int = 14, now: int | None = None) -> dict:
    now = now or int(time.time())
    close_windows(conn, now)
    cutoff = now - days * 86400
    rows = conn.execute(
        """SELECT reset_at, peak_pct, hit_cap, total_cost, session_count
           FROM window_history WHERE reset_at >= ? ORDER BY reset_at DESC""",
        (cutoff,),
    ).fetchall()
    windows = [
        {
            "reset_at": int(r["reset_at"]),
            "peak_pct": round(float(r["peak_pct"]), 2),
            "hit_cap": bool(r["hit_cap"]),
            "total_cost": round(float(r["total_cost"]), 4),
            "session_count": int(r["session_count"]),
        }
        for r in rows
    ]
    return {"now": now, "days": days, "windows": windows}
