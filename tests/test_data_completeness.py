import sqlite3

from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
from opentelemetry.proto.common.v1 import common_pb2

from claude_quota import db
from claude_quota.daemon import ingest, otlp


def test_parse_rich_statusline_fields():
    payload = {
        "session_id": "s1",
        "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
        "cost": {"total_cost_usd": 4.82, "total_lines_added": 120, "total_lines_removed": 8},
        "context_window": {"used_percentage": 37},
        "exceeds_200k_tokens": True,
        "rate_limits": {"five_hour": {"used_percentage": 62.0, "resets_at": 10}},
    }
    s = ingest.parse_statusline(payload, now=1000, device="mac-mini")
    assert s.device == "mac-mini"
    assert s.model_id == "claude-opus-5"
    assert s.cost_usd_total == 4.82
    assert s.context_pct == 37
    assert s.lines_added == 120
    assert s.lines_removed == 8
    assert s.exceeds_200k == 1


def test_store_sample_writes_rich_columns_and_raw(conn):
    s = ingest.parse_statusline(
        {"session_id": "s1", "cost": {"total_cost_usd": 1.5}}, now=500, device="laptop"
    )
    ingest.store_sample(conn, s, raw='{"session_id":"s1"}')
    row = conn.execute("SELECT * FROM quota_sample").fetchone()
    assert row["device"] == "laptop"
    assert row["cost_usd_total"] == 1.5
    sess = conn.execute("SELECT device FROM session WHERE session_id='s1'").fetchone()
    assert sess["device"] == "laptop"
    raw = conn.execute("SELECT payload FROM raw_statusline").fetchone()
    assert raw["payload"] == '{"session_id":"s1"}'


def _kv(key, s):
    v = common_pb2.AnyValue(string_value=s)
    return common_pb2.KeyValue(key=key, value=v)


def test_otel_captures_device_plugin_effort(conn):
    req = metrics_service_pb2.ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    rm.resource.attributes.append(_kv("device.name", "mac-mini"))
    m = rm.scope_metrics.add().metrics.add()
    m.name = otlp.COST_METRIC
    dp = m.sum.data_points.add()
    dp.as_double = 2.0
    dp.attributes.extend([
        _kv("session.id", "s9"),
        _kv("model", "claude-opus-5"),
        _kv("plugin.name", "superpowers"),
        _kv("effort", "high"),
    ])
    otlp.handle_metrics_request(conn, req, now=1000)

    sess = conn.execute("SELECT device FROM session WHERE session_id='s9'").fetchone()
    assert sess["device"] == "mac-mini"
    bucket = conn.execute(
        "SELECT plugin_name, effort, cost_usd FROM usage_bucket WHERE session_id='s9'"
    ).fetchone()
    assert bucket["plugin_name"] == "superpowers"
    assert bucket["effort"] == "high"
    assert abs(bucket["cost_usd"] - 2.0) < 1e-9


# The v1 schema, so we can prove the in-place migration to v2.
_V1 = """
CREATE TABLE quota_sample (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
  session_id TEXT NOT NULL, five_h_pct REAL, five_h_reset INTEGER, seven_d_pct REAL,
  seven_d_reset INTEGER, cc_version TEXT, had_limits INTEGER NOT NULL);
CREATE TABLE session (session_id TEXT PRIMARY KEY, first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL, cwd TEXT, project_dir TEXT, git_worktree TEXT,
  entrypoint TEXT, start_type TEXT, color_idx INTEGER NOT NULL);
CREATE TABLE usage_bucket (ts INTEGER NOT NULL, session_id TEXT NOT NULL,
  model TEXT NOT NULL, query_source TEXT, agent_name TEXT, skill_name TEXT,
  mcp_server TEXT, cost_usd REAL NOT NULL DEFAULT 0, tok_input INTEGER NOT NULL DEFAULT 0,
  tok_output INTEGER NOT NULL DEFAULT 0, tok_cache_r INTEGER NOT NULL DEFAULT 0,
  tok_cache_w INTEGER NOT NULL DEFAULT 0, active_ms INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ts, session_id, model, query_source, agent_name, skill_name, mcp_server)
) WITHOUT ROWID;
"""


def test_migration_v1_to_v2(tmp_path):
    p = tmp_path / "old.db"
    raw = sqlite3.connect(str(p))
    raw.executescript(_V1)
    raw.execute(
        "INSERT INTO usage_bucket (ts, session_id, model, query_source, agent_name, "
        "skill_name, mcp_server, cost_usd) VALUES (100, 's', 'm', '', '', '', '', 3.0)"
    )
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    # Opening through db.connect runs schema.sql + migrate().
    conn = db.connect(p)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(quota_sample)")}
    assert {"device", "cost_usd_total", "context_pct"} <= cols
    ucols = {r["name"] for r in conn.execute("PRAGMA table_info(usage_bucket)")}
    assert {"plugin_name", "effort"} <= ucols
    # Existing data preserved through the rebuild.
    row = conn.execute("SELECT cost_usd, plugin_name FROM usage_bucket WHERE session_id='s'").fetchone()
    assert row["cost_usd"] == 3.0
    assert row["plugin_name"] == ""
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
