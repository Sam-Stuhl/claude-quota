"""Assemble /api/health: calibration state, source liveness, sample counts.

Three independent integrations can each silently fail, so health reports on
each separately. The CLI's ``doctor`` reads this to render its checks.
"""

from __future__ import annotations

import sqlite3
import time

from .. import config
from . import calibration
from .state import AppState


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def build(conn: sqlite3.Connection, state: AppState, now: int | None = None) -> dict:
    now = now or int(time.time())

    last_status = _scalar(conn, "SELECT MAX(ts) FROM quota_sample")
    last_limits = _scalar(conn, "SELECT MAX(ts) FROM quota_sample WHERE had_limits = 1")
    quota_count = _scalar(conn, "SELECT COUNT(*) FROM quota_sample")
    bucket_count = _scalar(conn, "SELECT COUNT(*) FROM usage_bucket")
    session_count = _scalar(conn, "SELECT COUNT(*) FROM session")
    event_count = _scalar(conn, "SELECT COUNT(*) FROM event_log")

    cal_rows = conn.execute(
        "SELECT model, pct_per_usd, r2, n_samples, updated_at FROM calibration "
        "ORDER BY n_samples DESC"
    ).fetchall()

    statusline_live = last_status > 0 and (now - last_status) < 120
    has_limits = last_limits > 0 and (now - last_limits) < config.DEGRADED_AFTER_SECONDS
    metric_live = state.otlp.last_metric_ts > 0 and (now - state.otlp.last_metric_ts) < 120

    return {
        "now": now,
        "uptime_s": int(now - state.started_at),
        "sources": {
            "statusline": {
                "live": statusline_live,
                "last_seen": last_status or None,
                "samples": quota_count,
                "rate_limits_present": has_limits,
                "last_rate_limits": last_limits or None,
            },
            "otel_metrics": {
                "live": metric_live,
                "last_seen": (
                    int(state.otlp.last_metric_ts) if state.otlp.last_metric_ts else None
                ),
                "messages": state.otlp.metric_messages,
                "data_points": state.otlp.data_points,
                "buckets": bucket_count,
            },
            "otel_logs": {
                "last_seen": (
                    int(state.otlp.last_log_ts) if state.otlp.last_log_ts else None
                ),
                "records": state.otlp.log_messages,
                "events": event_count,
            },
        },
        "calibration": {
            "calibrated": calibration.is_calibrated(conn),
            "models": [
                {
                    "model": r["model"],
                    "pct_per_usd": round(float(r["pct_per_usd"]), 4),
                    "r2": round(float(r["r2"]), 3) if r["r2"] is not None else None,
                    "n_samples": int(r["n_samples"]),
                    "updated_at": int(r["updated_at"]),
                }
                for r in cal_rows
            ],
        },
        "counts": {
            "quota_sample": quota_count,
            "usage_bucket": bucket_count,
            "session": session_count,
            "event_log": event_count,
        },
    }
