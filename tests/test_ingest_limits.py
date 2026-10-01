"""POST /ingest/limits: an atlas-deck daemon's usageLimits become a quota sample."""

import pytest
from fastapi.testclient import TestClient

from claude_quota.daemon.ingest import parse_limits

BODY = {
    "session_id": "cc-123",
    "device": "Sams-MacBook-Pro.local",
    "model": "claude-opus-5-5",
    "usageLimits": {
        "capturedAt": 1_790_000_000_000,
        "windows": [
            {"id": "five_hour", "status": "allowed", "utilization": 42, "resetsAt": 1_790_010_000_000},
            {"id": "seven_day", "utilization": 17.5, "resetsAt": 1_790_500_000_000},
            {"id": "seven_day_opus", "utilization": 3},
        ],
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_QUOTA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_QUOTA_DISABLE_OTLP", "1")
    from claude_quota.daemon.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_parse_limits_maps_happy_windows():
    s = parse_limits(BODY)
    assert s is not None
    assert s.ts == 1_790_000_000
    assert (s.five_h_pct, s.five_h_reset) == (42.0, 1_790_010_000)
    assert (s.seven_d_pct, s.seven_d_reset) == (17.5, 1_790_500_000)
    assert s.had_limits
    assert (s.device, s.model_id) == ("Sams-MacBook-Pro.local", "claude-opus-5-5")


def test_parse_limits_rejects_bad_bodies():
    assert parse_limits({"usageLimits": {"windows": []}}) is None
    assert parse_limits({"session_id": "x"}) is None
    assert parse_limits({"session_id": "x", "usageLimits": {"windows": "no"}}) is None
    # No known window is still a sample, honestly marked as carrying no limits.
    s = parse_limits({"session_id": "x", "usageLimits": {"windows": [{"id": "other"}]}})
    assert s is not None and not s.had_limits


def test_ingest_limits_stores_a_row(client):
    r = client.post("/ingest/limits", json=BODY)
    assert r.status_code == 204
    conn = client.app.state.qstate.conn
    row = conn.execute(
        "SELECT session_id, device, five_h_pct, seven_d_pct, model_id, had_limits FROM quota_sample"
    ).fetchone()
    assert dict(row) == {
        "session_id": "cc-123",
        "device": "Sams-MacBook-Pro.local",
        "five_h_pct": 42.0,
        "seven_d_pct": 17.5,
        "model_id": "claude-opus-5-5",
        "had_limits": 1,
    }
    sess = conn.execute("SELECT entrypoint FROM session WHERE session_id = 'cc-123'").fetchone()
    assert sess["entrypoint"] == "atlas-deck"


def test_ingest_limits_rejects_garbage(client):
    assert client.post("/ingest/limits", content=b"not json{{{").status_code == 400
    assert client.post("/ingest/limits", json={"session_id": "x"}).status_code == 400


def test_ingest_limits_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setenv("CLAUDE_QUOTA_TOKEN", "s3cret")
    assert client.post("/ingest/limits", json=BODY).status_code == 401
    r = client.post("/ingest/limits", json=BODY, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 204
