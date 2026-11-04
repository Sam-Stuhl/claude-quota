"""``/api/usage``: where usage went, general first and specific on demand.

Every request in a window is placed by ``classify`` at a path of
category / group / item (a session). Asking for a path returns that node and
its children, each with the same numbers: list-price cost, tokens, share of
the parent, estimated quota points, and the change from the previous window of
the same length. Below an item come its turns, subagents, MCP servers, skills
and models; below a turn come its individual requests. ``by`` pivots any node's
descendants on one dimension instead (model, mcp, skill, subagent, device,
effort), so "all browser MCP use, across everything" is ``by=mcp`` at the root.

At the root, the Claude app (claude.ai chat, voice, mobile) appears as the one
thing that cannot be broken down: the quota the status line measured that no
shipped request explains. It is only computed where samples exist and
calibration is trusted, and is labelled as an estimate.
"""

from __future__ import annotations

import bisect
import time
from collections import defaultdict

from . import calibration, classify

WINDOWS = {"5h": 5 * 3600, "24h": 86400, "today": None, "7d": 7 * 86400, "30d": 30 * 86400}
PIVOTS = {
    "model": "model",
    "mcp": "mcp_server",
    "skill": "skill",
    "subagent": "agent_type",
    "device": "device",
    "effort": "effort",
}
MAX_CHILDREN = 200


def window_bounds(window: str, now: int) -> tuple[int, int]:
    if window == "today":
        from datetime import datetime

        lt = datetime.fromtimestamp(now, classify.tz())
        start = int(lt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return start, now
    return now - WINDOWS.get(window, WINDOWS["7d"]), now


def _rows(conn, start: int, end: int) -> list[dict]:
    rows = conn.execute(
        """SELECT r.request_id, r.ts, r.device, r.session_id, r.agent_id, r.prompt_id,
                  r.model, r.query_source, r.agent_type, r.skill, r.mcp_server, r.mcp_tool,
                  r.effort, r.tok_input, r.tok_output, r.tok_cache_r, r.tok_cache_w, r.cost_usd,
                  t.door AS door, t.routine AS routine
           FROM req_usage r LEFT JOIN turn t ON t.prompt_id = r.prompt_id
           WHERE r.ts > ? AND r.ts <= ?""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _lookup(conn, table: str, key: str, ids: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    ids_l = list(ids)
    for i in range(0, len(ids_l), 500):
        part = ids_l[i : i + 500]
        q = ", ".join("?" for _ in part)
        for r in conn.execute(f"SELECT * FROM {table} WHERE {key} IN ({q})", tuple(part)).fetchall():
            out[r[key]] = dict(r)
    return out


class _Placer:
    """Caches per-session context so each request is classified cheaply."""

    def __init__(self, conn, rows: list[dict]):
        sids = {r["session_id"] for r in rows}
        self.conv = _lookup(conn, "conv", "session_id", sids)
        self.atlas = _lookup(conn, "atlas_session", "claude_session_id", sids)
        self._base: dict[str, classify.Place] = {}

    def place(self, r: dict) -> classify.Place:
        sid = r["session_id"]
        base = self._base.get(sid)
        if base is None:
            base = classify.session_place(self.conv.get(sid), self.atlas.get(sid), sid)
            self._base[sid] = base
        return classify.request_place(base, r.get("door"), r.get("routine"), r.get("ts"), r.get("model"))

    def label(self, sid: str) -> str:
        return classify.item_label(self.conv.get(sid), self.atlas.get(sid), sid)


def _acc() -> dict:
    return {"cost_usd": 0.0, "requests": 0, "tok_input": 0, "tok_output": 0,
            "tok_cache_r": 0, "tok_cache_w": 0, "est_points": 0.0, "first_ts": None,
            "last_ts": None, "estimated": False, "models": defaultdict(float)}


def _add(a: dict, r: dict, rate: float, estimated: bool = False) -> None:
    c = float(r["cost_usd"] or 0.0)
    a["cost_usd"] += c
    a["requests"] += 1
    for k in ("tok_input", "tok_output", "tok_cache_r", "tok_cache_w"):
        a[k] += int(r[k] or 0)
    a["est_points"] += c * rate
    ts = r["ts"]
    a["first_ts"] = ts if a["first_ts"] is None else min(a["first_ts"], ts)
    a["last_ts"] = ts if a["last_ts"] is None else max(a["last_ts"], ts)
    a["estimated"] = a["estimated"] or estimated
    a["models"][(r["model"] or "?").split("[", 1)[0]] += c


def _out(key: str, label: str, a: dict, parent_cost: float, prev: dict | None,
         calibrated: bool, kind: str, **extra) -> dict:
    cost = a["cost_usd"]
    prev_cost = prev["cost_usd"] if prev else 0.0
    node = {
        "key": key,
        "label": label,
        "kind": kind,
        "cost_usd": round(cost, 4),
        "share": round(cost / parent_cost, 4) if parent_cost > 0 else 0.0,
        "requests": a["requests"],
        "tok_input": a["tok_input"],
        "tok_output": a["tok_output"],
        "tok_cache_read": a["tok_cache_r"],
        "tok_cache_write": a["tok_cache_w"],
        "est_5h_points": round(a["est_points"], 2) if calibrated else None,
        "prev_cost_usd": round(prev_cost, 4),
        "change": round((cost - prev_cost) / prev_cost, 3) if prev_cost > 0 else None,
        "first_ts": a["first_ts"],
        "last_ts": a["last_ts"],
        "estimated": a["estimated"],
        "models": {m: round(v, 4) for m, v in sorted(a["models"].items(), key=lambda x: -x[1])},
    }
    node.update(extra)
    return node


def quota_measured(conn, start: int, end: int) -> dict:
    """Quota the status line saw used in a window, and the stretches it covers.

    Samples are grouped by reset period; within one, used points are the rise
    from the first sample in the window to the highest. Gaps (no Claude Code
    session open, so no samples) are simply not covered.
    """
    out = {"five_h_points": 0.0, "seven_d_points": 0.0, "covered": []}
    for pct_col, reset_col, key in (
        ("five_h_pct", "five_h_reset", "five_h_points"),
        ("seven_d_pct", "seven_d_reset", "seven_d_points"),
    ):
        rows = conn.execute(
            f"""SELECT {reset_col} AS reset, MIN(ts) AS t0, MAX(ts) AS t1, MAX({pct_col}) AS hi
                FROM quota_sample
                WHERE had_limits = 1 AND {pct_col} IS NOT NULL AND ts > ? AND ts <= ?
                GROUP BY {reset_col}""",
            (start, end),
        ).fetchall()
        for r in rows:
            first = conn.execute(
                f"""SELECT {pct_col} AS p FROM quota_sample
                    WHERE had_limits = 1 AND {pct_col} IS NOT NULL AND ts = ?
                      AND {reset_col} IS NOT DISTINCT FROM ?""",
                (r["t0"], r["reset"]),
            ).fetchone()
            lo = float(first["p"]) if first else float(r["hi"])
            out[key] += max(0.0, float(r["hi"]) - lo)
            if key == "five_h_points":
                out["covered"].append((int(r["t0"]), int(r["t1"])))
    return out


def _covered(ts: int, starts: list[int], spans: list[tuple[int, int]]) -> bool:
    i = bisect.bisect_right(starts, ts) - 1
    return i >= 0 and spans[i][0] <= ts <= spans[i][1]


def _residual(rows: list[dict], m: dict, rates: dict, calibrated: bool) -> dict:
    node = {
        "key": classify.RESIDUAL_KEY,
        "label": classify.RESIDUAL_LABEL,
        "kind": "residual",
        "measured_5h_points": round(m["five_h_points"], 2),
        "measured_7d_points": round(m["seven_d_points"], 2),
        "est_5h_points": None,
        "estimated": True,
        "note": None,
    }
    if not m["covered"]:
        node["note"] = "no status line samples in this window"
        return node
    if not calibrated:
        node["note"] = "calibration not trusted yet"
        return node
    spans = sorted(m["covered"])
    starts = [a for a, _ in spans]
    local = 0.0
    for r in rows:
        if _covered(r["ts"], starts, spans):
            local += float(r["cost_usd"] or 0.0) * calibration.rate_for(rates, r["model"] or "")
    node["est_5h_points"] = round(max(0.0, m["five_h_points"] - local), 2)
    return node


def _child_key(path: list[str], p: classify.Place) -> str | None:
    """The next path segment under ``path`` for a request placed at ``p``."""
    full = [p.category, p.group, p.item]
    if full[: len(path)] != path:
        return None
    return full[len(path)] if len(path) < 3 else None


def build(conn, window: str = "7d", path: list[str] | None = None, by: str | None = None,
          now: int | None = None) -> dict:
    now = now or int(time.time())
    path = [p for p in (path or []) if p]
    start, end = window_bounds(window, now)
    span = end - start
    rows = _rows(conn, start, end)
    prev_rows = _rows(conn, start - span, start)
    placer = _Placer(conn, rows + prev_rows)
    calibrated = calibration.is_calibrated(conn)
    rates = calibration.get_rates(conn) if calibrated else {}

    def rate(r):
        return calibration.rate_for(rates, r["model"] or "") if calibrated else 0.0

    turn_id = None
    if len(path) == 4:
        turn_id = path[3]
        path = path[:3]

    def in_path(r) -> classify.Place | None:
        p = placer.place(r)
        if [p.category, p.group, p.item][: len(path)] != path:
            return None
        return p

    mine = [(r, p) for r in rows if (p := in_path(r))]
    prev_mine = [(r, p) for r in prev_rows if (p := in_path(r))]

    node_acc = _acc()
    for r, p in mine:
        _add(node_acc, r, rate(r), p.estimated)
    prev_acc = _acc()
    for r, _p in prev_mine:
        _add(prev_acc, r, rate(r))

    resp = {
        "now": now,
        "window": window,
        "start": start,
        "end": end,
        "path": path + ([turn_id] if turn_id else []),
        "by": by,
        "calibrated": calibrated,
        "breadcrumbs": _crumbs(path, placer, turn_id, conn),
    }
    # How much of the weekly limit one point of the 5-hour limit was worth over
    # this window, from the two measured rises; multiply est_5h_points by it.
    measured = quota_measured(conn, start, end)
    resp["week_per_5h_point"] = (
        round(measured["seven_d_points"] / measured["five_h_points"], 4)
        if measured["five_h_points"] > 0
        else None
    )
    total_cost = node_acc["cost_usd"]
    resp["node"] = _out("/".join(path) or "all", resp["breadcrumbs"][-1]["label"], node_acc,
                        total_cost, prev_acc, calibrated, "node")

    if by:
        col = PIVOTS.get(by, "model")
        resp["children"] = _group(mine, prev_mine, lambda r, p: r.get(col) or "(none)",
                                  lambda k: k, total_cost, calibrated, rate, "pivot")
        return resp

    if turn_id:
        reqs = [r for r, _ in mine if r["prompt_id"] == turn_id and not r["agent_id"]]
        reqs += [r for r, _ in mine if r["agent_id"] and _agent_turn(conn, mine, r["agent_id"]) == turn_id]
        reqs.sort(key=lambda r: r["ts"])
        resp["requests"] = [
            {k: r[k] for k in ("request_id", "ts", "model", "query_source", "agent_type", "skill",
                               "mcp_server", "mcp_tool", "effort", "tok_input", "tok_output",
                               "tok_cache_r", "tok_cache_w", "cost_usd")}
            for r in reqs[:2000]
        ]
        return resp

    if len(path) < 3:
        def key(r, p):
            return _child_key(path, p)

        if len(path) == 0:
            def label(k):
                return classify.CATEGORY_LABEL.get(k, k)
        elif len(path) == 1:
            def label(k):
                return k
        else:
            label = placer.label
        kind = ("category", "group", "item")[len(path)]
        children = _group(mine, prev_mine, key, label, total_cost, calibrated, rate, kind)
        if len(path) == 2:
            for c in children:
                c.update(_item_meta(placer, c["key"]))
        if len(path) == 0:
            order = {k: i for i, (k, _) in enumerate(classify.CATEGORIES)}
            children.sort(key=lambda c: (-c["cost_usd"], order.get(c["key"], 99)))
            resp["residual"] = _residual(rows, measured, rates, calibrated)
        resp["children"] = children
        return resp

    # An item: one session, broken down every useful way.
    sid = path[2]
    resp["item"] = _item_meta(placer, sid)
    turns = _lookup(conn, "turn", "prompt_id", {r["prompt_id"] for r, _ in mine if r["prompt_id"]})
    subs = _lookup(conn, "subagent", "agent_id", {r["agent_id"] for r, _ in mine if r["agent_id"]})

    # A subagent's requests belong to the main-thread turn that was running
    # when it started, so turn shares add up to the whole session.
    main_turns = sorted(
        (t["ts"], pid) for pid, t in turns.items() if not t.get("agent_id") and t.get("ts")
    )
    mt_ts = [ts for ts, _ in main_turns]
    agent_start: dict[str, int] = {}
    for r, _p in mine:
        if r["agent_id"]:
            agent_start[r["agent_id"]] = min(agent_start.get(r["agent_id"], r["ts"]), r["ts"])
    agent_turn = {}
    for aid, ts in agent_start.items():
        i = bisect.bisect_right(mt_ts, ts) - 1
        agent_turn[aid] = main_turns[i][1] if i >= 0 else "(no turn)"

    def turn_key(r, p):
        if r["agent_id"]:
            return agent_turn.get(r["agent_id"], "(no turn)")
        return r["prompt_id"] or "(no turn)"

    def turn_label(k):
        t = turns.get(k) or {}
        return t.get("label") or ("(turn)" if k != "(no turn)" else "(before the first prompt)")

    turn_children = _group(mine, prev_mine, turn_key, turn_label, total_cost, calibrated, rate,
                           "turn", limit=5000)
    for c in turn_children:
        t = turns.get(c["key"]) or {}
        c["ts"] = t.get("ts") or c["first_ts"]
        c["origin"] = t.get("door") or t.get("origin")

    def sub_label(k):
        s = subs.get(k) or {}
        return " · ".join(x for x in (s.get("agent_type"), s.get("description")) if x) or k[:10]

    resp["sections"] = {
        "turns": turn_children,
        "subagents": _group(mine, prev_mine, lambda r, p: r["agent_id"], sub_label,
                            total_cost, calibrated, rate, "subagent"),
        "mcp": _group(mine, prev_mine, lambda r, p: r["mcp_server"], lambda k: k,
                      total_cost, calibrated, rate, "pivot"),
        "skills": _group(mine, prev_mine, lambda r, p: r["skill"], lambda k: k,
                         total_cost, calibrated, rate, "pivot"),
        "models": _group(mine, prev_mine, lambda r, p: (r["model"] or "?").split("[", 1)[0],
                         lambda k: k, total_cost, calibrated, rate, "pivot"),
    }
    return resp


def _group(mine, prev_mine, keyfn, labelfn, parent_cost, calibrated, rate, kind,
           limit: int = MAX_CHILDREN) -> list[dict]:
    accs: dict[str, dict] = {}
    for r, p in mine:
        k = keyfn(r, p)
        if k is None:
            continue
        _add(accs.setdefault(k, _acc()), r, rate(r), p.estimated)
    prevs: dict[str, dict] = {}
    for r, p in prev_mine:
        k = keyfn(r, p)
        if k is None or k not in accs:
            continue
        _add(prevs.setdefault(k, _acc()), r, rate(r))
    out = [_out(k, labelfn(k), a, parent_cost, prevs.get(k), calibrated, kind) for k, a in accs.items()]
    out.sort(key=lambda c: -c["cost_usd"])
    return out[:limit]


def _agent_turn(conn, mine, agent_id: str) -> str | None:
    """The main-thread turn that was running when a subagent started."""
    sid = next((r["session_id"] for r, _ in mine if r["agent_id"] == agent_id), None)
    start = min((r["ts"] for r, _ in mine if r["agent_id"] == agent_id), default=None)
    if sid is None or start is None:
        return None
    row = conn.execute(
        """SELECT prompt_id FROM turn WHERE session_id = ? AND agent_id IS NULL
             AND ts IS NOT NULL AND ts <= ? ORDER BY ts DESC LIMIT 1""",
        (sid, start),
    ).fetchone()
    return row["prompt_id"] if row else None


def _item_meta(placer: _Placer, sid: str) -> dict:
    conv = placer.conv.get(sid) or {}
    atlas = placer.atlas.get(sid) or {}
    return {
        "session_id": sid,
        "title": placer.label(sid),
        "device": conv.get("device"),
        "cwd": conv.get("cwd"),
        "entrypoint": conv.get("entrypoint"),
        "source": conv.get("source"),
        "started_by": atlas.get("started_by"),
        "atlas_number": atlas.get("number"),
        "atlas_id": atlas.get("atlas_id"),
        "folder": atlas.get("folder"),
        "pr_url": atlas.get("pr_url"),
        "session_first_ts": conv.get("first_ts"),
        "session_last_ts": conv.get("last_ts"),
    }


def _crumbs(path: list[str], placer: _Placer, turn_id: str | None, conn) -> list[dict]:
    crumbs = [{"path": [], "label": "Everything"}]
    if len(path) >= 1:
        crumbs.append({"path": path[:1], "label": classify.CATEGORY_LABEL.get(path[0], path[0])})
    if len(path) >= 2:
        crumbs.append({"path": path[:2], "label": path[1]})
    if len(path) >= 3:
        crumbs.append({"path": path[:3], "label": placer.label(path[2])})
    if turn_id:
        t = conn.execute("SELECT label FROM turn WHERE prompt_id = ?", (turn_id,)).fetchone()
        crumbs.append({"path": path + [turn_id], "label": (t["label"] if t else None) or "turn"})
    return crumbs
