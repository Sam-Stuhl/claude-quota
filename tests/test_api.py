import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_QUOTA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_QUOTA_DISABLE_OTLP", "1")
    from claude_quota.daemon.app import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_summary_shape(client):
    r = client.get("/api/summary")
    assert r.status_code == 200
    body = r.json()
    assert "five_hour" in body
    assert "seven_day" in body
    assert "projection" in body
    assert "sessions" in body


def test_health_reports_sources(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    src = r.json()["sources"]
    assert set(src) == {"statusline", "otel_metrics", "otel_logs"}


def test_ingest_returns_204_immediately(client):
    r = client.post(
        "/ingest/statusline",
        json={"session_id": "s1", "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 1}}},
    )
    assert r.status_code == 204


def test_ingest_survives_garbage(client):
    r = client.post("/ingest/statusline", content=b"not json{{{")
    assert r.status_code == 204


def test_other_endpoints(client):
    assert client.get("/api/sessions").status_code == 200
    assert client.get("/api/breakdown", params={"by": "model", "window": "5h"}).status_code == 200
    assert client.get("/api/history", params={"days": 7}).status_code == 200
