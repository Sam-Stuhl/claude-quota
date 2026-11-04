"""The measured limits: which feed to believe, and whether a reading is still live.

Three feeds can report the 5-hour and 7-day percentages (the status line,
the quota-meter mod, atlas-deck) and they can differ by a few points, so a
series is only ever fitted from one of them: the source of the newest sample.
A reading also dies with its window. A 5-hour sample whose reset time has
passed describes a window that no longer exists, so it is reported as expired
rather than carried forward (the old code kept serving it, which froze the
dashboard and put the window bounds in the past).
"""

from __future__ import annotations

import sqlite3

from .. import config


def latest(conn: sqlite3.Connection) -> dict:
    """The newest sample that carried limits, with its source."""
    row = conn.execute(
        """SELECT ts, source, five_h_pct, five_h_reset, seven_d_pct, seven_d_reset
           FROM quota_sample WHERE had_limits = 1 ORDER BY ts DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return {}
    return {
        "ts": int(row["ts"]),
        "source": row["source"] or "statusline",
        "five_h_pct": row["five_h_pct"],
        "five_h_reset": row["five_h_reset"],
        "seven_d_pct": row["seven_d_pct"],
        "seven_d_reset": row["seven_d_reset"],
    }


def source_clause(source: str) -> tuple[str, tuple]:
    """SQL fragment restricting quota_sample to one source (NULL means statusline)."""
    if source == "statusline":
        return "(source IS NULL OR source = ?)", (source,)
    return "source = ?", (source,)


def read(conn: sqlite3.Connection, now: int) -> dict:
    """Latest limits plus liveness: what is current, what has expired."""
    lim = latest(conn)
    if not lim:
        return {"known": False, "reason": "no rate_limits ever received"}
    age = now - lim["ts"]
    five_reset, seven_reset = lim["five_h_reset"], lim["seven_d_reset"]
    five_ok = lim["five_h_pct"] is not None and (five_reset is None or five_reset > now)
    seven_ok = lim["seven_d_pct"] is not None and (seven_reset is None or seven_reset > now)
    return {
        "known": True,
        "source": lim["source"],
        "ts": lim["ts"],
        "age_s": age,
        "stale": age > config.DEGRADED_AFTER_SECONDS,
        "five_h_pct": lim["five_h_pct"] if five_ok else None,
        "five_h_reset": five_reset,
        "five_expired": lim["five_h_pct"] is not None and not five_ok,
        "seven_d_pct": lim["seven_d_pct"] if seven_ok else None,
        "seven_d_reset": seven_reset,
        "seven_expired": lim["seven_d_pct"] is not None and not seven_ok,
    }
