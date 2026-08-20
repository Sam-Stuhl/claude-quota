"""Projection: burn rate, cutoff time, and the SHORT/CLEAR verdict.

The burn rate is the OLS slope of the 5-hour percentage over the last 20
minutes (not 5: short windows swing wildly on a single compaction). We publish
the projection as a range using the standard error of the slope, and suppress
it entirely rather than print a garbage number when the signal is too thin.
"""

from __future__ import annotations

import math
import sqlite3
import time

# The regression window. Twenty minutes, per the spec.
BURN_WINDOW_SECONDS = 20 * 60
MIN_SAMPLES = 4


def _series(conn: sqlite3.Connection, since: int) -> list[tuple[int, float]]:
    rows = conn.execute(
        """SELECT ts, MAX(five_h_pct) AS pct FROM quota_sample
           WHERE had_limits = 1 AND five_h_pct IS NOT NULL AND ts >= ?
           GROUP BY ts ORDER BY ts""",
        (since,),
    ).fetchall()
    return [(int(r["ts"]), float(r["pct"])) for r in rows]


def _ols(points: list[tuple[int, float]]) -> tuple[float, float]:
    """Return (slope, slope_stderr) for pct vs time, in pct per second."""
    n = len(points)
    ts = [p[0] for p in points]
    ys = [p[1] for p in points]
    tbar = sum(ts) / n
    ybar = sum(ys) / n
    s_tt = sum((t - tbar) ** 2 for t in ts)
    if s_tt == 0:
        return 0.0, 0.0
    s_ty = sum((t - tbar) * (y - ybar) for t, y in zip(ts, ys, strict=True))
    slope = s_ty / s_tt
    # Residual variance -> standard error of the slope.
    resid = [y - (ybar + slope * (t - tbar)) for t, y in zip(ts, ys, strict=True)]
    if n > 2:
        s2 = sum(r * r for r in resid) / (n - 2)
        stderr = math.sqrt(s2 / s_tt)
    else:
        stderr = 0.0
    return slope, stderr


def _compaction_in_window(conn: sqlite3.Connection, since: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM event_log WHERE kind = 'claude_code.compaction' AND ts >= ? LIMIT 1",
        (since,),
    ).fetchone()
    return row is not None


def project(conn: sqlite3.Connection, now: int | None = None) -> dict:
    """Compute the projection, or an explained suppression."""
    now = now or int(time.time())
    since = now - BURN_WINDOW_SECONDS
    points = _series(conn, since)

    latest = conn.execute(
        """SELECT MAX(five_h_pct) AS pct, MAX(five_h_reset) AS reset
           FROM quota_sample
           WHERE had_limits = 1 AND five_h_pct IS NOT NULL
             AND ts = (SELECT MAX(ts) FROM quota_sample
                       WHERE had_limits = 1 AND five_h_pct IS NOT NULL)"""
    ).fetchone()
    current_pct = float(latest["pct"]) if latest and latest["pct"] is not None else None
    reset = latest["reset"] if latest else None

    base = {
        "available": False,
        "current_pct": current_pct,
        "resets_at": reset,
        "burn_rate_pct_per_hour": None,
        "burn_rate_stderr": None,
        "cutoff_at": None,
        "seconds_to_cap": None,
        "verdict": None,
        "margin_seconds": None,
        "ci_low_cutoff": None,
        "ci_high_cutoff": None,
        "suppressed_reason": None,
    }

    if len(points) < MIN_SAMPLES:
        base["suppressed_reason"] = "not enough signal"
        return base
    if _compaction_in_window(conn, since):
        base["suppressed_reason"] = "compaction spike, projection paused"
        return base

    slope_per_s, stderr_per_s = _ols(points)
    burn_per_hour = slope_per_s * 3600.0
    base["burn_rate_pct_per_hour"] = burn_per_hour
    base["burn_rate_stderr"] = stderr_per_s * 3600.0

    if burn_per_hour <= 0 or current_pct is None:
        base["suppressed_reason"] = "not enough signal"
        return base

    def cutoff_for(rate_per_hour: float) -> float | None:
        if rate_per_hour <= 0:
            return None
        secs = (100.0 - current_pct) / rate_per_hour * 3600.0
        return now + secs

    seconds_to_cap = (100.0 - current_pct) / burn_per_hour * 3600.0
    cutoff_at = now + seconds_to_cap
    base.update(
        available=True,
        seconds_to_cap=seconds_to_cap,
        cutoff_at=cutoff_at,
    )

    # Confidence range from the slope's standard error (faster burn -> earlier
    # cutoff, so the high-rate bound is the low-time bound).
    hi_rate = burn_per_hour + base["burn_rate_stderr"]
    lo_rate = max(burn_per_hour - base["burn_rate_stderr"], 1e-9)
    base["ci_low_cutoff"] = cutoff_for(hi_rate)
    base["ci_high_cutoff"] = cutoff_for(lo_rate)

    if reset is not None:
        base["verdict"] = "SHORT" if cutoff_at < reset else "CLEAR"
        base["margin_seconds"] = abs(cutoff_at - reset)
    return base
