"""SQLite access: connection management, schema init, and retention.

The daemon keeps a single write connection (SQLite serializes writes anyway)
and hands out short-lived read connections. WAL mode lets readers proceed while
the writer is busy, which is what the SSE/API side wants.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from . import config

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

# A small, stable palette of slots. color_idx indexes into this on the client;
# we only store the integer so a session keeps its colour across reloads.
PALETTE_SIZE = 12


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with sane defaults and the schema applied."""
    p = path or config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def apply_retention(conn: sqlite3.Connection, now: int | None = None) -> None:
    """Roll off high-volume tables older than the retention window.

    window_history is deliberately kept forever; it is tiny.
    """
    now = now or int(time.time())
    cutoff = now - config.RETENTION_DAYS * 86400
    conn.execute("DELETE FROM quota_sample WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM usage_bucket WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM event_log WHERE ts < ?", (cutoff,))
    conn.commit()


def next_color_idx(conn: sqlite3.Connection) -> int:
    """Assign the next palette slot, cycling through PALETTE_SIZE."""
    row = conn.execute("SELECT COUNT(*) AS n FROM session").fetchone()
    return int(row["n"]) % PALETTE_SIZE


def upsert_session(
    conn: sqlite3.Connection,
    session_id: str,
    ts: int,
    *,
    cwd: str | None = None,
    project_dir: str | None = None,
    git_worktree: str | None = None,
    entrypoint: str | None = None,
    start_type: str | None = None,
) -> None:
    """Insert a session on first sight, otherwise refresh last_seen and metadata.

    COALESCE keeps the first non-null value we learned for the static fields so
    a later sparse ingest (e.g. an OTel-only sighting) doesn't blank them out.
    """
    existing = conn.execute(
        "SELECT session_id FROM session WHERE session_id = ?", (session_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO session
               (session_id, first_seen, last_seen, cwd, project_dir, git_worktree,
                entrypoint, start_type, color_idx)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                ts,
                ts,
                cwd,
                project_dir,
                git_worktree,
                entrypoint,
                start_type,
                next_color_idx(conn),
            ),
        )
    else:
        conn.execute(
            """UPDATE session SET
                 last_seen = MAX(last_seen, ?),
                 cwd = COALESCE(?, cwd),
                 project_dir = COALESCE(?, project_dir),
                 git_worktree = COALESCE(?, git_worktree),
                 entrypoint = COALESCE(?, entrypoint),
                 start_type = COALESCE(?, start_type)
               WHERE session_id = ?""",
            (ts, cwd, project_dir, git_worktree, entrypoint, start_type, session_id),
        )


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple]) -> None:
    conn.executemany(sql, rows)
    conn.commit()
