"""``claude-quota ship``: send this machine's usage to the daemon.

Runs on every machine that runs Claude (a launchd agent, once a minute). Each
run reads what was appended to the transcripts since the last run, plus ATLAS's
own records on the machine that hosts it, and posts them to ``/ingest/usage``.
It sends metadata and token counts only. The one piece of text is an 80
character label per turn and per conversation, so a turn is recognisable in
the dashboard; ``--no-labels`` (or ``CLAUDE_QUOTA_LABELS=0``) turns that off.

Sources:

* ``~/.claude/projects/**.jsonl``: every Claude Code run, interactive or SDK,
  including ATLAS sessions, jobs, the assistant thread and their subagents.
* ``~/Library/Application Support/Claude/local-agent-mode-sessions``: Cowork.
* ``~/atlas/atlas.db`` (read-only): what each ATLAS session and job is, who or
  what started it, and its folder, joined to transcripts by session id.
* ``~/.claude/plugins/store/quota-meter_*.json``: the quota-meter mod's own
  samples of the 5-hour and 7-day percentages. Claude Code only runs the status
  line command when no mod owns the status line, so with the mod installed the
  status line feed goes silent while this file keeps the readings. They are
  posted to ``/ingest/limits`` as source ``quota-meter``.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import httpx

from . import config, transcripts

BATCH = 2000


def _home() -> Path:
    return Path(os.environ.get("CLAUDE_QUOTA_HOME") or Path.home())


def transcript_roots() -> list[Path]:
    return [
        config.claude_config_dir() / "projects",
        _home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions",
    ]


def atlas_db_path() -> Path:
    return Path(os.environ.get("CLAUDE_QUOTA_ATLAS_DB") or _home() / "atlas" / "atlas.db")



def state_path() -> Path:
    return config.runtime_dir() / "ship-state.json"


def labels_enabled() -> bool:
    return os.environ.get("CLAUDE_QUOTA_LABELS", "1").lower() not in ("0", "false", "no")


def _load_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    config.ensure_runtime_dir()
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_path())


def _files(since_ts: float) -> list[Path]:
    out = []
    for root in transcript_roots():
        if not root.exists():
            continue
        for p in root.rglob("*.jsonl"):
            try:
                if p.stat().st_mtime >= since_ts:
                    out.append(p)
            except OSError:
                continue
    return out


def collect_transcripts(state: dict, since_days: float, with_labels: bool) -> transcripts.Parsed:
    files_state: dict = state.setdefault("files", {})
    # Only files changed within since_days are looked at; one already read up
    # to its end costs a stat and nothing more.
    horizon = time.time() - since_days * 86400
    total = transcripts.Parsed()
    failed = 0
    seen_paths = _files(horizon)
    for path in seen_paths:
        key = str(path)
        fs = files_state.get(key)
        carry = transcripts.Carry.from_json(fs)
        try:
            parsed = transcripts.read_incremental(path, carry, with_labels=with_labels)
            quiet = path.stat().st_mtime < time.time() - 3600
        except OSError:
            failed += 1
            continue
        if quiet:
            carry.query_sources = {}  # a quiet file needs no api-request memory
        files_state[key] = carry.to_json()
        total.merge(parsed)
    # Forget files outside the horizon (or gone) so the state stays small. One
    # that comes back to life is re-read from the start; the upserts make that
    # harmless.
    live = {str(p) for p in seen_paths}
    for key in [k for k in files_state if k not in live]:
        del files_state[key]
    state["last_scan"] = time.time()
    state["failed_files"] = failed
    return total


QM_MAX_PER_RUN = 40
QM_MAX_AGE_S = 6 * 3600


def quota_meter_dir() -> Path:
    return Path(os.environ.get("CLAUDE_QUOTA_QM_DIR") or _home() / ".claude" / "plugins" / "store")


def _qm_ms(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    return None


def collect_limits(state: dict, now: float | None = None) -> list[dict]:
    """New quota-meter readings, as ``/ingest/limits`` bodies, oldest first.

    The mod writes one file per install; the one with the newest sample wins
    and anything older than six hours is ignored (a window that old is over).
    """
    now = now or time.time()
    best: dict | None = None
    for f in sorted(quota_meter_dir().glob("quota-meter_*.json")):
        try:
            samples = json.loads(f.read_text()).get("samples") or {}
        except (OSError, ValueError):
            continue
        five = samples.get("five_hour") or []
        if five and (best is None or five[-1].get("atMs", 0) > best["five"][-1].get("atMs", 0)):
            best = {"five": five, "seven": samples.get("seven_day") or []}
    if best is None:
        return []
    last = float(state.get("qm_last_ms") or 0)
    seven = {x.get("atMs"): x for x in best["seven"]}
    bodies = []
    for x in best["five"]:
        at = x.get("atMs")
        if not isinstance(at, (int, float)) or at <= last or now - at / 1000 > QM_MAX_AGE_S:
            continue
        windows = [{"id": "five_hour", "utilization": x.get("percentUsed"),
                    "resetsAt": _qm_ms(x.get("resetsAt"))}]
        sd = seven.get(at) or (best["seven"][-1] if best["seven"] else None)
        if sd:
            windows.append({"id": "seven_day", "utilization": sd.get("percentUsed"),
                            "resetsAt": _qm_ms(sd.get("resetsAt"))})
        bodies.append({"session_id": "quota-meter", "source": "quota-meter",
                       "usageLimits": {"capturedAt": at, "windows": windows}})
    return bodies[-QM_MAX_PER_RUN:]


def collect_atlas(state: dict) -> dict:
    """ATLAS sessions and jobs changed since the last run."""
    out: dict = {"atlas_sessions": []}
    db = atlas_db_path()
    if db.exists():
        since = state.get("atlas_since") or ""
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
            want = [c for c in (
                "id", "number", "kind", "title", "started_by", "model", "repo",
                "folder", "host", "cwd", "pr_url", "series_key", "created_at", "updated_at",
                "claude_session_id",
            ) if c in cols]
            rows = conn.execute(
                f"SELECT {', '.join(want)} FROM sessions WHERE claude_session_id IS NOT NULL "
                "AND updated_at >= ? ORDER BY updated_at",
                (since,),
            ).fetchall()
            newest = since
            for r in rows:
                d = dict(r)
                newest = max(newest, d.get("updated_at") or "")
                out["atlas_sessions"].append(_atlas_row(d))
            jcols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
            if {"session_id", "title"} <= jcols:
                # Jobs get their session id after they start, so a cursor on
                # created_at could step past one. The table is small: send the
                # rows that changed since the last run, keyed by content.
                seen = set(state.get("atlas_jobs_seen") or [])
                now_seen = set()
                jrows = conn.execute(
                    "SELECT id, session_id, title, profile, started_by, created_at, folder FROM jobs "
                    "WHERE session_id IS NOT NULL"
                ).fetchall()
                for r in jrows:
                    d = dict(r)
                    key = f"{d['id']}:{d['session_id']}"
                    now_seen.add(key)
                    if key in seen:
                        continue
                    out["atlas_sessions"].append(
                        {
                            "claude_session_id": d["session_id"],
                            "atlas_id": str(d["id"]),
                            "number": None,
                            "kind": "job",
                            "title": (d.get("title") or "")[:200] or None,
                            "started_by": d.get("started_by") or "job",
                            "folder": d.get("folder"),
                            "repo": None,
                            "profile": d.get("profile"),
                            "pr_url": None,
                            "series_key": None,
                            "created_ts": transcripts._ts(d.get("created_at")),
                        }
                    )
                state["atlas_jobs_seen"] = sorted(now_seen)
            conn.close()
            state["atlas_since"] = newest
        except sqlite3.Error:
            pass
    return out


def _atlas_row(d: dict) -> dict:
    return {
        "claude_session_id": d.get("claude_session_id"),
        "atlas_id": d.get("id"),
        "number": d.get("number"),
        "kind": d.get("kind"),
        "title": str(d.get("title") or "")[:200] or None,
        "started_by": d.get("started_by"),
        "folder": d.get("folder"),
        "repo": d.get("repo"),
        "profile": None,
        "pr_url": d.get("pr_url"),
        "series_key": d.get("series_key"),
        "created_ts": transcripts._ts(d.get("created_at")),
    }


def _post(server: str, token: str | None, body: dict) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = httpx.post(server.rstrip("/") + "/ingest/usage", json=body, headers=headers, timeout=60)
    r.raise_for_status()


def _post_limits(server: str, token: str | None, body: dict) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = httpx.post(server.rstrip("/") + "/ingest/limits", json=body, headers=headers, timeout=20)
    r.raise_for_status()


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def run(
    *,
    since_days: float = 30,
    dry_run: bool = False,
    with_labels: bool | None = None,
    server: str | None = None,
    token: str | None = None,
    device: str | None = None,
    post=None,
    post_limits=None,
) -> dict:
    """One ship pass. ``post`` is injectable for tests."""
    with_labels = labels_enabled() if with_labels is None else with_labels
    cfg = config.load_config()
    server = server or config.client_base_url()
    token = token or config.client_token()
    device = device or cfg.get("device") or socket.gethostname().split(".")[0]
    post = post or (lambda body: _post(server, token, body))

    state = _load_state()
    snapshot = json.dumps(state)
    # Limits first: they are tiny and they are what moves the projection, so a
    # slow or failing usage batch must never hold them back.
    qm = [] if dry_run else collect_limits(state)
    if qm:
        post_limits = post_limits or (lambda body: _post_limits(server, token, body))
        sent = 0
        try:
            for body in qm:
                body["device"] = device
                post_limits(body)
                state["qm_last_ms"] = body["usageLimits"]["capturedAt"]
                sent += 1
        except Exception as e:
            state["limits_error"] = str(e)[:200]
        snapshot = json.dumps(state)
    parsed = collect_transcripts(state, since_days, with_labels)
    atlas = collect_atlas(state)

    convs = [{"session_id": k, **v} for k, v in parsed.conversations.items()]
    result = {
        "device": device,
        "requests": len(parsed.requests),
        "conversations": len(convs),
        "turns": len(parsed.turns),
        "subagents": len(parsed.subagents),
        "atlas_sessions": len(atlas["atlas_sessions"]),
        "lines": parsed.lines,
        "bad_lines": parsed.bad_lines,
        "failed_files": state.get("failed_files", 0),
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    try:
        # Context first (conversations, ATLAS rows), then the requests, so the
        # daemon can classify requests the moment they land.
        post(
            {
                "device": device,
                "subagents": list(parsed.subagents.values()),
                "atlas_sessions": atlas["atlas_sessions"],
            }
        )
        for chunk in _chunks(convs, BATCH):
            post({"device": device, "conversations": chunk})
        for chunk in _chunks(list(parsed.turns.values()), BATCH):
            post({"device": device, "turns": chunk})
        for chunk in _chunks(list(parsed.requests.values()), BATCH):
            post({"device": device, "requests": chunk})
    except Exception as e:
        # Nothing is lost: the saved state is left as it was, so the next run
        # re-reads the same lines and the daemon's upserts make that harmless.
        _save_state({**json.loads(snapshot), "last_error": str(e)[:300], "last_error_at": time.time()})
        result["error"] = str(e)[:300]
        return result

    state["last_ok"] = time.time()
    state["last_result"] = result
    state.pop("last_error", None)
    _save_state(state)
    return result
