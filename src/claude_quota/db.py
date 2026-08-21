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

SCHEMA_VERSION = 2


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
    migrate(conn)
    conn.commit()
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a pre-existing database up to the current schema version.

    schema.sql creates fresh databases at the current version directly (its
    CREATE ... IF NOT EXISTS statements no-op on existing tables), so this only
    has real work to do when upgrading an older database in place.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    # v1 -> v2: richer statusline columns, device tags, plugin/effort in the
    # usage key, and the raw archive (the latter created by schema.sql already).
    qs = _columns(conn, "quota_sample")
    for col, decl in (
        ("device", "TEXT"),
        ("model_id", "TEXT"),
        ("cost_usd_total", "REAL"),
        ("context_pct", "REAL"),
        ("lines_added", "INTEGER"),
        ("lines_removed", "INTEGER"),
        ("exceeds_200k", "INTEGER"),
    ):
        if col not in qs:
            conn.execute(f"ALTER TABLE quota_sample ADD COLUMN {col} {decl}")

    if "device" not in _columns(conn, "session"):
        conn.execute("ALTER TABLE session ADD COLUMN device TEXT")

    # usage_bucket gained plugin_name/effort in its primary key, so it must be
    # rebuilt rather than altered. Copy existing rows forward with empty values.
    if "plugin_name" not in _columns(conn, "usage_bucket"):
        conn.executescript(
            """
            ALTER TABLE usage_bucket RENAME TO usage_bucket_v1;
            CREATE TABLE usage_bucket (
              ts INTEGER NOT NULL, session_id TEXT NOT NULL, model TEXT NOT NULL,
              query_source TEXT, agent_name TEXT, skill_name TEXT, mcp_server TEXT,
              plugin_name TEXT, effort TEXT,
              cost_usd REAL NOT NULL DEFAULT 0, tok_input INTEGER NOT NULL DEFAULT 0,
              tok_output INTEGER NOT NULL DEFAULT 0, tok_cache_r INTEGER NOT NULL DEFAULT 0,
              tok_cache_w INTEGER NOT NULL DEFAULT 0, active_ms INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (ts, session_id, model, query_source, agent_name,
                           skill_name, mcp_server, plugin_name, effort)
            ) WITHOUT ROWID;
            INSERT INTO usage_bucket
              (ts, session_id, model, query_source, agent_name, skill_name,
               mcp_server, plugin_name, effort, cost_usd, tok_input, tok_output,
               tok_cache_r, tok_cache_w, active_ms)
              SELECT ts, session_id, model, query_source, agent_name, skill_name,
                     mcp_server, '', '', cost_usd, tok_input, tok_output,
                     tok_cache_r, tok_cache_w, active_ms
              FROM usage_bucket_v1;
            DROP TABLE usage_bucket_v1;
            CREATE INDEX IF NOT EXISTS idx_bucket_ts ON usage_bucket(ts);
            """
        )

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def apply_retention(conn: sqlite3.Connection, now: int | None = None) -> None:
    """Roll off high-volume tables older than the retention window.

    window_history is deliberately kept forever; it is tiny.
    """
    now = now or int(time.time())
    cutoff = now - config.RETENTION_DAYS * 86400
    conn.execute("DELETE FROM quota_sample WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM usage_bucket WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM event_log WHERE ts < ?", (cutoff,))
    raw_cutoff = now - config.RAW_RETENTION_DAYS * 86400
    conn.execute("DELETE FROM raw_statusline WHERE ts < ?", (raw_cutoff,))
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
    device: str | None = None,
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
               (session_id, first_seen, last_seen, device, cwd, project_dir,
                git_worktree, entrypoint, start_type, color_idx)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                ts,
                ts,
                device,
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
                 device = COALESCE(?, device),
                 cwd = COALESCE(?, cwd),
                 project_dir = COALESCE(?, project_dir),
                 git_worktree = COALESCE(?, git_worktree),
                 entrypoint = COALESCE(?, entrypoint),
                 start_type = COALESCE(?, start_type)
               WHERE session_id = ?""",
            (ts, device, cwd, project_dir, git_worktree, entrypoint, start_type, session_id),
        )


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple]) -> None:
    conn.executemany(sql, rows)
    conn.commit()
