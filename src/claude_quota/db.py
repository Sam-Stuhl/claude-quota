"""Database access: connection management, schema init, and retention.

Defaults to local SQLite. When DATABASE_URL points at a Postgres instance the
same code runs against Postgres instead, so a container with no persistent
volume can keep its data in an external database. A small adapter (PgConn)
translates the handful of SQLite-isms the rest of the code relies on (``?``
placeholders, dict-style rows), so callers stay dialect-agnostic.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from . import config

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
_SCHEMA_PG = (Path(__file__).parent / "schema_pg.sql").read_text()

# A small, stable palette of slots. color_idx indexes into this on the client;
# we only store the integer so a session keeps its colour across reloads.
PALETTE_SIZE = 12

SCHEMA_VERSION = 3


class PgConn:
    """Adapts a psycopg connection to the subset of the sqlite3 API we use.

    Translates ``?`` placeholders to ``%s`` and runs in autocommit, so each
    write lands immediately and there is no aborted-transaction state to nurse.
    Rows come back dict-style (via psycopg's dict_row), matching sqlite3.Row's
    ``row["col"]`` access.
    """

    def __init__(self, raw) -> None:
        self._raw = raw

    @staticmethod
    def _q(sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params: tuple = ()):  # returns a cursor
        return self._raw.execute(self._q(sql), tuple(params) or None)

    def executemany(self, sql: str, rows: Iterable[tuple]) -> None:
        rows = [tuple(r) for r in rows]
        if not rows:
            return
        with self._raw.cursor() as cur:
            cur.executemany(self._q(sql), rows)

    def executescript(self, script: str) -> None:
        with self._raw.cursor() as cur:
            for stmt in script.split(";"):
                if stmt.strip():
                    cur.execute(stmt)

    def commit(self) -> None:  # autocommit is on; nothing to do
        pass

    def close(self) -> None:
        self._raw.close()


def connect(path: Path | None = None):
    """Open a connection with the schema applied (SQLite or Postgres)."""
    if config.is_postgres():
        return _connect_pg()
    return _connect_sqlite(path)


def _connect_sqlite(path: Path | None) -> sqlite3.Connection:
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


def _connect_pg() -> PgConn:
    import psycopg
    from psycopg.rows import dict_row

    raw = psycopg.connect(config.database_url(), autocommit=True, row_factory=dict_row)
    conn = PgConn(raw)
    conn.executescript(_SCHEMA_PG)
    # Idempotent column adds so an existing Postgres database picks up new
    # columns on redeploy (Postgres supports ADD COLUMN IF NOT EXISTS).
    conn.execute("ALTER TABLE session ADD COLUMN IF NOT EXISTS name TEXT")
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

    scols = _columns(conn, "session")
    if "device" not in scols:
        conn.execute("ALTER TABLE session ADD COLUMN device TEXT")
    if "name" not in scols:
        conn.execute("ALTER TABLE session ADD COLUMN name TEXT")

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
    name: str | None = None,
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
        "SELECT last_seen FROM session WHERE session_id = ?", (session_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO session
               (session_id, first_seen, last_seen, name, device, cwd, project_dir,
                git_worktree, entrypoint, start_type, color_idx)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                ts,
                ts,
                name,
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
        # Compute the new last_seen in Python: SQLite's scalar MAX(a, b) and
        # Postgres's GREATEST(a, b) differ, so avoid both.
        last_seen = max(int(existing["last_seen"]), ts)
        conn.execute(
            """UPDATE session SET
                 last_seen = ?,
                 name = COALESCE(?, name),
                 device = COALESCE(?, device),
                 cwd = COALESCE(?, cwd),
                 project_dir = COALESCE(?, project_dir),
                 git_worktree = COALESCE(?, git_worktree),
                 entrypoint = COALESCE(?, entrypoint),
                 start_type = COALESCE(?, start_type)
               WHERE session_id = ?""",
            (last_seen, name, device, cwd, project_dir, git_worktree, entrypoint, start_type, session_id),
        )


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple]) -> None:
    conn.executemany(sql, rows)
    conn.commit()
