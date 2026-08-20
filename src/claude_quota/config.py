"""Runtime configuration: filesystem paths and network ports.

Everything the daemon and CLI need to agree on lives here. Paths default to
``~/.claude-quota`` and the Claude Code config dir defaults to ``~/.claude``;
both are overridable by environment variable so tests (and unusual installs)
can point them elsewhere.
"""

from __future__ import annotations

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


# Retention: quota_sample and usage_bucket roll off at this age on daemon start.
RETENTION_DAYS = 90

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
