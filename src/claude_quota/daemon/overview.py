"""``/api/overview``: where the quota went, answered in one pass.

One grouped query over the shipped requests of a window, each (session, model,
turn stamp) group placed by ``classify`` and named by ``workmap``, then rolled
up five ways: kind of work, project, model, session, host. Every roll-up sums to
the same total, so nothing can hide in a bucket that one view lacks. What
``classify`` could not place is reported as ``unclassified`` with its biggest
folders, so a leftover is explained rather than silently dropped.

Quota points are the measured percentage of the matching limit (5-hour window
or week) split by each row's share of list-price cost. The limits also carry
claude.ai app use, which no local record sees, so the points are a split of the
measured whole, not an exact bill.
"""

from __future__ import annotations

import time
from collections import defaultdict

from . import calibration, classify, limits, projection, summary, workmap

HOUR = 3600
WINDOWS = {"5h": 5 * HOUR, "24h": 24 * HOUR, "week": 7 * 24 * HOUR, "30d": 30 * 24 * HOUR}
TOP_SESSIONS = 15


def window_bounds(window: str, cur: dict, now: int) -> tuple[int, int, str]:
    """(start, end, basis). 5h and week follow the measured reset when it is live."""
    span = WINDOWS.get(window, WINDOWS["5h"])
    if window == "5h" and cur.get("five_h_reset") and cur.get("five_h_pct") is not None:
        return cur["five_h_reset"] - span, now, "since the window opened"
    if window == "week" and cur.get("seven_d_reset") and cur.get("seven_d_pct") is not None:
        return cur["seven_d_reset"] - span, now, "since the week reset"
    return now - span, now, "rolling"


def _rows(conn, start: int, end: int) -> list[dict]:
    rows = conn.execute(
        """SELECT r.session_id AS sid, r.model AS model, r.device AS device,
                  t.door AS door, t.routine AS routine,
                  SUM(r.cost_usd) AS cost, COUNT(*) AS n, MAX(r.ts) AS last_ts,
                  MIN(r.ts) AS first_ts,
                  SUM(r.tok_input + r.tok_output + r.tok_cache_r + r.tok_cache_w) AS toks
           FROM req_usage r LEFT JOIN turn t ON t.prompt_id = r.prompt_id
           WHERE r.ts > ? AND r.ts <= ?
           GROUP BY r.session_id, r.model, r.device, t.door, t.routine""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _lookup(conn, table: str, key: str, ids: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    ids_l = sorted(ids)
    for i in range(0, len(ids_l), 500):
        part = ids_l[i : i + 500]
        q = ", ".join("?" for _ in part)
        for r in conn.execute(f"SELECT * FROM {table} WHERE {key} IN ({q})", tuple(part)).fetchall():
            out[r[key]] = dict(r)
    return out


def _bucket() -> dict:
    return {"cost": 0.0, "n": 0, "toks": 0, "last_ts": 0, "sessions": set()}


def _add(b: dict, g: dict, sid: str) -> None:
    b["cost"] += float(g["cost"] or 0.0)
    b["n"] += int(g["n"] or 0)
    b["toks"] += int(g["toks"] or 0)
    b["last_ts"] = max(b["last_ts"], int(g["last_ts"] or 0))
    b["sessions"].add(sid)


def _rank(buckets: dict[str, dict], labels: dict[str, str], total: float, pct: float | None,
          limit: int | None = None, meta: dict | None = None) -> list[dict]:
    out = []
    for k, b in buckets.items():
        share = b["cost"] / total if total > 0 else 0.0
        item = {
            "key": k,
            "label": labels.get(k, k),
            "cost_usd": round(b["cost"], 2),
            "share": round(share, 4),
            "points": round(share * pct, 1) if pct is not None else None,
            "requests": b["n"],
            "sessions": len(b["sessions"]),
            "last_ts": b["last_ts"],
        }
        if meta and k in meta:
            item.update(meta[k])
        out.append(item)
    out.sort(key=lambda x: -x["cost_usd"])
    if limit and len(out) > limit:
        rest = out[limit:]
        out = out[:limit]
        cost = sum(x["cost_usd"] for x in rest)
        out.append({
            "key": "(smaller)", "label": f"{len(rest)} smaller", "cost_usd": round(cost, 2),
            "share": round(cost / total, 4) if total > 0 else 0.0,
            "points": round(sum(x["points"] or 0 for x in rest), 1) if pct is not None else None,
            "requests": sum(x["requests"] for x in rest), "sessions": sum(x["sessions"] for x in rest),
            "last_ts": max(x["last_ts"] for x in rest),
        })
    return out


def build(conn, window: str = "5h", now: int | None = None) -> dict:
    now = now or int(time.time())
    window = window if window in WINDOWS else "5h"
    cur = limits.read(conn, now)
    start, end, basis = window_bounds(window, cur, now)
    rows = _rows(conn, start, end)

    sids = {r["sid"] for r in rows}
    convs = _lookup(conn, "conv", "session_id", sids)
    atlas = _lookup(conn, "atlas_session", "claude_session_id", sids)

    pct = None
    if cur.get("known"):
        pct = cur["five_h_pct"] if window == "5h" else cur["seven_d_pct"] if window == "week" else None

    total = sum(float(r["cost"] or 0.0) for r in rows)
    base_cache: dict[str, classify.Place] = {}
    info_cache: dict[str, dict] = {}
    kinds, projects, models, hosts = (defaultdict(_bucket) for _ in range(4))
    sess: dict[str, dict] = {}
    sess_models: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    other_folders: dict[str, float] = defaultdict(float)
    labels_k = dict(workmap.KINDS)
    labels_p: dict[str, str] = {}
    unclassified = 0.0

    for g in rows:
        sid = g["sid"]
        conv, atl = convs.get(sid) or {}, atlas.get(sid) or {}
        base = base_cache.get(sid)
        if base is None:
            base = base_cache[sid] = classify.session_place(conv or None, atl or None, sid)
        place = classify.request_place(base, g["door"], g["routine"], g["last_ts"], g["model"])
        info = info_cache.get(sid)
        if info is None or place.category != info["category"]:
            project = workmap.project_name(place, conv, atl)
            info = info_cache[sid] = {
                "category": place.category,
                "project": project,
                "kind": workmap.kind_of(place, project, conv, atl),
                "label": classify.item_label(conv or None, atl or None, sid),
            }
        model = (g["model"] or "?").split("[", 1)[0]
        host = workmap.host_name(g["device"] or conv.get("device"))
        cost = float(g["cost"] or 0.0)
        if info["kind"] == "other":
            unclassified += cost
            other_folders[classify._home_rel(conv.get("cwd"))] += cost
        _add(kinds[info["kind"]], g, sid)
        _add(projects[info["project"]], g, sid)
        _add(models[model], g, sid)
        _add(hosts[host], g, sid)
        labels_p[info["project"]] = info["project"]
        s = sess.setdefault(sid, {**_bucket(), **info, "host": host, "first_ts": g["first_ts"],
                                   "started_by": atl.get("started_by"),
                                   "atlas_number": atl.get("number")})
        _add(s, g, sid)
        s["first_ts"] = min(s["first_ts"], g["first_ts"] or s["first_ts"])
        sess_models[sid][model] += cost

    sess_meta = {}
    for sid, s in sess.items():
        dom = max(sess_models[sid].items(), key=lambda kv: kv[1])[0]
        sess_meta[sid] = {
            "title": s["label"], "kind": workmap.KINDS[s["kind"]], "project": s["project"],
            "host": s["host"], "model": dom, "first_ts": s["first_ts"],
            "live": (now - s["last_ts"]) < 300, "started_by": s["started_by"],
        }
    sess_buckets = {sid: s for sid, s in sess.items()}
    session_items = _rank(sess_buckets, {sid: s["label"] for sid, s in sess.items()}, total, pct,
                          limit=TOP_SESSIONS, meta=sess_meta)

    five_start = (cur["five_h_reset"] - 5 * HOUR) if cur.get("five_h_pct") is not None and cur.get("five_h_reset") else now - 5 * HOUR
    return {
        "now": now,
        "window": window,
        "start": start,
        "end": end,
        "basis": basis,
        "totals": {
            "cost_usd": round(total, 2),
            "requests": sum(int(r["n"] or 0) for r in rows),
            "sessions": len(sids),
            "points": pct,
        },
        "limits": {
            "known": bool(cur.get("known")),
            "source": cur.get("source"),
            "age_s": cur.get("age_s"),
            "stale": cur.get("stale"),
            "five": {"pct": cur.get("five_h_pct"), "resets_at": cur.get("five_h_reset"),
                     "expired": cur.get("five_expired", False)},
            "seven": {"pct": cur.get("seven_d_pct"), "resets_at": cur.get("seven_d_reset"),
                      "expired": cur.get("seven_expired", False)},
        },
        "projection": projection.project(conn, now),
        "series": summary._series(conn, five_start, now),
        "five_start": five_start,
        "by": {
            "kind": _rank(kinds, labels_k, total, pct),
            "project": _rank(projects, labels_p, total, pct, limit=14),
            "model": _rank(models, {}, total, pct),
            "host": _rank(hosts, {}, total, pct),
            "session": session_items,
        },
        "unclassified": {
            "cost_usd": round(unclassified, 2),
            "share": round(unclassified / total, 4) if total > 0 else 0.0,
            "top_folders": [
                {"folder": f, "cost_usd": round(c, 2)}
                for f, c in sorted(other_folders.items(), key=lambda kv: -kv[1])[:3]
            ],
        },
        "calibrated": calibration.is_calibrated(conn),
    }
