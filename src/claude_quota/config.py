"""Runtime configuration: filesystem paths and network ports.

Everything the daemon and CLI need to agree on lives here. Paths default to
``~/.claude-quota`` and the Claude Code config dir defaults to ``~/.claude``;
both are overridable by environment variable so tests (and unusual installs)
can point them elsewhere.
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

# Network ports. The daemon serves its HTTP API on DAEMON_PORT and listens for
# OpenTelemetry on the two OTLP ports Claude Code's env block configures.
DAEMON_HOST = os.environ.get("CLAUDE_QUOTA_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("CLAUDE_QUOTA_PORT", "7788"))
OTLP_GRPC_PORT = int(os.environ.get("CLAUDE_QUOTA_OTLP_GRPC_PORT", "4317"))
OTLP_HTTP_PORT = int(os.environ.get("CLAUDE_QUOTA_OTLP_HTTP_PORT", "4318"))

# The API is only meant for the daemon's own clients (CLI, web) over
# LAN/Tailscale. When the CLI talks to the daemon it uses this base URL.
DAEMON_BASE_URL = os.environ.get(
    "CLAUDE_QUOTA_URL", f"http://{DAEMON_HOST}:{DAEMON_PORT}"
)


def runtime_dir() -> Path:
    """Directory holding the SQLite database, pidfile, and daemon log."""
    override = os.environ.get("CLAUDE_QUOTA_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".claude-quota"
    return base


def claude_config_dir() -> Path:
    """Claude Code's own config dir, where settings.json and the wrapper live."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def db_path() -> Path:
    return runtime_dir() / "quota.db"


def pid_path() -> Path:
    return runtime_dir() / "daemon.pid"


def log_path() -> Path:
    return runtime_dir() / "daemon.log"


def statusline_wrapper_path() -> Path:
    return claude_config_dir() / "claude-quota-statusline.sh"


def settings_path() -> Path:
    return claude_config_dir() / "settings.json"


def ensure_runtime_dir() -> Path:
    d = runtime_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def otlp_enabled() -> bool:
    """Whether the daemon should boot the OTLP receivers.

    Set CLAUDE_QUOTA_DISABLE_OTLP=1 to run quota-only (also used by tests to
    avoid binding the telemetry ports).
    """
    return os.environ.get("CLAUDE_QUOTA_DISABLE_OTLP", "").lower() not in (
        "1",
        "true",
        "yes",
    )


# Retention: high-volume tables roll off at this age on daemon start. Override
# to collect more (or less) history. window_history is always kept forever.
RETENTION_DAYS = int(os.environ.get("CLAUDE_QUOTA_RETENTION_DAYS", "90"))
# The raw statusline archive can grow fast; it rolls off separately.
RAW_RETENTION_DAYS = int(
    os.environ.get("CLAUDE_QUOTA_RAW_RETENTION_DAYS", str(RETENTION_DAYS))
)

# A session is "idle" after this long with no activity, and drops off the live
# list after the longer window (its usage still counts toward the window total).
SESSION_IDLE_SECONDS = 5 * 60
SESSION_DROP_SECONDS = 30 * 60

# Degraded mode: if no ingest has carried rate_limits for this long across all
# sessions, quota figures fall back to the local-telemetry estimate.
DEGRADED_AFTER_SECONDS = 10 * 60

# Calibration is considered trustworthy once a model clears both thresholds.
CALIBRATION_MIN_SAMPLES = 50
CALIBRATION_MIN_R2 = 0.7

# Bucketing granularity for accumulated OTel usage.
BUCKET_SECONDS = 10


# --- Client/server config, for the optional remote/multi-device mode ---------
#
# `claude-quota install` writes a small config file so the CLI knows which
# server to talk to and which token to present. The server reads its own auth
# secret from the environment (clean for containers) or the same config file.

def config_file() -> Path:
    return runtime_dir() / "config.json"


def load_config() -> dict:
    p = config_file()
    if p.exists():
        try:
            return json.loads(p.read_text() or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(data: dict) -> None:
    ensure_runtime_dir()
    config_file().write_text(json.dumps(data, indent=2) + "\n")


def client_base_url() -> str:
    """Where the CLI should send its API requests (remote server or local)."""
    return os.environ.get("CLAUDE_QUOTA_URL") or load_config().get("server") or DAEMON_BASE_URL


def client_token() -> str | None:
    return os.environ.get("CLAUDE_QUOTA_TOKEN") or load_config().get("token")


def server_token() -> str | None:
    """The auth secret this daemon requires on ingest, if any (None = open)."""
    return os.environ.get("CLAUDE_QUOTA_TOKEN") or load_config().get("token")


def token_ok(provided: str | None) -> bool:
    """Constant-time check of a presented token/Authorization value.

    Returns True when no server token is configured (open, local/LAN default).
    Accepts either a raw token or an "Authorization: Bearer <token>" value.
    """
    expected = server_token()
    if not expected:
        return True
    if not provided:
        return False
    if provided.startswith("Bearer "):
        provided = provided[len("Bearer ") :]
    return hmac.compare_digest(provided, expected)
