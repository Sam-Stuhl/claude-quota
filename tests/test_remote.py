import json

import pytest
from fastapi.testclient import TestClient

from claude_quota.cli import install


@pytest.fixture
def authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_QUOTA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_QUOTA_DISABLE_OTLP", "1")
    monkeypatch.setenv("CLAUDE_QUOTA_TOKEN", "s3cret")
    from claude_quota.daemon.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_ingest_requires_token_when_configured(authed_client):
    # No token -> rejected.
    r = authed_client.post("/ingest/statusline", json={"session_id": "s1"})
    assert r.status_code == 401
    # Correct token -> accepted.
    r = authed_client.post(
        "/ingest/statusline",
        json={"session_id": "s1"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 204
    # Wrong token -> rejected.
    r = authed_client.post(
        "/ingest/statusline",
        json={"session_id": "s1"},
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 401


def test_human_endpoints_not_gated_by_app_token(authed_client):
    # The API/web are meant to sit behind a reverse proxy, not the app token.
    assert authed_client.get("/api/summary").status_code == 200


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CLAUDE_QUOTA_DIR", str(tmp_path / "cq"))
    return tmp_path


def test_remote_install_writes_http_otel_and_auth(dirs):
    result = install.install(
        server="https://claude-quota.example.com", token="secret", device="box"
    )
    assert result["mode"] == "remote"

    wrapper = (dirs / "claude" / "claude-quota-statusline.sh").read_text()
    assert "https://claude-quota.example.com/ingest/statusline?device=box" in wrapper
    assert "Authorization: Bearer secret" in wrapper

    settings = json.loads((dirs / "claude" / "settings.json").read_text())
    env = settings["env"]
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://claude-quota.example.com"
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer secret"
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "device.name=box"

    cfg = json.loads((dirs / "cq" / "config.json").read_text())
    assert cfg == {"server": "https://claude-quota.example.com", "token": "secret", "device": "box"}


def test_local_install_uses_grpc_and_no_auth(dirs):
    result = install.install(device="mymac")
    assert result["mode"] == "local"
    env = json.loads((dirs / "claude" / "settings.json").read_text())["env"]
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
    wrapper = (dirs / "claude" / "claude-quota-statusline.sh").read_text()
    assert "?device=mymac" in wrapper
    assert "Authorization" not in wrapper
