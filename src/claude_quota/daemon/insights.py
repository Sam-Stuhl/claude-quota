"""``/ingest/insights``: real usage in the units that run out, for ATLAS's Usage page.

Everything here is in points of the weekly limit (percent of the week), not
dollars, because the week is what Sam watches. Each request's list-price cost
is converted with its own week's percent per dollar: the measured peak of that
week's 7-day reading over the cost shipped in it, or the current reading for
the week under way, or (with neither) the most recent ratio known. So a day's
total is "how much of the week this day ate", and this week's days add up to
the measured weekly percentage.

Days start at the weekly reset's clock time (01:00 for Sam), the same days the
budget uses, so a late night counts toward the day it belongs to.

What it serves:

- ``today``: hour by hour by kind of work, the running total against today's
  budget, the last hour's burn, and the sessions that spent it
- ``days``: each of the last N days, by kind, with background split out
- ``weeks``: each week's total (measured when a reading exists)
- ``ranges``: today, this week and 30 days, ranked by kind, project and session

Session labels are prompt text, which is why this is on the token route.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta

from . import budget, classify, limits, overview, workmap

HOUR = 3600
DAY = 86400
TOP_SESSIONS = 10
TOP_PROJECTS = 12


def _ratios(conn, resets: list[int], cur: dict, current_reset: int) -> dict[int, float]:
    """%/$ for each week window (keyed by its reset).

    Resets are found by stepping back whole weeks from the current one, so a
    week on the other side of a daylight saving change is an hour off the reset
    its readings carry: match readings within two hours."""
    out: dict[int, float] = {}
    for reset in resets:
        cost = conn.execute(
            "SELECT SUM(cost_usd) AS c FROM req_usage WHERE ts > ? AND ts <= ?", (reset - budget.WEEK, reset)
        ).fetchone()["c"] or 0.0
        if reset == current_reset and cur.get("seven_d_pct") is not None and cur.get("seven_d_reset") == reset:
            pct = float(cur["seven_d_pct"])
        else:
            row = conn.execute(
                "SELECT MAX(seven_d_pct) AS p FROM quota_sample WHERE seven_d_reset BETWEEN ? AND ?",
                (reset - 2 * HOUR, reset + 2 * HOUR),
            ).fetchone()
            pct = float(row["p"]) if row and row["p"] is not None else None
        if pct is not None and cost >= 5:
            out[reset] = pct / cost
    known = [out[r] for r in sorted(out, reverse=True)]
    fallback = known[0] if known else budget.DEFAULT_RATIO
    return {r: out.get(r, fallback) for r in resets} | {"_measured": set(out)}  # type: ignore[dict-item]


def _rank(buckets: dict[str, float], labels: dict[str, str] | None = None, limit: int = 99) -> list[dict]:
    items = sorted(((k, v) for k, v in buckets.items() if v > 0.0005), key=lambda kv: -kv[1])
    out = [{"key": k, "label": (labels or {}).get(k, k), "pct": round(v, 2)} for k, v in items[:limit]]
    rest = items[limit:]
    if rest:
        out.append({"key": "(smaller)", "label": f"{len(rest)} smaller", "pct": round(sum(v for _, v in rest), 2)})
    return out


def build(conn, now: int | None = None, days: int = 28) -> dict:
    now = now or int(time.time())
    days = max(7, min(int(days), 60))
    reset, cur = budget._week_reset(conn, now)
    if reset is None:
        return {"known": False, "reason": "no 7-day reading yet", "now": now}
    tz = classify.tz()

    # Day boundaries at the reset's clock time, going back from this week's.
    week_start = datetime.fromtimestamp(reset - budget.WEEK, tz)
    first = week_start - timedelta(days=((days + 6) // 7) * 7)
    bounds = []
    d = first
    while int(d.timestamp()) <= now:
        bounds.append(int(d.timestamp()))
        d = d + timedelta(days=1)
    bounds.append(int(d.timestamp()))
    # Keep only the last `days` days (today included).
    bounds = bounds[-(days + 1):]
    start = bounds[0]

    resets = []
    r = reset
    while r - budget.WEEK > start - budget.WEEK:
        resets.append(r)
        r -= budget.WEEK
    ratio = _ratios(conn, resets, cur, reset)
    measured = ratio.pop("_measured")  # type: ignore[arg-type]

    def week_of(ts: int) -> int:
        k = (reset - ts - 1) // budget.WEEK if ts < reset else 0
        return reset - int(k) * budget.WEEK

    rows = [dict(x) for x in conn.execute(
        """SELECT r.ts AS ts, r.session_id AS sid, r.model AS model, r.device AS device,
                  r.cost_usd AS cost, t.door AS door, t.routine AS routine
           FROM req_usage r LEFT JOIN turn t ON t.prompt_id = r.prompt_id
           WHERE r.ts > ? AND r.ts <= ? ORDER BY r.ts""",
        (start, now),
    ).fetchall()]
    sids = {x["sid"] for x in rows}
    convs = overview._lookup(conn, "conv", "session_id", sids)
    atlas = overview._lookup(conn, "atlas_session", "claude_session_id", sids)

    n_days = len(bounds) - 1
    t = n_days - 1
    today_start = bounds[t]
    day_kind = [defaultdict(float) for _ in range(n_days)]
    day_bg = [0.0] * n_days
    day_proj = [defaultdict(float) for _ in range(n_days)]
    hours = [defaultdict(float) for _ in range(24)]
    cum: list[list[float]] = []
    week_kind: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    ranges = {
        "today": today_start,
        "week": reset - budget.WEEK,
        "30d": max(start, now - 30 * DAY),
    }
    rk = {k: defaultdict(float) for k in ranges}
    rp = {k: defaultdict(float) for k in ranges}
    rm = {k: defaultdict(float) for k in ranges}
    rs: dict[str, dict[str, float]] = {k: defaultdict(float) for k in ranges}
    sess_info: dict[str, dict] = {}
    last_seen: dict[str, int] = {}
    base: dict[str, classify.Place] = {}
    info: dict[tuple, tuple] = {}
    running = 0.0
    burn_hour = 0.0
    burn_15 = 0.0

    di = 0
    for x in rows:
        ts = int(x["ts"])
        while di < n_days - 1 and ts >= bounds[di + 1]:
            di += 1
        sid = x["sid"]
        if sid not in base:
            base[sid] = classify.session_place(convs.get(sid), atlas.get(sid), sid)
        place = classify.request_place(base[sid], x["door"], x["routine"], ts, x["model"])
        key = (sid, place.category, place.group)
        if key not in info:
            conv, atl = convs.get(sid) or {}, atlas.get(sid) or {}
            project = workmap.project_name(place, conv, atl)
            info[key] = (project, workmap.kind_of(place, project, conv, atl))
        project, kind = info[key]
        pct = float(x["cost"] or 0.0) * ratio[week_of(ts)]
        bg = place.category in budget.BACKGROUND

        day_kind[di][kind] += pct
        day_proj[di][project] += pct
        if bg:
            day_bg[di] += pct
        week_kind[week_of(ts)][kind] += pct
        if ts >= today_start:
            hours[min(23, (ts - today_start) // HOUR)][kind] += pct
            running += pct
            if not cum or ts - cum[-1][0] >= 300:
                cum.append([ts, round(running, 2)])
            else:
                cum[-1] = [ts, round(running, 2)]
            if ts > now - HOUR:
                burn_hour += pct
            if ts > now - 900:
                burn_15 += pct
        model = (x["model"] or "?").split("[", 1)[0]
        for name, since in ranges.items():
            if ts > since:
                rk[name][kind] += pct
                rp[name][project] += pct
                rm[name][model] += pct
                rs[name][sid] += pct
        last_seen[sid] = ts
        if sid not in sess_info:
            conv, atl = convs.get(sid) or {}, atlas.get(sid) or {}
            sess_info[sid] = {
                "label": classify.item_label(conv or None, atl or None, sid)[:120],
                "atlas_id": atl.get("atlas_id"), "atlas_number": atl.get("number"),
                "project": project, "kind": kind, "kind_label": workmap.KINDS.get(kind, kind),
                "host": workmap.host_name(x["device"] or conv.get("device")),
                "background": bg,
            }

    def sessions(bucket: dict[str, float]) -> list[dict]:
        top = sorted(bucket.items(), key=lambda kv: -kv[1])[:TOP_SESSIONS]
        return [{"sid": s, **sess_info[s], "pct": round(v, 2), "last_ts": last_seen[s],
                 "live": now - last_seen[s] < 300} for s, v in top if v > 0.005]

    b = budget.build(conn, now)
    today_budget = b["today"]["budget_pct"] if b.get("known") else None

    day_list = []
    for i in range(n_days):
        dd = datetime.fromtimestamp(bounds[i], tz).date()
        tot = sum(day_kind[i].values())
        top = max(day_proj[i].items(), key=lambda kv: kv[1]) if day_proj[i] else ("", 0.0)
        day_list.append({
            "date": dd.isoformat(), "dow": dd.strftime("%a"), "start": bounds[i],
            "total": round(tot, 2), "background": round(day_bg[i], 2),
            "by_kind": {k: round(v, 2) for k, v in day_kind[i].items() if v > 0.005},
            "top_project": top[0], "top_project_pct": round(top[1], 2),
            "estimated": week_of(bounds[i]) not in measured,
        })

    weeks = []
    for r in sorted(resets):
        tot = sum(week_kind[r].values())
        weeks.append({
            "start": r - budget.WEEK, "reset": r, "total": round(tot, 1),
            "measured": r in measured, "current": r == reset,
            "partial": r - budget.WEEK < start,
            "by_kind": {k: round(v, 2) for k, v in week_kind[r].items() if v > 0.005},
        })

    # When today's budget runs out at the last hour's pace, if it does today.
    runs_out = None
    if today_budget is not None and burn_hour > 0.05 and running < today_budget:
        runs_out = now + int((today_budget - running) / burn_hour * HOUR)
        if runs_out >= bounds[-1]:
            runs_out = None

    return {
        "known": True,
        "now": now,
        "kinds": workmap.KINDS,
        "day_starts": datetime.fromtimestamp(reset, tz).strftime("%H:%M"),
        "limits": {"five_h_pct": limits.read(conn, now).get("five_h_pct"), "seven_d_pct": cur.get("seven_d_pct")},
        "today": {
            "start": today_start, "end": bounds[-1],
            "total": round(running, 2), "budget_pct": today_budget,
            "hours": [{"at": today_start + h * HOUR, "total": round(sum(hours[h].values()), 2),
                       "by_kind": {k: round(v, 2) for k, v in hours[h].items() if v > 0.005}} for h in range(24)],
            "cumulative": cum,
            "burn_last_hour": round(burn_hour, 2),
            "burn_last_15m": round(burn_15, 2),
            "runs_out_at": runs_out,
        },
        "days": day_list,
        "weeks": weeks,
        "ranges": {
            name: {
                "since": since,
                "total": round(sum(rk[name].values()), 2),
                "by_kind": _rank(rk[name], workmap.KINDS),
                "by_project": _rank(rp[name], None, TOP_PROJECTS),
                "by_model": _rank(rm[name]),
                "sessions": sessions(rs[name]),
            }
            for name, since in ranges.items()
        },
        "budget": b if b.get("known") else None,
    }
