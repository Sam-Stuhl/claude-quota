"""The weekly budget: the 7-day limit split across the days of the week.

Sam's weekly limit resets at a fixed time (Wednesday 01:00 for his account), so
the week is that window, cut into seven days that start at the reset's clock
time. Each day gets a share of the limit by its type:

- ``work``: a Lumasoft shift day (from Cedar when configured, else the pattern)
- ``school``: an ordinary weekday
- ``light``: weekends and days off

The default weights come from his own history (Sep 28 to Nov 10, both machines
shipping): a work day averaged $182 of list-price use, a school day $118 and a
weekend day $53, so work 1.55, school 1.0, light 0.45. A reserve (10% by
default) is never planned, so a bad day has somewhere to land.

**Today's budget is dynamic.** It is what is left of the planned part of the
week (``allocatable - used before today``) split over today and the days still
to come by their weights, so a day not fully used raises the allowance for the
rest of the week and an overspent day lowers it. It is fixed for the day: what
today spends does not move today's own number, only tomorrow's. Two bounds keep
it sane: a floor (``daily_floor`` of the day's fixed plan share, never more than
what is left) so a bad day cannot starve the rest to nothing, and a cap
(``daily_cap`` times the fixed plan share) so one underspent stretch cannot let a
single day eat everything. The last day of the week has no cap: it gets all that
is left of the plan, the reserve still untouched. The week resets with the 7-day
window, and the first day of the new week starts from the full allocatable share
again. The fixed plan (each day's share from the start of the week) is kept
beside it, so "ahead of pace" means the week has used more than the plan allowed
through the end of today.

**What the daily number holds, and what it never does.** It holds background work
(``background``), which also covers agents' session requests and Start cards.
It never blocks the main ATLAS chat: ``chat`` is advice on how to run it, a tier
cap that degrades the model (``sonnet``, then ``haiku``) at the weekly and 5-hour
guards, and at most a ``sonnet`` cap when only the daily number is passed. The
chat is always ``available``.

**Where the numbers come from.** The week's percentage is the measured 7-day
reading (the only truth). How it splits across days comes from shipped
list-price cost: each day's cost times the week's measured percent per dollar,
so the days always add up to the measured total. Last week that ratio held
steady at 0.083 to 0.095 %/$ from Thursday to Wednesday. With no live reading
for this week, the last closed week's ratio estimates it and the answer says so.

**Background.** Scheduled runs, role check-ins, ATLAS's helpers, event-triggered
jobs and tasks agents started are background (``classify`` categories). They
get a share of each day's budget (25% by default, about what they used in the
last 30 days), and ``background.level`` tells ATLAS whether to start one:
``hold`` when the week is into the reserve, today's budget is spent, background
has used its share, the 5-hour window is past the guard, or Sam is on a
Lumasoft shift; ``warn`` when today is nearly spent or the week is ahead.

It reports and advises. Nothing here stops a session; ATLAS chooses to defer.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

from . import cedar, classify, limits, overview

WEEK = 7 * 86400
DAY_TYPES = ("work", "school", "light")
TYPE_LABEL = {"work": "Work day", "school": "School day", "light": "Light day"}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BACKGROUND = frozenset({"scheduled", "helpers", "event", "roles", "tasks_agent"})
# Measured on the week ending 2026-11-11 (78% for $914); used only until this
# account has a closed week of its own to learn from.
DEFAULT_RATIO = 0.085
SETTING_KEY = "budget_plan"

DEFAULT_PLAN: dict = {
    "weights": {"work": 1.55, "school": 1.0, "light": 0.45},
    "week": {"mon": "work", "tue": "school", "wed": "school", "thu": "work",
             "fri": "school", "sat": "light", "sun": "light"},
    "reserve_pct": 10.0,
    "background_share": 0.25,
    "five_h_guard": 70.0,
    "five_h_hard": 90.0,
    "daily_floor": 0.4,
    "daily_cap": 2.0,
    "near_at": 0.8,
    "use_cedar": True,
    "overrides": {},
}


class PlanError(ValueError):
    """A plan change that does not validate."""


# ---- the plan --------------------------------------------------------------

def load_plan(conn) -> dict:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,)).fetchone()
    plan = json.loads(json.dumps(DEFAULT_PLAN))
    if row:
        try:
            saved = json.loads(row["value"])
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(plan.get(k), dict) and k != "overrides":
                    plan[k].update(v)
                else:
                    plan[k] = v
        except (ValueError, AttributeError):
            pass
    return plan


def _num(v, lo: float, hi: float, name: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError) as e:
        raise PlanError(f"{name} must be a number") from e
    if not lo <= x <= hi:
        raise PlanError(f"{name} must be between {lo:g} and {hi:g}")
    return x


def merge_plan(plan: dict, change: dict) -> dict:
    """Apply a partial change to a plan, validating every field it touches."""
    out = json.loads(json.dumps(plan))
    for k, v in change.items():
        if k == "weights":
            for t, w in dict(v).items():
                if t not in DAY_TYPES:
                    raise PlanError(f"unknown day type {t!r} (work, school, light)")
                out["weights"][t] = _num(w, 0, 5, f"weight for {t}")
        elif k == "week":
            for d, t in dict(v).items():
                d = str(d).lower()[:3]
                if d not in WEEKDAYS:
                    raise PlanError(f"unknown weekday {d!r}")
                if t not in DAY_TYPES:
                    raise PlanError(f"unknown day type {t!r} (work, school, light)")
                out["week"][d] = t
        elif k == "overrides":
            for d, t in dict(v).items():
                try:
                    date.fromisoformat(str(d))
                except ValueError as e:
                    raise PlanError(f"override dates are YYYY-MM-DD, not {d!r}") from e
                if t in (None, "", "auto"):
                    out["overrides"].pop(str(d), None)
                elif t in DAY_TYPES:
                    out["overrides"][str(d)] = t
                else:
                    raise PlanError(f"unknown day type {t!r} (work, school, light, auto)")
        elif k == "reserve_pct":
            out[k] = _num(v, 0, 50, "reserve")
        elif k == "background_share":
            x = _num(v, 0, 100, "background share")
            out[k] = x / 100 if x > 1 else x
        elif k == "five_h_guard":
            out[k] = _num(v, 0, 100, "5-hour guard")
        elif k == "five_h_hard":
            out[k] = _num(v, 0, 100, "5-hour hard guard")
        elif k == "daily_floor":
            out[k] = _num(v, 0, 1, "daily floor")
        elif k == "daily_cap":
            out[k] = _num(v, 1, 10, "daily cap")
        elif k == "near_at":
            out[k] = _num(v, 0.1, 1, "near threshold")
        elif k == "use_cedar":
            out[k] = bool(v)
        else:
            raise PlanError(f"unknown plan field {k!r}")
    if sum(out["weights"][out["week"][d]] for d in WEEKDAYS) <= 0:
        raise PlanError("every day weighs zero; nothing to split the week by")
    # Overrides for days long past are dropped so the list stays short.
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    out["overrides"] = {d: t for d, t in out["overrides"].items() if d >= cutoff}
    return out


def save_plan(conn, change: dict, now: int | None = None) -> dict:
    plan = merge_plan(load_plan(conn), change)
    saved = {k: v for k, v in plan.items()}
    conn.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SETTING_KEY, json.dumps(saved), now or int(time.time())),
    )
    conn.commit()
    return plan


# ---- the week --------------------------------------------------------------

def _week_reset(conn, now: int) -> tuple[int | None, dict]:
    cur = limits.read(conn, now)
    reset = cur.get("seven_d_reset") if cur.get("known") else None
    if reset is None:
        return None, cur
    reset = int(reset)
    while reset <= now:  # the reading is from a week that has ended: same weekly cadence
        reset += WEEK
    while reset - WEEK > now:  # asked about an earlier week (a replay)
        reset -= WEEK
    return reset, cur


def _ratio(conn, reset: int) -> float:
    """%/$ for the most recent closed week that has a measured peak."""
    rows = conn.execute(
        "SELECT seven_d_reset AS r, MAX(seven_d_pct) AS p FROM quota_sample "
        "WHERE seven_d_reset IS NOT NULL AND seven_d_reset < ? AND seven_d_pct IS NOT NULL "
        "GROUP BY seven_d_reset ORDER BY seven_d_reset DESC LIMIT 1",
        (reset,),
    ).fetchall()
    for row in rows:
        cost = conn.execute(
            "SELECT SUM(cost_usd) AS c FROM req_usage WHERE ts > ? AND ts <= ?",
            (int(row["r"]) - WEEK, int(row["r"])),
        ).fetchone()["c"] or 0.0
        if cost >= 50 and row["p"]:
            return float(row["p"]) / float(cost)
    return DEFAULT_RATIO


def _costs(conn, bounds: list[int], now: int) -> tuple[list[float], list[float]]:
    """(all cost, background cost) per day, from shipped requests."""
    rows = [dict(r) for r in conn.execute(
        """SELECT r.ts AS ts, r.session_id AS sid, r.model AS model, r.cost_usd AS cost,
                  t.door AS door, t.routine AS routine
           FROM req_usage r LEFT JOIN turn t ON t.prompt_id = r.prompt_id
           WHERE r.ts > ? AND r.ts <= ?""",
        (bounds[0], min(now, bounds[-1])),
    ).fetchall()]
    sids = {r["sid"] for r in rows}
    convs = overview._lookup(conn, "conv", "session_id", sids)
    atlas = overview._lookup(conn, "atlas_session", "claude_session_id", sids)
    base: dict[str, classify.Place] = {}
    total = [0.0] * 7
    bg = [0.0] * 7
    for r in rows:
        i = _day_index(bounds, int(r["ts"]))
        if i is None:
            continue
        sid = r["sid"]
        if sid not in base:
            base[sid] = classify.session_place(convs.get(sid), atlas.get(sid), sid)
        place = classify.request_place(base[sid], r["door"], r["routine"], r["ts"], r["model"])
        c = float(r["cost"] or 0.0)
        total[i] += c
        if place.category in BACKGROUND:
            bg[i] += c
    return total, bg


def _day_index(bounds: list[int], ts: int) -> int | None:
    for i in range(7):
        if bounds[i] <= ts < bounds[i + 1] or (i == 6 and ts == bounds[7]):
            return i
    return None


def _day_type(d: date, plan: dict, by_day: set[date] | None, last_shift: date | None) -> tuple[str, str]:
    key = d.isoformat()
    if key in plan["overrides"]:
        return plan["overrides"][key], "override"
    base = plan["week"][WEEKDAYS[d.weekday()]]
    if by_day is not None and last_shift is not None and d <= last_shift:
        if d in by_day:
            return "work", "cedar"
        if base == "work":
            return "school", "cedar"
    return base, "pattern"


def daily_limit(alloc: float, used_before: float, weights: list[float], t: int,
                plan: dict) -> tuple[float, str | None, float, float, float | None]:
    """Day ``t``'s limit in points of the week, from what is left of ``alloc``.

    Returns (limit, bound, fixed plan share, floor, cap); ``bound`` is "floor",
    "cap" or None, and ``cap`` is None on the last day.
    """
    total_w = sum(weights) or 1.0
    w_rest = sum(weights[t:]) or 1.0
    remaining = max(0.0, alloc - used_before)
    share = alloc * weights[t] / total_w
    raw = remaining * weights[t] / w_rest
    if t >= len(weights) - 1:  # last day: all that is left of the plan
        return remaining, None, share, 0.0, None
    floor = min(remaining, share * float(plan["daily_floor"]))
    cap = max(floor, share * float(plan["daily_cap"]))
    if raw < floor:
        return floor, "floor", share, floor, cap
    if raw > cap:
        return cap, "cap", share, floor, cap
    return raw, None, share, floor, cap


def chat_advice(five: float | None, week_state: str, today_status: str, plan: dict) -> dict:
    """How ATLAS's main chat should run. It is never blocked: a tier cap
    degrades the model at the weekly and 5-hour guards, and the daily number
    alone only asks for a cheaper one (``sonnet``)."""
    guard, hard = float(plan["five_h_guard"]), float(plan["five_h_hard"])
    cap, level, reasons = None, "ok", []
    daily = False
    if week_state == "out":
        cap, level = "haiku", "throttle"
        reasons.append("the weekly limit is used up")
    elif week_state == "reserve":
        cap, level = "sonnet", "degrade"
        reasons.append("the week is into the reserve")
    if five is not None and five >= hard:
        cap, level = "haiku", "throttle"
        reasons.append(f"the 5-hour window is at {five:.0f}% (hard guard {hard:.0f}%)")
    elif five is not None and five >= guard:
        if cap is None:
            cap, level = "sonnet", "degrade"
        reasons.append(f"the 5-hour window is at {five:.0f}% (guard {guard:.0f}%)")
    if today_status == "over" and cap is None:
        cap, level, daily = "sonnet", "degrade", True
        reasons.append("today's budget is passed (the chat is not held for it)")
    return {"available": True, "level": level, "tier_cap": cap, "daily_only": daily, "reasons": reasons}


def build(conn, now: int | None = None, shifts: list | None | bool = False) -> dict:
    """The week's plan, today's budget, and whether background work may start.

    ``shifts`` is for tests: a list of ``cedar.Shift`` (or None for no Cedar).
    Left at False, Cedar is asked when the plan says to use it.
    """
    now = now or int(time.time())
    plan = load_plan(conn)
    reset, cur = _week_reset(conn, now)
    if reset is None:
        return {"known": False, "reason": "no 7-day reading yet, so the week's reset time is unknown",
                "plan": plan, "now": now}

    cedar_error = ""
    if shifts is False:
        shifts, cedar_error = cedar.shifts() if plan.get("use_cedar") else (None, "")
    by_day = {s.day for s in shifts} if shifts is not None else None
    last_shift = max(by_day) if by_day else None
    on_shift = next((s for s in shifts or [] if s.start <= now < s.end), None)

    tz = classify.tz()
    start = reset - WEEK
    start_dt = datetime.fromtimestamp(start, tz)
    bounds = [int((start_dt + timedelta(days=i)).timestamp()) for i in range(8)]
    bounds[0], bounds[7] = start, reset
    t = max(i for i in range(7) if bounds[i] <= now)

    cost, bg_cost = _costs(conn, bounds, now)
    week_cost = sum(cost)
    measured = cur.get("seven_d_pct") is not None and cur.get("seven_d_reset") == reset
    if measured:
        week_used = float(cur["seven_d_pct"])
        ratio = week_used / week_cost if week_cost >= 5 else _ratio(conn, reset)
    else:
        ratio = _ratio(conn, reset)
        week_used = week_cost * ratio
    used = [c * ratio for c in cost]
    if measured and week_cost < 5:
        # Too little shipped to split by: the whole measured amount is today's.
        used = [0.0] * 7
        used[t] = week_used
    bg_used = [c * ratio for c in bg_cost]

    weights_by_type = plan["weights"]
    days = []
    for i in range(7):
        d = datetime.fromtimestamp(bounds[i], tz).date()
        typ, src = _day_type(d, plan, by_day, last_shift)
        days.append({"date": d.isoformat(), "dow": d.strftime("%a"), "type": typ, "type_source": src,
                     "label": TYPE_LABEL[typ], "weight": float(weights_by_type[typ]),
                     "starts_at": bounds[i], "ends_at": bounds[i + 1]})
    W = sum(x["weight"] for x in days) or 1.0
    reserve = float(plan["reserve_pct"])
    alloc = 100.0 - reserve

    used_before = sum(used[:t])
    days_left = 7 - t
    today_budget, bound, today_plan, floor, cap = daily_limit(
        alloc, used_before, [x["weight"] for x in days], t, plan)
    today_used = used[t]
    left_after_today = max(0.0, alloc - used_before - max(today_used, today_budget))
    w_future = sum(x["weight"] for x in days[t + 1:]) or 1.0

    cum = 0.0
    for i, x in enumerate(days):
        x["plan_pct"] = round(alloc * x["weight"] / W, 1)
        cum += alloc * x["weight"] / W
        x["plan_through_pct"] = round(cum, 1)
        x["used_pct"] = round(used[i], 1) if i <= t else None
        x["background_pct"] = round(bg_used[i], 1) if i <= t else None
        x["state"] = "past" if i < t else "today" if i == t else "future"
        if i == t:
            x["budget_pct"] = round(today_budget, 1)
        elif i > t:
            x["budget_pct"] = round(left_after_today * x["weight"] / w_future, 1)
        else:
            x["budget_pct"] = None

    plan_through_today = days[t]["plan_through_pct"]
    ahead_by = week_used - plan_through_today
    near_at = float(plan["near_at"])
    if today_used > today_budget + 0.05:
        today_status = "over"
    elif today_budget > 0 and today_used >= near_at * today_budget:
        today_status = "near"
    else:
        today_status = "ok"
    if week_used >= 100:
        week_state = "out"
    elif week_used > alloc:
        week_state = "reserve"
    elif ahead_by > 0.5:
        week_state = "ahead"
    else:
        week_state = "on_plan"

    bg_allow = float(plan["background_share"]) * today_budget
    five = cur.get("five_h_pct")
    guard = float(plan["five_h_guard"])
    hold, warn = [], []
    if week_state in ("out", "reserve"):
        hold.append(f"the week is at {week_used:.0f}%, into the {reserve:.0f}% reserve")
    if today_status == "over" or (today_budget - today_used) <= 0.05:
        hold.append(f"today's budget is spent ({today_used:.1f}% of {today_budget:.1f}%)")
    if bg_used[t] >= bg_allow and bg_allow > 0:
        hold.append(f"background has used its share today ({bg_used[t]:.1f}% of {bg_allow:.1f}%)")
    if five is not None and five >= guard:
        hold.append(f"the 5-hour window is at {five:.0f}% (guard {guard:.0f}%)")
    if on_shift is not None:
        end = datetime.fromtimestamp(on_shift.end, tz).strftime("%-I:%M %p")
        hold.append(f"Sam is on a Lumasoft shift until {end}")
    if today_status == "near":
        warn.append(f"today's budget is {today_used / today_budget:.0%} used" if today_budget else "today is nearly spent")
    if week_state == "ahead":
        warn.append(f"the week is {ahead_by:.0f} points ahead of plan")
    level = "hold" if hold else "warn" if warn else "ok"

    warnings = []
    if today_status == "over":
        warnings.append(f"Over today's budget: {today_used:.1f}% used of {today_budget:.1f}%.")
    elif today_status == "near":
        warnings.append(f"Near today's budget: {today_used:.1f}% used of {today_budget:.1f}%.")
    if week_state == "ahead":
        warnings.append(f"The week is {ahead_by:.1f} points ahead of plan ({week_used:.0f}% used, plan {plan_through_today:.0f}% by tonight).")
    elif week_state == "reserve":
        warnings.append(f"Into the reserve: the week is at {week_used:.0f}%, the plan stops at {alloc:.0f}%.")
    elif week_state == "out":
        warnings.append("The weekly limit is used up.")

    today = days[t]
    left = today_budget - today_used
    line = (f"Today ({today['label'].lower()}): {today_used:.1f}% of {today_budget:.1f}% used, "
            + (f"{left:.1f}% left" if left >= 0 else f"{-left:.1f}% over")
            + f". Week {week_used:.0f}%, "
            + (f"{ahead_by:.0f} points ahead of plan" if ahead_by > 0.5 else
               f"{-ahead_by:.0f} points under plan" if ahead_by < -0.5 else "on plan")
            + ".")

    return {
        "known": True,
        "estimated": not measured,
        "now": now,
        "ratio_pct_per_usd": round(ratio, 4),
        "week": {
            "start": start, "reset": reset, "used_pct": round(week_used, 1),
            "allocatable_pct": alloc, "reserve_pct": reserve,
            "plan_through_today_pct": plan_through_today, "ahead_by": round(ahead_by, 1),
            "state": week_state,
        },
        "today": {
            "index": t, "date": today["date"], "dow": today["dow"], "type": today["type"],
            "label": today["label"], "budget_pct": round(today_budget, 1),
            "used_pct": round(today_used, 1), "left_pct": round(left, 1),
            "background_pct": round(bg_used[t], 1), "background_allow_pct": round(bg_allow, 1),
            "status": today_status, "ends_at": today["ends_at"],
            "plan_pct": round(today_plan, 1), "floor_pct": round(floor, 1),
            "cap_pct": round(cap, 1) if cap is not None else None, "bound": bound,
            "days_left": days_left, "remaining_pct": round(max(0.0, alloc - used_before), 1),
        },
        "days": days,
        "background": {"level": level, "hold": hold, "warn": warn},
        "chat": chat_advice(five, week_state, today_status, plan),
        "five_h_pct": five,
        "on_shift": ({"until": on_shift.end} if on_shift else None),
        "cedar": {"configured": cedar.configured(), "used": by_day is not None, "error": cedar_error},
        "warnings": warnings,
        "line": line,
        "plan": plan,
    }
