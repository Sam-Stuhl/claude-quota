"""Calibration: fit the local-cost -> quota-percentage exchange rate.

Quota percentage is server-side and account-wide; OTel cost is local and
per-session. Nothing tells us the exchange rate, and it differs by model. But
we observe both streams at once, so we can fit it: pair consecutive quota
samples, measure the percentage delta, sum per-model cost over the same
interval, and solve a non-negative least squares problem for pct-per-usd.
"""

from __future__ import annotations

import sqlite3
import time

import numpy as np
from scipy.optimize import nnls

from .. import config


def account_series(conn: sqlite3.Connection, since: int) -> list[tuple[int, float, int | None]]:
    """Account-level 5h series: (ts, pct, reset), one point per timestamp.

    Concurrent sessions report the same account-wide percentage; collapsing by
    timestamp (taking the max, which they agree on) gives a clean series.
    """
    rows = conn.execute(
        """SELECT ts, MAX(five_h_pct) AS pct, MAX(five_h_reset) AS reset
           FROM quota_sample
           WHERE had_limits = 1 AND five_h_pct IS NOT NULL AND ts >= ?
           GROUP BY ts ORDER BY ts""",
        (since,),
    ).fetchall()
    return [(int(r["ts"]), float(r["pct"]), r["reset"]) for r in rows]


def _cost_by_model(conn: sqlite3.Connection, t0: int, t1: int) -> dict[str, float]:
    rows = conn.execute(
        """SELECT model, SUM(cost_usd) AS c FROM usage_bucket
           WHERE ts > ? AND ts <= ? GROUP BY model""",
        (t0, t1),
    ).fetchall()
    return {r["model"]: float(r["c"] or 0.0) for r in rows}


def build_observations(
    conn: sqlite3.Connection, now: int | None = None, lookback_days: int = 14
) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Build the (models, A, b) system from paired consecutive samples.

    A pair is discarded if a reset occurred in the interval (reset id changed
    or the percentage decreased), since that is not usage we can attribute.
    """
    now = now or int(time.time())
    since = now - lookback_days * 86400
    series = account_series(conn, since)

    models: list[str] = []
    model_idx: dict[str, int] = {}
    rows: list[dict[str, float]] = []
    deltas: list[float] = []

    for (t0, p0, r0), (t1, p1, r1) in zip(series, series[1:], strict=False):
        if r0 is not None and r1 is not None and r0 != r1:
            continue  # window rolled over
        dpct = p1 - p0
        if dpct < 0:
            continue  # reset within the interval
        costs = _cost_by_model(conn, t0, t1)
        if not costs and dpct == 0:
            continue  # uninformative
        for m in costs:
            if m not in model_idx:
                model_idx[m] = len(models)
                models.append(m)
        rows.append(costs)
        deltas.append(dpct)

    if not rows:
        return models, np.zeros((0, len(models))), np.zeros(0)

    A = np.zeros((len(rows), len(models)))
    for i, costs in enumerate(rows):
        for m, c in costs.items():
            A[i, model_idx[m]] = c
    b = np.asarray(deltas, dtype=float)
    return models, A, b


def seed_prior(conn: sqlite3.Connection, now: int | None = None) -> None:
    """Cold start: a flat prior of 1.0 pct_per_usd for every model seen."""
    now = now or int(time.time())
    models = [
        r["model"]
        for r in conn.execute(
            "SELECT DISTINCT model FROM usage_bucket WHERE model != ''"
        ).fetchall()
    ]
    for m in models:
        exists = conn.execute(
            "SELECT 1 FROM calibration WHERE model = ?", (m,)
        ).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO calibration (model, pct_per_usd, r2, n_samples, updated_at)
                   VALUES (?, 1.0, NULL, 0, ?)""",
                (m, now),
            )
    conn.commit()


def refit(conn: sqlite3.Connection, now: int | None = None) -> dict:
    """Refit the NNLS model and rewrite the calibration table.

    Returns a small summary dict for logging/health.
    """
    now = now or int(time.time())
    seed_prior(conn, now)
    models, A, b = build_observations(conn, now)
    if A.shape[0] < 2 or not models:
        return {"models": models, "n_samples": int(A.shape[0]), "r2": None, "fitted": False}

    coef, _ = nnls(A, b)
    pred = A @ coef
    ss_res = float(np.sum((b - pred) ** 2))
    ss_tot = float(np.sum((b - np.mean(b)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    n = int(A.shape[0])

    for m, c in zip(models, coef, strict=True):
        conn.execute(
            """INSERT INTO calibration (model, pct_per_usd, r2, n_samples, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(model) DO UPDATE SET
                 pct_per_usd = excluded.pct_per_usd,
                 r2 = excluded.r2,
                 n_samples = excluded.n_samples,
                 updated_at = excluded.updated_at""",
            (m, float(c), r2, n, now),
        )
    conn.commit()
    return {"models": models, "n_samples": n, "r2": r2, "fitted": True}


def get_rates(conn: sqlite3.Connection) -> dict[str, float]:
    """model -> pct_per_usd, falling back to the 1.0 prior for unknown models."""
    rows = conn.execute("SELECT model, pct_per_usd FROM calibration").fetchall()
    return {r["model"]: float(r["pct_per_usd"]) for r in rows}


def rate_for(rates: dict[str, float], model: str) -> float:
    return rates.get(model, 1.0)


def is_calibrated(conn: sqlite3.Connection) -> bool:
    """True once at least one model clears both trust thresholds."""
    row = conn.execute(
        "SELECT MAX(n_samples) AS n, MAX(r2) AS r2 FROM calibration"
    ).fetchone()
    if not row or row["n"] is None:
        return False
    n = row["n"] or 0
    r2 = row["r2"] or 0.0
    return n >= config.CALIBRATION_MIN_SAMPLES and r2 >= config.CALIBRATION_MIN_R2
