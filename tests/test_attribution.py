import time

from claude_quota import db
from claude_quota.daemon import attribution, history


def test_shares_and_unattributed_sum_to_measured(conn):
    now = int(time.time())
    reset = now + 3600
    # Two sessions with known cost; calibration 1.0 pct_per_usd (prior).
    db.upsert_session(conn, "atlas", now, project_dir="/x/atlas")
    db.upsert_session(conn, "bank", now, project_dir="/x/banking-dash")
    conn.execute(
        "INSERT INTO calibration (model, pct_per_usd, r2, n_samples, updated_at) "
        "VALUES ('m', 1.0, 0.9, 100, ?)",
        (now,),
    )
    for sid, cost in (("atlas", 20.0), ("bank", 10.0)):
        conn.execute(
            "INSERT INTO usage_bucket (ts, session_id, model, query_source, agent_name, "
            "skill_name, mcp_server, plugin_name, effort, cost_usd) "
            "VALUES (?, ?, 'm', '', '', '', '', '', '', ?)",
            (now - 60, sid, cost),
        )
    # Measured 5h is 40%: 30% explained by local cost, 10% unattributed.
    conn.execute(
        "INSERT INTO quota_sample (ts, session_id, five_h_pct, five_h_reset, had_limits) "
        "VALUES (?, 'atlas', 40.0, ?, 1)",
        (now, reset),
    )
    conn.commit()

    out = attribution.compute(conn, now=now)
    assert out["five_hour"]["used_percentage"] == 40.0
    assert not out["five_hour"]["estimated"]
    est_total = sum(s["est_pct"] for s in out["sessions"])
    assert abs(est_total - 30.0) < 1e-6
    assert abs(out["unattributed_pct"] - 10.0) < 1e-6
    # Shares plus residual reconstruct the measured percentage.
    assert abs(est_total + out["unattributed_pct"] - 40.0) < 1e-6


def test_labels_from_project_dir(conn):
    now = int(time.time())
    db.upsert_session(conn, "s", now, project_dir="/home/x/atlas", git_worktree="wt")
    conn.execute(
        "INSERT INTO usage_bucket (ts, session_id, model, query_source, agent_name, "
        "skill_name, mcp_server, plugin_name, effort, cost_usd) "
        "VALUES (?, 's', 'm', '', '', '', '', '', '', 1.0)",
        (now - 10,),
    )
    conn.commit()
    out = attribution.compute(conn, now=now)
    assert out["sessions"][0]["label"] == "atlas@wt"


def test_window_close_out(conn):
    now = int(time.time())
    reset = now - 100  # already passed
    start = reset - 5 * 3600
    conn.execute(
        "INSERT INTO quota_sample (ts, session_id, five_h_pct, five_h_reset, had_limits) "
        "VALUES (?, 's', 100.0, ?, 1)",
        (reset - 10, reset),
    )
    conn.execute(
        "INSERT INTO usage_bucket (ts, session_id, model, query_source, agent_name, "
        "skill_name, mcp_server, plugin_name, effort, cost_usd) "
        "VALUES (?, 's', 'm', '', '', '', '', '', '', 5.0)",
        (start + 10, ),
    )
    conn.commit()
    n = history.close_windows(conn, now=now)
    assert n == 1
    row = conn.execute("SELECT * FROM window_history").fetchone()
    assert row["reset_at"] == reset
    assert row["hit_cap"] == 1
    assert abs(row["total_cost"] - 5.0) < 1e-9
