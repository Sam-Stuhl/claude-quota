import json
import time

from claude_quota import ship
from claude_quota.daemon import attribution, limits, overview, projection
from claude_quota.daemon.state import TTLCache

NOW = 1_800_000_000


def _lim(conn, ts, five, reset, source="statusline", seven=10.0, seven_reset=NOW + 86400):
    conn.execute(
        "INSERT INTO quota_sample (ts, session_id, five_h_pct, five_h_reset, seven_d_pct, "
        "seven_d_reset, had_limits, source) VALUES (?, 's', ?, ?, ?, ?, 1, ?)",
        (ts, five, reset, seven, seven_reset, source),
    )
    conn.commit()


def test_expired_window_is_not_served(conn):
    # The last reading belongs to a window that reset an hour ago.
    _lim(conn, NOW - 7200, 80.0, NOW - 3600)
    r = limits.read(conn, NOW)
    assert r["five_expired"] and r["five_h_pct"] is None
    p = projection.project(conn, NOW)
    assert not p["available"] and "reset" in p["suppressed_reason"]
    # The window maths must not anchor on the dead reset time.
    out = attribution.compute(conn, NOW)
    assert out["window_start"] == NOW - 5 * 3600
    assert out["five_hour"]["resets_at"] is None


def test_projection_fits_one_source_only(conn):
    reset = NOW + 3 * 3600
    for i in range(12):  # statusline: flat at 40%
        _lim(conn, NOW - 700 + i * 60, 40.0, reset)
    for i in range(12):  # quota-meter: rising 1 point a minute, newest sample wins
        _lim(conn, NOW - 690 + i * 60, 20.0 + i, reset, source="quota-meter")
    p = projection.project(conn, NOW)
    assert p["current_pct"] == 31.0
    assert abs(p["burn_rate_pct_per_hour"] - 60.0) < 2.0


def _req(conn, rid, sid, ts, cost, model="claude-opus-5-5", prompt=None):
    conn.execute(
        "INSERT INTO req_usage (request_id, ts, device, session_id, prompt_id, model, cost_usd) "
        "VALUES (?, ?, 'sams-mac-mini', ?, ?, ?, ?)",
        (rid, ts, sid, prompt, model, cost),
    )


def test_overview_names_work_and_sums_agree(conn):
    now = int(time.time())
    conv = "INSERT INTO conv (session_id, device, cwd, entrypoint, first_ts, last_ts) VALUES (?,?,?,?,?,?)"
    conn.execute(conv, ("s-atlas", "sams-mac-mini", "/Users/sam/repos/atlas", "sdk-cli", now - 900, now))
    conn.execute(conv, ("s-hw", "sams-mac-mini", "/Users/sam/school/phys121", "sdk-cli", now - 900, now))
    conn.execute(conv, ("s-term", "sams-macbook-pro", "/Users/sam/Desktop/repos/planner", "cli", now - 900, now))
    conn.execute(conv, ("s-mystery", "sams-macbook-pro", "/Users/sam/Desktop/misc", "sdk-py", now - 900, now))
    atl = ("INSERT INTO atlas_session (claude_session_id, atlas_id, number, kind, title, started_by, "
           "folder, repo) VALUES (?,?,?,?,?,?,?,?)")
    conn.execute(atl, ("s-atlas", "s_1", 1, "session", "Fix daemon", "sam", "Projects/atlas/Daemon", "atlas"))
    conn.execute(atl, ("s-hw", "s_2", 2, "session", "Homework 9", "sam", "School/Fall 2026/PHYS 121/HW", ""))
    _req(conn, "r1", "s-atlas", now - 60, 10.0)
    _req(conn, "r2", "s-hw", now - 50, 5.0, model="claude-sonnet-5-5")
    _req(conn, "r3", "s-term", now - 40, 3.0)
    _req(conn, "r4", "s-mystery", now - 30, 2.0)
    conn.commit()

    d = overview.build(conn, window="5h", now=now)
    kinds = {k["key"]: k for k in d["by"]["kind"]}
    assert kinds["atlas_dev"]["cost_usd"] == 10.0
    assert kinds["school"]["cost_usd"] == 5.0
    assert kinds["interactive"]["cost_usd"] == 3.0
    projects = {p["key"]: p for p in d["by"]["project"]}
    assert "School: PHYS 121" in projects and "atlas" in projects
    # every roll-up accounts for the same total
    for name in ("kind", "project", "model", "host", "session"):
        assert abs(sum(i["cost_usd"] for i in d["by"][name]) - 20.0) < 0.01, name
    assert d["by"]["session"][0]["title"] == "#1 Fix daemon"
    # whatever could not be placed is named, not hidden
    assert d["unclassified"]["cost_usd"] == 2.0
    assert d["unclassified"]["top_folders"][0]["folder"].endswith("misc")


def test_week_window_follows_the_measured_reset(conn):
    now = int(time.time())
    _lim(conn, now - 30, 10.0, now + 3600, seven=20.0, seven_reset=now + 2 * 86400)
    start, _end, basis = overview.window_bounds("week", limits.read(conn, now), now)
    assert start == now + 2 * 86400 - 7 * 86400 and "reset" in basis


def test_collect_limits_reads_quota_meter_store(tmp_path, monkeypatch):
    store = tmp_path / "quota-meter_claude-mods-abc.json"
    at = int(time.time() * 1000)
    store.write_text(json.dumps({"samples": {
        "five_hour": [{"atMs": at - 60000, "percentUsed": 4, "resetsAt": "2026-11-05T03:10:00.000Z"},
                      {"atMs": at, "percentUsed": 5, "resetsAt": "2026-11-05T03:10:00.000Z"}],
        "seven_day": [{"atMs": at, "percentUsed": 16, "resetsAt": "2026-11-11T06:00:00.000Z"}],
    }}))
    monkeypatch.setenv("CLAUDE_QUOTA_QM_DIR", str(tmp_path))
    state: dict = {}
    bodies = ship.collect_limits(state)
    assert [b["usageLimits"]["capturedAt"] for b in bodies] == [at - 60000, at]
    w = bodies[-1]["usageLimits"]["windows"]
    assert w[0]["id"] == "five_hour" and w[0]["utilization"] == 5
    assert w[0]["resetsAt"] == 1793848200000.0 and w[1]["utilization"] == 16
    state["qm_last_ms"] = at
    assert ship.collect_limits(state) == []


def test_ttl_cache_computes_once():
    c, calls = TTLCache(), []
    for _ in range(5):
        c.get("k", 60, lambda: calls.append(1) or "v")
    assert len(calls) == 1
