import json

import pytest

from claude_quota.cli import install


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_fresh_install(claude_home):
    result = install.install()
    wrapper = claude_home / "claude-quota-statusline.sh"
    settings = claude_home / "settings.json"
    assert wrapper.exists()
    assert wrapper.stat().st_mode & 0o111  # executable
    body = wrapper.read_text()
    assert install.MANAGED_MARKER in body
    assert "ingest/statusline" in body

    conf = json.loads(settings.read_text())
    assert conf["statusLine"]["command"] == str(wrapper)
    assert conf["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert result["chained_command"] is None


def test_idempotent(claude_home):
    install.install()
    first = (claude_home / "settings.json").read_text()
    install.install()
    second = json.loads((claude_home / "settings.json").read_text())
    # Re-running does not change the statusLine or duplicate env.
    assert second["statusLine"]["command"] == str(
        claude_home / "claude-quota-statusline.sh"
    )
    assert "already" in " ".join(install.install()["actions"]).lower()
    assert first  # sanity


def test_chains_existing_statusline_and_backs_up(claude_home):
    settings = claude_home / "settings.json"
    settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "my-prompt --fancy"}})
    )
    result = install.install()
    assert result["chained_command"] == "my-prompt --fancy"
    wrapper_body = (claude_home / "claude-quota-statusline.sh").read_text()
    assert "my-prompt --fancy" in wrapper_body
    # A backup of the original settings was made.
    backups = list(claude_home.glob("settings.json.bak-*"))
    assert backups
