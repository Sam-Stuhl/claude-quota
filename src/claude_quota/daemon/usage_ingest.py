"""Store what ``claude-quota ship`` posts to ``/ingest/usage``.

Every write is an upsert keyed on a stable id (requestId, session id, promptId),
so a shipper that re-sends lines after a failed post changes nothing. The SQL is
the subset SQLite and Postgres share (ON CONFLICT ... DO UPDATE, CASE).
"""

from __future__ import annotations

from typing import Any

_REQ_COLS = (
    "request_id", "ts", "device", "session_id", "agent_id", "prompt_id", "model",
    "query_source", "agent_type", "skill", "mcp_server", "mcp_tool", "effort", "speed",
    "tok_input", "tok_output", "tok_cache_r", "tok_cache_w", "cost_usd",
)
_INT_COLS = {"ts", "tok_input", "tok_output", "tok_cache_r", "tok_cache_w"}


def _s(v: Any, n: int = 300) -> str | None:
    if v is None:
        return None
    return str(v)[:n]


def _i(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def _req_row(r: dict, device: str | None) -> tuple | None:
    if not isinstance(r, dict) or not r.get("request_id") or not r.get("session_id"):
        return None
    if _i(r.get("ts")) is None:
        return None
    vals = []
    for c in _REQ_COLS:
        v = device if c == "device" else r.get(c)
        if c in _INT_COLS:
            vals.append(_i(v) or 0)
        elif c == "cost_usd":
            vals.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0)
        else:
            vals.append(_s(v))
    return tuple(vals)


def store(conn, body: dict) -> dict:
    device = _s(body.get("device"), 80)
    counts = {}

    rows = [x for x in (_req_row(r, device) for r in body.get("requests") or []) if x]
    if rows:
        cols = ", ".join(_REQ_COLS)
        qs = ", ".join("?" for _ in _REQ_COLS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _REQ_COLS if c != "request_id")
        conn.executemany(
            f"INSERT INTO req_usage ({cols}) VALUES ({qs}) "
            f"ON CONFLICT(request_id) DO UPDATE SET {updates}",
            rows,
        )
    counts["requests"] = len(rows)

    convs = []
    for c in body.get("conversations") or []:
        if not isinstance(c, dict) or not c.get("session_id"):
            continue
        convs.append(
            (
                _s(c["session_id"]), device, _s(c.get("source"), 20), _s(c.get("cwd"), 500),
                _s(c.get("entrypoint"), 40), _s(c.get("git_branch"), 200), _s(c.get("cc_version"), 40),
                _s(c.get("title"), 200), _s(c.get("first_prompt"), 200),
                _i(c.get("first_ts")), _i(c.get("last_ts")),
            )
        )
    if convs:
        conn.executemany(
            """INSERT INTO conv (session_id, device, source, cwd, entrypoint, git_branch,
                                 cc_version, title, first_prompt, first_ts, last_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 device = COALESCE(conv.device, excluded.device),
                 source = COALESCE(conv.source, excluded.source),
                 cwd = COALESCE(conv.cwd, excluded.cwd),
                 entrypoint = COALESCE(conv.entrypoint, excluded.entrypoint),
                 git_branch = COALESCE(excluded.git_branch, conv.git_branch),
                 cc_version = COALESCE(excluded.cc_version, conv.cc_version),
                 title = COALESCE(excluded.title, conv.title),
                 first_prompt = COALESCE(conv.first_prompt, excluded.first_prompt),
                 first_ts = CASE WHEN conv.first_ts IS NULL OR excluded.first_ts < conv.first_ts
                                 THEN excluded.first_ts ELSE conv.first_ts END,
                 last_ts = CASE WHEN conv.last_ts IS NULL OR excluded.last_ts > conv.last_ts
                                THEN excluded.last_ts ELSE conv.last_ts END""",
            convs,
        )
    counts["conversations"] = len(convs)

    turns = [
        (_s(t["prompt_id"]), _s(t.get("session_id")), _s(t.get("agent_id")), _i(t.get("ts")),
         _s(t.get("origin"), 40), _s(t.get("door"), 40), _s(t.get("routine"), 80),
         _s(t.get("label"), 120))
        for t in body.get("turns") or []
        if isinstance(t, dict) and t.get("prompt_id") and t.get("session_id")
    ]
    if turns:
        conn.executemany(
            """INSERT INTO turn (prompt_id, session_id, agent_id, ts, origin, door, routine, label)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(prompt_id) DO UPDATE SET
                 label = COALESCE(turn.label, excluded.label),
                 origin = COALESCE(turn.origin, excluded.origin),
                 door = COALESCE(turn.door, excluded.door),
                 routine = COALESCE(turn.routine, excluded.routine)""",
            turns,
        )
    counts["turns"] = len(turns)

    subs = [
        (_s(a["agent_id"]), _s(a.get("session_id")), _s(a.get("agent_type"), 80),
         _s(a.get("description"), 200), _s(a.get("model"), 80))
        for a in body.get("subagents") or []
        if isinstance(a, dict) and a.get("agent_id") and a.get("session_id")
    ]
    if subs:
        conn.executemany(
            """INSERT INTO subagent (agent_id, session_id, agent_type, description, model)
               VALUES (?,?,?,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 agent_type = excluded.agent_type, description = excluded.description,
                 model = excluded.model""",
            subs,
        )
    counts["subagents"] = len(subs)

    atl = [
        (_s(a["claude_session_id"]), _s(a.get("atlas_id"), 80), _i(a.get("number")),
         _s(a.get("kind"), 40), _s(a.get("title"), 200), _s(a.get("started_by"), 80),
         _s(a.get("folder"), 200), _s(a.get("repo"), 120), _s(a.get("profile"), 40),
         _s(a.get("pr_url"), 300), _s(a.get("series_key"), 120), _i(a.get("created_ts")))
        for a in body.get("atlas_sessions") or []
        if isinstance(a, dict) and a.get("claude_session_id")
    ]
    if atl:
        conn.executemany(
            """INSERT INTO atlas_session (claude_session_id, atlas_id, number, kind, title,
                 started_by, folder, repo, profile, pr_url, series_key, created_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(claude_session_id) DO UPDATE SET
                 atlas_id = excluded.atlas_id, number = excluded.number,
                 kind = excluded.kind, title = excluded.title,
                 started_by = excluded.started_by, folder = excluded.folder,
                 repo = excluded.repo, profile = excluded.profile,
                 pr_url = excluded.pr_url, series_key = excluded.series_key,
                 created_ts = excluded.created_ts""",
            atl,
        )
    counts["atlas_sessions"] = len(atl)

    conn.commit()
    return counts
