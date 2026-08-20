"""Assemble /api/summary: the current windows, projection, and verdict."""

from __future__ import annotations

import sqlite3
import time

from . import attribution, calibration, projection


def build(conn: sqlite3.Connection, now: int | None = None) -> dict:
    now = now or int(time.time())
    out = attribution.compute(conn, now)
    out["projection"] = projection.project(conn, now)
    out["calibrated"] = calibration.is_calibrated(conn)
    return out
