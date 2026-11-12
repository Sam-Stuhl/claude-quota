from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from claude_quota.daemon import budget, cedar

TZ = ZoneInfo("America/New_York")
# A Wednesday 01:00 reset, like Sam's account.
RESET = int(datetime(2026, 11, 18, 1, 0, tzinfo=TZ).timestamp())
START = RESET - budget.WEEK


def at(day: int, hour: int = 12) -> int:
    """Seconds for day `day` of the week (0 = Wednesday) at `hour` local."""
    return int((datetime.fromtimestamp(START, TZ) + timedelta(days=day, hours=hour - 1)).timestamp())


def reading(conn, ts, seven, five=10.0):
    conn.execute(
        "INSERT INTO quota_sample (ts, session_id, five_h_pct, five_h_reset, seven_d_pct, "
        "seven_d_reset, had_limits, source) VALUES (?, 's', ?, ?, ?, ?, 1, 'statusline')",
        (ts, five, ts + 3600, seven, RESET),
    )
    conn.commit()


_seq = iter(range(10**6))


def spend(conn, ts, cost, sid="sam-1", started_by=None):
    conn.execute(
        "INSERT INTO req_usage (request_id, ts, session_id, model, cost_usd) VALUES (?, ?, ?, 'claude-opus-5-5', ?)",
        (f"r{next(_seq)}", ts, sid, cost),
    )
    if started_by:
        conn.execute(
            "INSERT INTO atlas_session (claude_session_id, kind, started_by) VALUES (?, 'session', ?) "
            "ON CONFLICT (claude_session_id) DO NOTHING",
            (sid, started_by),
        )
    conn.commit()


def test_unknown_without_a_weekly_reading(conn):
    b = budget.build(conn, now=at(0), shifts=None)
    assert b["known"] is False


def test_plan_splits_the_allocatable_part_by_weight(conn):
    reading(conn, at(0, 2), 0.0)
    b = budget.build(conn, now=at(0, 3), shifts=None)
    assert [d["dow"] for d in b["days"]] == ["Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue"]
    assert [d["type"] for d in b["days"]] == ["school", "work", "school", "light", "light", "work", "school"]
    assert sum(d["plan_pct"] for d in b["days"]) == pytest.approx(90.0, abs=0.2)
    work, school, light = b["days"][1]["plan_pct"], b["days"][0]["plan_pct"], b["days"][3]["plan_pct"]
    assert work > school > light
    assert work / school == pytest.approx(1.55, abs=0.02)


def test_days_split_the_measured_percent_by_cost(conn):
    spend(conn, at(0), 100.0)
    spend(conn, at(1), 300.0)
    reading(conn, at(1, 18), 40.0)
    b = budget.build(conn, now=at(1, 18), shifts=None)
    assert b["estimated"] is False
    assert b["days"][0]["used_pct"] == pytest.approx(10.0)
    assert b["days"][1]["used_pct"] == pytest.approx(30.0)
    assert b["week"]["used_pct"] == 40.0
    assert b["ratio_pct_per_usd"] == pytest.approx(0.1)


def test_overspending_shrinks_the_rest_of_the_week(conn):
    reading(conn, at(0, 2), 0.0)
    fresh = budget.build(conn, now=at(1, 9), shifts=None)["today"]["budget_pct"]
    spend(conn, at(0), 300.0)
    reading(conn, at(0, 22), 40.0)
    b = budget.build(conn, now=at(1, 9), shifts=None)
    assert b["today"]["budget_pct"] < fresh
    # What is left of the plan is split over today and the days after it.
    rest = sum(d["budget_pct"] for d in b["days"][1:])
    assert rest == pytest.approx(90.0 - 40.0, abs=0.3)
    assert b["week"]["state"] == "ahead"
    assert any("ahead of plan" in w for w in b["warnings"])


def test_today_over_holds_background(conn):
    spend(conn, at(0, 10), 200.0)
    reading(conn, at(0, 20), 20.0)
    b = budget.build(conn, now=at(0, 20), shifts=None)
    assert b["today"]["status"] == "over"
    assert b["background"]["level"] == "hold"
    assert any("today's budget is spent" in h for h in b["background"]["hold"])


def test_background_share_is_tracked_and_capped(conn):
    spend(conn, at(0, 9), 10.0)
    spend(conn, at(0, 10), 40.0, sid="routine-1", started_by="routine")
    reading(conn, at(0, 11), 5.0)
    b = budget.build(conn, now=at(0, 11), shifts=None)
    assert b["today"]["background_pct"] == pytest.approx(4.0)
    assert b["today"]["background_allow_pct"] == pytest.approx(0.25 * b["today"]["budget_pct"], abs=0.1)
    assert b["background"]["level"] == "hold"
    assert any("background has used its share" in h for h in b["background"]["hold"])


def test_five_hour_guard_holds_background(conn):
    reading(conn, at(0, 9), 1.0, five=75.0)
    b = budget.build(conn, now=at(0, 9), shifts=None)
    assert any("5-hour window" in h for h in b["background"]["hold"])


def test_cedar_shifts_decide_work_days_and_hold_during_one(conn):
    reading(conn, at(0, 2), 0.0)
    fri = datetime.fromtimestamp(at(2), TZ).date()
    shift = cedar.Shift(fri, at(2, 14), at(2, 19))
    later = cedar.Shift(fri + timedelta(days=7), at(2, 14) + budget.WEEK, at(2, 19) + budget.WEEK)
    b = budget.build(conn, now=at(2, 15), shifts=[shift, later])
    types = [d["type"] for d in b["days"]]
    # Friday has a shift; Thursday and Monday have none this week.
    assert types[2] == "work" and types[1] == "school" and types[5] == "school"
    assert b["days"][2]["type_source"] == "cedar"
    assert any("Lumasoft shift" in h for h in b["background"]["hold"])


def test_plan_changes_validate_and_persist(conn):
    reading(conn, at(0, 2), 0.0)
    sat = datetime.fromtimestamp(at(3), TZ).date().isoformat()
    budget.save_plan(conn, {"weights": {"light": 0.2}, "reserve_pct": 20, "overrides": {sat: "work"},
                            "background_share": 30})
    b = budget.build(conn, now=at(0, 3), shifts=None)
    assert b["plan"]["weights"]["light"] == 0.2
    assert b["plan"]["background_share"] == 0.3
    assert b["days"][3]["type"] == "work" and b["days"][3]["type_source"] == "override"
    assert sum(d["plan_pct"] for d in b["days"]) == pytest.approx(80.0, abs=0.2)
    with pytest.raises(budget.PlanError):
        budget.save_plan(conn, {"weights": {"holiday": 1}})
    with pytest.raises(budget.PlanError):
        budget.save_plan(conn, {"reserve_pct": 90})
    budget.save_plan(conn, {"overrides": {sat: "auto"}})
    assert sat not in budget.load_plan(conn)["overrides"]


def test_estimated_from_last_week_when_no_live_reading(conn):
    # Last week: 50% for $500, so 0.1 %/$. Nothing measured yet this week.
    conn.execute(
        "INSERT INTO quota_sample (ts, session_id, seven_d_pct, seven_d_reset, had_limits) VALUES (?, 's', 50, ?, 1)",
        (START - 3600, START),
    )
    spend(conn, START - 7200, 500.0)
    spend(conn, at(0, 9), 50.0)
    b = budget.build(conn, now=at(0, 10), shifts=None)
    assert b["estimated"] is True
    assert b["week"]["used_pct"] == pytest.approx(5.0)


def test_budget_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CLAUDE_QUOTA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_QUOTA_DISABLE_OTLP", "1")
    monkeypatch.setenv("CLAUDE_QUOTA_TOKEN", "t0k")
    monkeypatch.delenv("CEDAR_API_KEY", raising=False)
    from claude_quota.daemon.app import create_app

    with TestClient(create_app()) as c:
        assert c.get("/ingest/budget").status_code == 401
        r = c.get("/ingest/budget", headers={"Authorization": "Bearer t0k"})
        assert r.status_code == 200 and r.json()["known"] is False
        assert c.get("/api/budget").status_code == 200
        bad = c.put("/api/budget/plan", json={"reserve_pct": 99})
        assert bad.status_code == 400 and "reserve" in bad.json()["error"]
        ok = c.put("/ingest/budget/plan", json={"reserve_pct": 15}, headers={"Authorization": "Bearer t0k"})
        assert ok.status_code == 200 and ok.json()["plan"]["reserve_pct"] == 15
        assert c.get("/api/budget").json()["plan"]["reserve_pct"] == 15


def test_day_type_falls_back_to_pattern_past_cedars_horizon():
    plan = budget.merge_plan(budget.DEFAULT_PLAN, {})
    mon = date(2027, 1, 4)
    assert budget._day_type(mon, plan, {date(2026, 12, 10)}, date(2026, 12, 10)) == ("work", "pattern")
