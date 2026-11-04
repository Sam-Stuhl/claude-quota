"""Transcript shipping, classification and the /api/usage drill-down."""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from claude_quota import pricing, ship, transcripts
from claude_quota.daemon import classify, usage_ingest, usage_tree

NOW = int(time.time())


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _user(sid, pid, text, ts, **kw):
    return {"type": "user", "sessionId": sid, "promptId": pid, "timestamp": _iso(ts),
            "cwd": kw.get("cwd", "/Users/sam/repos/x"), "entrypoint": kw.get("entrypoint", "sdk-py"),
            "message": {"role": "user", "content": text}, **{k: v for k, v in kw.items() if k not in ("cwd", "entrypoint")}}


def _asst(sid, rid, ts, model="claude-opus-5-5", out=100, **kw):
    return {"type": "assistant", "sessionId": sid, "requestId": rid, "timestamp": _iso(ts),
            "cwd": kw.pop("cwd", "/Users/sam/repos/x"), "entrypoint": "sdk-py",
            "message": {"model": model, "usage": {
                "input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 300,
                "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}}},
            **kw}


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


# --- parsing ------------------------------------------------------------------

def test_pricing_uses_model_rates():
    # Opus 5.5: $4 in, $20 out, $0.20 read; 5m write 1.25x, 1h write 2x.
    c = pricing.cost_usd("claude-opus-5-5[1m]", 1_000_000, 0, 0, 0, 0)
    assert c == pytest.approx(4.0)
    assert pricing.cost_usd("claude-haiku-5-5", 0, 1_000_000, 0, 0, 0) == pytest.approx(0.5)
    assert pricing.cost_usd("claude-opus-5-5", 0, 0, 1_000_000, 0, 1_000_000) == pytest.approx(0.2 + 8.0)
    assert pricing.cost_usd("<synthetic>", 5, 5, 5, 5, 5) == 0.0


def test_parse_dedups_blocks_and_links_turns(tmp_path):
    p = tmp_path / "s1.jsonl"
    _write(p, [
        {"type": "agent-name", "sessionId": "s1", "agentName": "Fix the thing"},
        _user("s1", "p1", "please fix the thing", NOW - 60),
        {"type": "api-request", "sessionId": "s1", "id": "ref1", "querySource": "sdk"},
        _asst("s1", "r1", NOW - 59, apiBlockIndex=0, requestRef="ref1"),
        _asst("s1", "r1", NOW - 59, apiBlockIndex=1, requestRef="ref1", attributionMcpServer="browser"),
        "not json at all",
    ])
    p.write_text(p.read_text().replace('"not json at all"', "not json at all"))
    out = transcripts.read_incremental(p, transcripts.Carry())
    assert list(out.requests) == ["r1"]
    r = out.requests["r1"]
    assert r["prompt_id"] == "p1" and r["query_source"] == "sdk" and r["mcp_server"] == "browser"
    assert r["tok_cache_w"] == 300 and r["cost_usd"] > 0
    assert out.turns["p1"]["label"] == "please fix the thing"
    assert out.conversations["s1"]["title"] == "Fix the thing"
    assert out.bad_lines == 1


def test_incremental_read_keeps_partial_line(tmp_path):
    p = tmp_path / "s1.jsonl"
    first = json.dumps(_user("s1", "p1", "hi", NOW)) + "\n"
    second = json.dumps(_asst("s1", "r1", NOW))
    p.write_text(first + second[:20])
    carry = transcripts.Carry()
    a = transcripts.read_incremental(p, carry)
    assert not a.requests and carry.offset == len(first)
    p.write_text(first + second + "\n")
    b = transcripts.read_incremental(p, carry)
    assert list(b.requests) == ["r1"] and b.requests["r1"]["prompt_id"] == "p1"


def test_atlas_stamp_gives_door_and_routine():
    door, routine, rest = transcripts.atlas_marker(
        "[The thread since you last spoke...]\n[Tuesday 2026-11-03 14:00 EST via schedule] Hourly watch. Look around")
    assert (door, routine) == ("schedule", "Hourly watch")
    assert rest.startswith("Hourly watch")
    assert transcripts.atlas_marker("[Monday 2026-11-02 09:00 EST via schedule] [Scheduled check-in: morning. x]")[1] \
        == "Scheduled check-in: morning"
    assert transcripts.atlas_marker("[Sunday 2026-11-01 10:00 EST via voice] hey")[:2] == ("voice", None)
    assert transcripts.atlas_marker("no stamp here") == (None, None, None)


def test_subagent_meta_and_cowork_source(tmp_path):
    sub = tmp_path / "local-agent-mode-sessions" / "x" / "s1" / "subagents" / "agent-a1.jsonl"
    _write(sub, [_user("s1", "p9", "look around", NOW, agentId="a1", isSidechain=True),
                 _asst("s1", "r9", NOW, agentId="a1", attributionAgent="Explore")])
    sub.with_name("agent-a1.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "map it"}))
    out = transcripts.read_incremental(sub, transcripts.Carry())
    assert out.requests["r9"]["agent_type"] == "Explore" and out.requests["r9"]["agent_id"] == "a1"
    assert out.subagents["a1"]["description"] == "map it"
    assert out.conversations["s1"]["source"] == "cowork"


# --- classification -----------------------------------------------------------

@pytest.mark.parametrize(
    "conv,atlas,expect",
    [
        ({"cwd": "/Users/sam/repos/x"}, {"kind": "session", "started_by": "sam", "folder": "Projects/x"},
         ("tasks_sam", "Projects/x")),
        ({}, {"kind": "session", "started_by": "coordinator", "repo": "atlas"}, ("tasks_agent", "atlas")),
        ({}, {"kind": "session", "started_by": "network-health"}, ("event", "network-health")),
        ({}, {"kind": "session", "started_by": "routine", "series_key": "nightly"}, ("scheduled", "nightly")),
        ({"cwd": "/Users/sam/x"}, {"kind": "outside", "started_by": "terminal"}, ("terminal", "x")),
        ({"source": "cowork"}, None, ("cowork", "Cowork")),
        ({"first_prompt": "You are the memory gardener for ATLAS"}, None, ("helpers", "Memory gardener")),
        ({"cwd": "/Users/sam/agents/trainer"}, None, ("roles", "trainer")),
        ({"cwd": "/Users/sam/repos/planner/.claude/worktrees/foo", "entrypoint": "cli"}, None, ("terminal", "planner")),
        ({"cwd": "/private/tmp/weird", "entrypoint": "sdk-cli"}, None, ("other", "/tmp/weird")),
    ],
)
def test_session_place(conv, atlas, expect):
    p = classify.session_place(conv, atlas, "sid")
    assert (p.category, p.group) == expect


def test_turn_stamp_overrides_assistant_thread():
    base = classify.session_place({"cwd": "/Users/sam/atlas/assistant", "first_ts": NOW}, None, "s")
    assert base.category == "atlas_chat" and base.estimated
    sched = classify.request_place(base, "schedule", "Hourly watch", NOW, "claude-opus-5-5")
    assert (sched.category, sched.group, sched.estimated) == ("scheduled", "Hourly watch", False)
    assert classify.request_place(base, "voice", None, NOW, None).category == "atlas_voice"
    assert classify.request_place(base, "web", None, NOW, None).category == "atlas_chat"
    # No stamp: Haiku turns in the assistant thread were voice.
    assert classify.request_place(base, None, None, NOW, "claude-haiku-5-5").category == "atlas_voice"


# --- ingest + tree --------------------------------------------------------------

def _body():
    reqs = []
    for i in range(3):
        reqs.append({"request_id": f"a{i}", "ts": NOW - 100 + i, "session_id": "task", "prompt_id": "pt",
                     "model": "claude-opus-5-5", "cost_usd": 1.0, "tok_input": 1, "tok_output": 2,
                     "tok_cache_r": 3, "tok_cache_w": 4, "mcp_server": "browser" if i == 0 else None})
    reqs.append({"request_id": "w1", "ts": NOW - 50, "session_id": "hw", "prompt_id": "ph",
                 "model": "claude-opus-5-5", "cost_usd": 0.5})
    reqs.append({"request_id": "old", "ts": NOW - 8 * 86400, "session_id": "task", "prompt_id": "pt",
                 "model": "claude-opus-5-5", "cost_usd": 2.0})
    return {
        "device": "mini",
        "requests": reqs,
        "conversations": [
            {"session_id": "task", "cwd": "/Users/sam/repos/x", "title": "Fix X", "first_ts": NOW - 100},
            {"session_id": "hw", "cwd": "/Users/sam/atlas/assistant", "first_prompt": "Hourly watch. Look"},
        ],
        "turns": [
            {"prompt_id": "pt", "session_id": "task", "ts": NOW - 100, "label": "fix x please"},
            {"prompt_id": "ph", "session_id": "hw", "ts": NOW - 50, "door": "schedule",
             "routine": "Hourly watch", "label": "Hourly watch. Look"},
        ],
        "atlas_sessions": [{"claude_session_id": "task", "number": 7, "kind": "session",
                            "title": "Fix X", "started_by": "sam", "folder": "Projects/x"}],
    }


def test_ingest_is_idempotent(conn):
    usage_ingest.store(conn, _body())
    usage_ingest.store(conn, _body())
    n = conn.execute("SELECT COUNT(*) AS n FROM req_usage").fetchone()["n"]
    assert n == 5


def test_tree_levels(conn):
    usage_ingest.store(conn, _body())
    root = usage_tree.build(conn, "7d", [], now=NOW)
    cats = {c["key"]: c for c in root["children"]}
    assert cats["tasks_sam"]["cost_usd"] == pytest.approx(3.0)
    assert cats["scheduled"]["cost_usd"] == pytest.approx(0.5)
    assert cats["tasks_sam"]["prev_cost_usd"] == pytest.approx(2.0)
    assert root["residual"]["key"] == "claude_app"
    assert root["node"]["cost_usd"] == pytest.approx(3.5)

    groups = usage_tree.build(conn, "7d", ["tasks_sam"], now=NOW)
    assert [g["key"] for g in groups["children"]] == ["Projects/x"]
    items = usage_tree.build(conn, "7d", ["tasks_sam", "Projects/x"], now=NOW)
    assert items["children"][0]["label"] == "#7 Fix X" and items["children"][0]["started_by"] == "sam"
    item = usage_tree.build(conn, "7d", ["tasks_sam", "Projects/x", "task"], now=NOW)
    assert item["sections"]["turns"][0]["label"] == "fix x please"
    assert {m["key"] for m in item["sections"]["mcp"]} == {"browser"}
    turn = usage_tree.build(conn, "7d", ["tasks_sam", "Projects/x", "task", "pt"], now=NOW)
    assert len(turn["requests"]) == 3
    piv = usage_tree.build(conn, "7d", [], by="mcp", now=NOW)
    assert {c["key"] for c in piv["children"]} == {"browser", "(none)"}


def test_residual_from_status_line(conn, monkeypatch):
    usage_ingest.store(conn, _body())
    for ts, pct in ((NOW - 200, 10.0), (NOW - 10, 30.0)):
        conn.execute(
            "INSERT INTO quota_sample (ts, session_id, five_h_pct, five_h_reset, seven_d_pct, seven_d_reset, had_limits)"
            " VALUES (?, 's', ?, 999, ?, 9999, 1)", (ts, pct, pct / 10))
    monkeypatch.setattr(usage_tree.calibration, "is_calibrated", lambda c: True)
    monkeypatch.setattr(usage_tree.calibration, "get_rates", lambda c: {"claude-opus-5-5": 4.0})
    root = usage_tree.build(conn, "7d", [], now=NOW)
    res = root["residual"]
    # 20 points measured; local 3.5 USD x 4 pts/USD = 14 explained -> 6 left.
    assert res["measured_5h_points"] == pytest.approx(20.0)
    assert res["est_5h_points"] == pytest.approx(6.0)
    assert root["week_per_5h_point"] == pytest.approx(0.1)


def test_api_usage_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_QUOTA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_QUOTA_DISABLE_OTLP", "1")
    from claude_quota.daemon.app import create_app

    with TestClient(create_app()) as c:
        assert c.post("/ingest/usage", json=_body()).json()["requests"] == 5
        assert c.post("/ingest/usage", content=b"nope").status_code == 400
        r = c.get("/api/usage", params=[("window", "7d"), ("p", "tasks_sam"), ("p", "Projects/x")])
        assert r.status_code == 200 and r.json()["children"][0]["session_id"] == "task"


# --- shipper ------------------------------------------------------------------

def test_ship_run_posts_and_resumes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_QUOTA_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CLAUDE_QUOTA_HOME", str(home))
    proj = home / ".claude" / "projects" / "-x"
    _write(proj / "s1.jsonl", [_user("s1", "p1", "hello", NOW), _asst("s1", "r1", NOW)])
    db = home / "atlas" / "atlas.db"
    db.parent.mkdir(parents=True)
    a = sqlite3.connect(db)
    a.execute("CREATE TABLE sessions (id TEXT, number INT, kind TEXT, title TEXT, started_by TEXT, "
              "folder TEXT, claude_session_id TEXT, created_at TEXT, updated_at TEXT)")
    a.execute("INSERT INTO sessions VALUES ('s_1', 3, 'session', 'Hello', 'sam', 'P', 's1', "
              "'2026-11-03T10:00:00+00:00', '2026-11-03T10:00:00+00:00')")
    a.commit()
    a.close()

    posted = []
    r1 = ship.run(server="http://x", device="mini", post=posted.append)
    assert r1["requests"] == 1 and r1["atlas_sessions"] == 1
    assert any(b.get("atlas_sessions") for b in posted)
    posted.clear()
    r2 = ship.run(server="http://x", device="mini", post=posted.append)
    assert r2["requests"] == 0 and r2["atlas_sessions"] == 0

    def boom(body):
        raise RuntimeError("down")

    with open(proj / "s1.jsonl", "a") as f:
        f.write(json.dumps(_asst("s1", "r2", NOW)) + "\n")
    assert "error" in ship.run(server="http://x", post=boom)
    # The failed run kept no progress, so the next one sends r2.
    r4 = ship.run(server="http://x", post=posted.append)
    assert r4["requests"] == 1
