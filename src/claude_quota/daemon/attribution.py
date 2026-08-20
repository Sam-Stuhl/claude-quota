"""Turn raw usage into per-session shares and the unattributed residual.

Shares are expressed as absolute percentage points of the current 5-hour
window, so a session's share plus the unattributed residual sum to the measured
percentage. The residual is real usage (claude.ai chat and Cowork draw from the
same pool); surfacing it keeps per-session shares honest.
"""

from __future__ import annotations

import os
import sqlite3
import time

from .. import config
from . import calibration

FIVE_HOURS = 5 * 3600


def session_label(cwd: str | None, project_dir: str | None, git_worktree: str | None) -> str:
    base = project_dir or cwd
    name = os.path.basename(base.rstrip("/")) if base else "unknown"
    if git_worktree:
        return f"{name}@{git_worktree}"
    return name or "unknown"


def latest_limits(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT ts, five_h_pct, five_h_reset, seven_d_pct, seven_d_reset
           FROM quota_sample
           WHERE had_limits = 1
             AND ts = (SELECT MAX(ts) FROM quota_sample WHERE had_limits = 1)"""
    ).fetchone()
    if not row:
        return {}
    return {
        "ts": int(row["ts"]),
        "five_h_pct": row["five_h_pct"],
        "five_h_reset": row["five_h_reset"],
        "seven_d_pct": row["seven_d_pct"],
        "seven_d_reset": row["seven_d_reset"],
    }


def degraded(conn: sqlite3.Connection, now: int) -> tuple[bool, str | None]:
    """True when rate_limits has been absent across all sessions for too long."""
    row = conn.execute(
        "SELECT MAX(ts) AS last FROM quota_sample WHERE had_limits = 1"
    ).fetchone()
    last = row["last"] if row else None
    if last is None:
        return True, "no rate_limits ever received"
    if now - int(last) > config.DEGRADED_AFTER_SECONDS:
        mins = (now - int(last)) // 60
        return True, f"rate_limits absent for {mins}m"
    return False, None


def window_bounds(reset: int | None, now: int) -> tuple[int, int]:
    if reset is not None:
        return reset - FIVE_HOURS, reset
    return now - FIVE_HOURS, now + FIVE_HOURS


def _session_costs(conn: sqlite3.Connection, window_start: int) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT b.session_id AS sid, b.model AS model, SUM(b.cost_usd) AS cost,
                  MAX(b.ts) AS last_active,
                  SUM(b.tok_input+b.tok_output+b.tok_cache_r+b.tok_cache_w) AS toks
           FROM usage_bucket b
           WHERE b.ts >= ?
           GROUP BY b.session_id, b.model""",
        (window_start,),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        sid = r["sid"]
        d = out.setdefault(
            sid, {"cost": 0.0, "last_active": 0, "toks": 0, "by_model": {}}
        )
        d["cost"] += float(r["cost"] or 0.0)
        d["toks"] += int(r["toks"] or 0)
        d["last_active"] = max(d["last_active"], int(r["last_active"] or 0))
        d["by_model"][r["model"]] = float(r["cost"] or 0.0)
    return out


def compute(conn: sqlite3.Connection, now: int | None = None) -> dict:
    now = now or int(time.time())
    limits = latest_limits(conn)
    is_degraded, reason = degraded(conn, now)
    # Trust fitted per-model rates only once calibration is actually good;
    # before that, a half-converged NNLS fit produces misleading shares, so
    # fall back to attributing straight by cost (the flat 1.0 prior).
    rates = calibration.get_rates(conn) if calibration.is_calibrated(conn) else {}

    reset = limits.get("five_h_reset")
    window_start, _ = window_bounds(reset, now)
    costs = _session_costs(conn, window_start)

    # Estimated pct per session from calibrated cost.
    est_by_session: dict[str, float] = {}
    for sid, d in costs.items():
        est = sum(
            c * calibration.rate_for(rates, m) for m, c in d["by_model"].items()
        )
        est_by_session[sid] = est
    total_est = sum(est_by_session.values())

    measured = limits.get("five_h_pct")
    estimated_flag = is_degraded or measured is None
    if estimated_flag:
        five_pct = min(total_est, 999.0)
    else:
        five_pct = float(measured)

    # Unattributed residual: measured minus what local cost explains.
    if not estimated_flag:
        unattributed = max(0.0, five_pct - total_est)
    else:
        unattributed = 0.0

    # Session metadata for labels/colours.
    meta = {
        r["session_id"]: r
        for r in conn.execute(
            "SELECT session_id, cwd, project_dir, git_worktree, color_idx, last_seen "
            "FROM session"
        ).fetchall()
    }

    sessions = []
    for sid, d in costs.items():
        m = meta.get(sid)
        last_active = max(d["last_active"], int(m["last_seen"]) if m else 0)
        sessions.append(
            {
                "session_id": sid,
                "label": session_label(
                    m["cwd"] if m else None,
                    m["project_dir"] if m else None,
                    m["git_worktree"] if m else None,
                ),
                "color_idx": int(m["color_idx"]) if m else 0,
                "cost_usd": round(d["cost"], 4),
                "tokens": d["toks"],
                "est_pct": round(est_by_session.get(sid, 0.0), 2),
                "last_active": last_active,
                "idle": (now - last_active) > config.SESSION_IDLE_SECONDS,
            }
        )
    sessions.sort(key=lambda s: s["est_pct"], reverse=True)

    capped_since = None
    if five_pct >= 100:
        row = conn.execute(
            """SELECT MIN(ts) AS t FROM quota_sample
               WHERE had_limits = 1 AND five_h_pct >= 100 AND ts >= ?""",
            (window_start,),
        ).fetchone()
        capped_since = int(row["t"]) if row and row["t"] else None

    return {
        "now": now,
        "degraded": is_degraded,
        "degraded_reason": reason,
        "window_start": window_start,
        "five_hour": {
            "used_percentage": round(five_pct, 2),
            "resets_at": reset,
            "estimated": estimated_flag,
            "capped": five_pct >= 100,
            "capped_since": capped_since,
        },
        "seven_day": {
            "used_percentage": (
                round(float(limits["seven_d_pct"]), 2)
                if limits.get("seven_d_pct") is not None
                else None
            ),
            "resets_at": limits.get("seven_d_reset"),
            "estimated": estimated_flag and limits.get("seven_d_pct") is None,
        },
        "unattributed_pct": round(unattributed, 2),
        "sessions": sessions,
    }
