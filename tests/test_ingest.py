from claude_quota.daemon import ingest


def test_parse_reset_variants():
    assert ingest.parse_reset(1738425600) == 1738425600
    assert ingest.parse_reset("1738425600") == 1738425600
    assert ingest.parse_reset("2025-02-01T16:00:00Z") == int(
        __import__("datetime").datetime.fromisoformat("2025-02-01T16:00:00+00:00").timestamp()
    )
    assert ingest.parse_reset("not a time") is None
    assert ingest.parse_reset(None) is None
    assert ingest.parse_reset(True) is None


def test_parse_with_rate_limits():
    payload = {
        "session_id": "abc",
        "version": "2.1.90",
        "model": {"id": "claude-opus-5"},
        "workspace": {"project_dir": "/x/atlas", "git_worktree": "wt"},
        "rate_limits": {
            "five_hour": {"used_percentage": 62.0, "resets_at": 1738425600},
            "seven_day": {"used_percentage": 31.0, "resets_at": 1738857600},
        },
    }
    s = ingest.parse_statusline(payload, now=1000)
    assert s is not None
    assert s.had_limits is True
    assert s.five_h_pct == 62.0
    assert s.five_h_reset == 1738425600
    assert s.seven_d_pct == 31.0
    assert s.git_worktree == "wt"


def test_parse_without_rate_limits():
    s = ingest.parse_statusline({"session_id": "abc"}, now=1000)
    assert s is not None
    assert s.had_limits is False
    assert s.five_h_pct is None


def test_parse_empty_rate_limits_block():
    s = ingest.parse_statusline({"session_id": "abc", "rate_limits": {}}, now=1000)
    assert s.had_limits is False


def test_parse_requires_session_id():
    assert ingest.parse_statusline({}, now=1000) is None


def test_store_sample_writes_rows(conn):
    payload = {
        "session_id": "s1",
        "workspace": {"project_dir": "/x/atlas"},
        "rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": 42}},
    }
    s = ingest.parse_statusline(payload, now=500)
    ingest.store_sample(conn, s)
    row = conn.execute("SELECT * FROM quota_sample").fetchone()
    assert row["session_id"] == "s1"
    assert row["had_limits"] == 1
    sess = conn.execute("SELECT * FROM session WHERE session_id='s1'").fetchone()
    assert sess["project_dir"] == "/x/atlas"
    assert sess["color_idx"] is not None
