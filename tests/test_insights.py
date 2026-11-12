from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from claude_quota.daemon import budget, insights

TZ = ZoneInfo("America/New_York")
RESET = int(datetime(2026, 11, 18, 1, 0, tzinfo=TZ).timestamp())
START = RESET - budget.WEEK


def at(day, hour=12):
    return int((datetime.fromtimestamp(START, TZ) + timedelta(days=day, hours=hour - 1)).timestamp())


def reading(conn, ts, seven, reset=RESET):
    conn.execute(
        "INSERT INTO quota_sample (ts, session_id, five_h_pct, five_h_reset, seven_d_pct, seven_d_reset, had_limits) "
        "VALUES (?, 's', 10, ?, ?, ?, 1)", (ts, ts + 3600, seven, reset))
    conn.commit()


_n = iter(range(10**6))


def spend(conn, ts, cost, sid="s1", cwd="/Users/sam/repos/atlas", started_by=None):
    conn.execute("INSERT INTO req_usage (request_id, ts, session_id, model, cost_usd) VALUES (?, ?, ?, 'claude-opus-5-5', ?)",
                 (f"r{next(_n)}", ts, sid, cost))
    conn.execute("INSERT INTO conv (session_id, cwd, entrypoint) VALUES (?, ?, 'cli') ON CONFLICT (session_id) DO NOTHING",
                 (sid, cwd))
    if started_by:
        conn.execute("INSERT INTO atlas_session (claude_session_id, kind, started_by, number, title, atlas_id) "
                     "VALUES (?, 'session', ?, 7, 'Nightly', 'abc') ON CONFLICT (claude_session_id) DO NOTHING",
                     (sid, started_by))
    conn.commit()


def test_points_follow_each_weeks_own_ratio(conn):
    # Last week: $1000 measured at 80% (0.08 %/$). This week: $100 at 20% (0.2 %/$).
    spend(conn, at(-3), 1000.0)
    reading(conn, at(-1), 80.0, reset=START)
    spend(conn, at(0, 9), 60.0)
    spend(conn, at(1, 15), 40.0, sid="s2")
    reading(conn, at(1, 16), 20.0)
    b = insights.build(conn, now=at(1, 16), days=14)
    days = {d["date"]: d for d in b["days"]}
    last = datetime.fromtimestamp(at(-3), TZ).date().isoformat()
    assert days[last]["total"] == pytest.approx(80.0)
    assert b["days"][-1]["total"] == pytest.approx(8.0)
    assert b["days"][-2]["total"] == pytest.approx(12.0)
    cur = [w for w in b["weeks"] if w["current"]][0]
    assert cur["total"] == pytest.approx(20.0) and cur["measured"]
    assert b["ranges"]["week"]["total"] == pytest.approx(20.0)


def test_today_hours_burn_and_sessions(conn):
    spend(conn, at(0, 2), 10.0, sid="bg", cwd="/Users/sam/atlas", started_by="routine")
    spend(conn, at(0, 14), 30.0)
    reading(conn, at(0, 14), 10.0)
    b = insights.build(conn, now=at(0, 14) + 60, days=7)
    t = b["today"]
    assert t["total"] == pytest.approx(10.0)
    assert t["hours"][1]["total"] == pytest.approx(2.5)  # day starts 01:00, so 02:xx is hour 1
    assert t["burn_last_hour"] == pytest.approx(7.5)
    assert b["days"][-1]["background"] == pytest.approx(2.5)
    top = b["ranges"]["today"]["sessions"]
    assert top[0]["sid"] == "s1" and top[0]["live"]
    assert top[1]["atlas_number"] == 7 and top[1]["background"]


def test_unknown_without_a_reading(conn):
    assert insights.build(conn, now=at(0), days=7)["known"] is False


def test_insights_route_needs_the_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CLAUDE_QUOTA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_QUOTA_DISABLE_OTLP", "1")
    monkeypatch.setenv("CLAUDE_QUOTA_TOKEN", "t0k")
    from claude_quota.daemon.app import create_app

    with TestClient(create_app()) as c:
        assert c.get("/ingest/insights").status_code == 401
        assert c.get("/ingest/insights", headers={"Authorization": "Bearer t0k"}).json()["known"] is False
