from claude_quota.daemon import projection

RESET = 2_000_000_000


def _sample(conn, ts, pct):
    conn.execute(
        "INSERT INTO quota_sample (ts, session_id, five_h_pct, five_h_reset, had_limits) "
        "VALUES (?, 's', ?, ?, 1)",
        (ts, pct, RESET),
    )


def test_linear_burn_rate(conn):
    now = 1_000_000
    # 10% over 10 minutes -> 60 %/hour, rising.
    for i in range(11):
        _sample(conn, now - 600 + i * 60, i)
    conn.commit()
    p = projection.project(conn, now=now)
    assert p["available"]
    assert abs(p["burn_rate_pct_per_hour"] - 60.0) < 1.0
    assert p["verdict"] in ("SHORT", "CLEAR")
    assert p["cutoff_at"] > now


def test_too_few_samples_suppressed(conn):
    now = 1_000_000
    _sample(conn, now - 100, 10)
    _sample(conn, now - 50, 12)
    conn.commit()
    p = projection.project(conn, now=now)
    assert not p["available"]
    assert "collecting" in p["suppressed_reason"]


def test_compaction_pauses_projection(conn):
    now = 1_000_000
    for i in range(11):
        _sample(conn, now - 600 + i * 60, i)
    conn.execute(
        "INSERT INTO event_log (ts, session_id, kind, detail) "
        "VALUES (?, 's', 'claude_code.compaction', '{}')",
        (now - 120,),
    )
    conn.commit()
    p = projection.project(conn, now=now)
    assert not p["available"]
    assert "compaction" in p["suppressed_reason"]


def test_flat_or_falling_suppressed(conn):
    now = 1_000_000
    for i in range(11):
        _sample(conn, now - 600 + i * 60, 50)  # flat
    conn.commit()
    p = projection.project(conn, now=now)
    assert not p["available"]
