from claude_quota.daemon import calibration

RESET = 10_000_000


def _sample(conn, ts, pct, reset=RESET):
    conn.execute(
        "INSERT INTO quota_sample (ts, session_id, five_h_pct, five_h_reset, had_limits) "
        "VALUES (?, 's', ?, ?, 1)",
        (ts, pct, reset),
    )


def _cost(conn, ts, model, cost):
    conn.execute(
        "INSERT INTO usage_bucket (ts, session_id, model, query_source, agent_name, "
        "skill_name, mcp_server, plugin_name, effort, cost_usd) "
        "VALUES (?, 's', ?, '', '', '', '', '', '', ?)",
        (ts, model, cost),
    )


def test_nnls_recovers_known_rates(conn):
    # dpct = 2*costA + 3*costB across five intervals.
    intervals = [
        (1, 0, 2),
        (0, 1, 3),
        (2, 1, 7),
        (1, 2, 8),
        (3, 0, 6),
    ]
    times = [100, 200, 300, 400, 500, 600]
    pct = 0.0
    _sample(conn, times[0], pct)
    for i, (a, b, dpct) in enumerate(intervals):
        t1 = times[i + 1]
        _cost(conn, t1, "A", a)
        _cost(conn, t1, "B", b)
        pct += dpct
        _sample(conn, t1, pct)
    conn.commit()

    result = calibration.refit(conn, now=700)
    assert result["fitted"]
    assert result["n_samples"] == 5
    assert result["r2"] > 0.99

    rates = calibration.get_rates(conn)
    assert abs(rates["A"] - 2.0) < 0.05
    assert abs(rates["B"] - 3.0) < 0.05


def test_reset_boundary_pair_is_discarded(conn):
    # Two points across a reset (reset id changes, pct drops) must not become an
    # observation.
    _sample(conn, 100, 80.0, reset=RESET)
    _sample(conn, 200, 5.0, reset=RESET + 1)
    conn.commit()
    models, A, b = calibration.build_observations(conn, now=700)
    assert A.shape[0] == 0


def test_seed_prior_is_flat(conn):
    _cost(conn, 100, "opus", 1.0)
    conn.commit()
    calibration.seed_prior(conn, now=700)
    row = conn.execute("SELECT * FROM calibration WHERE model='opus'").fetchone()
    assert row["pct_per_usd"] == 1.0
    assert row["n_samples"] == 0
    assert not calibration.is_calibrated(conn)
