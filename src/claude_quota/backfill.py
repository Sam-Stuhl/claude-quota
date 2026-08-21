"""One-shot transcript importer. QUARANTINED: nothing else may read transcripts.

The Claude Code transcript format is internal and changes between versions, so
this lives in exactly one module, the daemon never imports it, and a parse
failure warns and skips the file rather than taking anything down. When a
release breaks it, the cost is one broken script.

It backfills local usage (cost and tokens) into ``usage_bucket`` so the cost
and trend views are not empty on day one. It deliberately does NOT synthesize
``quota_sample`` or ``window_history``: transcripts carry no server-side quota
percentage, and we never fabricate quota truth.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from . import config, db
from .daemon.ingest import parse_reset


def _transcript_files(days: int) -> list[Path]:
    root = config.claude_config_dir() / "projects"
    if not root.exists():
        return []
    cutoff = time.time() - days * 86400
    files = []
    for p in root.glob("*/*.jsonl"):
        try:
            if p.stat().st_mtime >= cutoff:
                files.append(p)
        except OSError:
            continue
    return files


def _extract(entry: dict) -> dict | None:
    """Best-effort extraction of a usage record from one transcript line."""
    msg = entry.get("message") or {}
    usage = msg.get("usage") or entry.get("usage") or {}
    if not usage:
        return None
    ts = parse_reset(entry.get("timestamp"))
    if ts is None:
        return None
    session_id = entry.get("sessionId") or entry.get("session_id")
    if not session_id:
        return None
    model = msg.get("model") or entry.get("model") or ""
    cost = entry.get("costUSD") or entry.get("cost") or msg.get("costUSD") or 0.0
    return {
        "ts": ts,
        "session_id": session_id,
        "model": model,
        "cost": float(cost) if isinstance(cost, (int, float)) else 0.0,
        "tok_input": int(usage.get("input_tokens", 0) or 0),
        "tok_output": int(usage.get("output_tokens", 0) or 0),
        "tok_cache_r": int(usage.get("cache_read_input_tokens", 0) or 0),
        "tok_cache_w": int(usage.get("cache_creation_input_tokens", 0) or 0),
    }


def _import_file(conn: sqlite3.Connection, path: Path) -> int:
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = _extract(entry)
            if rec is None:
                continue
            bts = rec["ts"] - (rec["ts"] % config.BUCKET_SECONDS)
            db.upsert_session(conn, rec["session_id"], rec["ts"], entrypoint="backfill")
            conn.execute(
                """INSERT INTO usage_bucket
                   (ts, session_id, model, query_source, agent_name, skill_name,
                    mcp_server, plugin_name, effort, cost_usd, tok_input,
                    tok_output, tok_cache_r, tok_cache_w, active_ms)
                   VALUES (?,?,?,'','','','','','',?,?,?,?,?,0)
                   ON CONFLICT(ts, session_id, model, query_source, agent_name,
                               skill_name, mcp_server, plugin_name, effort)
                   DO UPDATE SET
                     cost_usd    = usage_bucket.cost_usd + excluded.cost_usd,
                     tok_input   = usage_bucket.tok_input + excluded.tok_input,
                     tok_output  = usage_bucket.tok_output + excluded.tok_output,
                     tok_cache_r = usage_bucket.tok_cache_r + excluded.tok_cache_r,
                     tok_cache_w = usage_bucket.tok_cache_w + excluded.tok_cache_w""",
                (
                    bts,
                    rec["session_id"],
                    rec["model"],
                    rec["cost"],
                    rec["tok_input"],
                    rec["tok_output"],
                    rec["tok_cache_r"],
                    rec["tok_cache_w"],
                ),
            )
            n += 1
    conn.commit()
    return n


def run(days: int = 30, conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or db.connect()
    files_ok = files_failed = records = 0
    try:
        for path in _transcript_files(days):
            try:
                records += _import_file(conn, path)
                files_ok += 1
            except Exception as e:  # a broken file must not stop the import
                print(f"warning: skipping {path}: {e}", file=sys.stderr)
                files_failed += 1
    finally:
        if own:
            conn.close()
    return {
        "days": days,
        "files_ok": files_ok,
        "files_failed": files_failed,
        "records": records,
    }
