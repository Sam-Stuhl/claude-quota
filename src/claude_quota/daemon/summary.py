"""Assemble /api/summary: the current windows, projection, and verdict.

Also carries the real 5-hour percentage history (for the sparkline) and a
per-feed source status, so the client can be informative from the status line
alone, before any OTel attribution arrives.
"""

from __future__ import annotations

import sqlite3
import time

from .. import config
from . import attribution, calibration, projection


def _series(conn: sqlite3.Connection, start: int, now: int, max_points: int = 150) -> list[list]:
    rows = conn.execute(
        """SELECT ts, MAX(five_h_pct) AS pct FROM quota_sample
           WHERE had_limits = 1 AND five_h_pct IS NOT NULL AND ts >= ? AND ts <= ?
           GROUP BY ts ORDER BY ts""",
        (start, now),
    ).fetchall()
    pts = [[int(r["ts"]), round(float(r["pct"]), 2)] for r in rows]
    if len(pts) > max_points:
        step = len(pts) / max_points
        pts = [pts[int(i * step)] for i in range(max_points)]
    return pts


def _sources(conn: sqlite3.Connection, now: int) -> dict:
    def scalar(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    last_status = scalar("SELECT MAX(ts) FROM quota_sample")
    last_limits = scalar("SELECT MAX(ts) FROM quota_sample WHERE had_limits = 1")
    last_otel = scalar("SELECT MAX(ts) FROM usage_bucket")
    # The status line only renders on session activity, so an active session can
    # sit quiet for a few minutes; be forgiving before calling the feed down.
    return {
        "statusline": {
            "live": bool(last_status) and (now - last_status) < 300,
            "rate_limits": bool(last_limits)
            and (now - last_limits) < config.DEGRADED_AFTER_SECONDS,
        },
        "otel": {
            "live": bool(last_otel) and (now - last_otel) < 120,
            "ever": bool(last_otel),
        },
    }


def build(conn: sqlite3.Connection, now: int | None = None) -> dict:
    now = now or int(time.time())
    out = attribution.compute(conn, now)
    out["projection"] = projection.project(conn, now)
    out["calibrated"] = calibration.is_calibrated(conn)
    out["series"] = _series(conn, out["window_start"], now)
    out["sources"] = _sources(conn, now)
    out["has_attribution"] = len(out["sessions"]) > 0
    return out
