"""``claude-quota install``: write the statusline wrapper and patch settings.json.

Idempotent. Backs up settings.json before touching it. If a status line is
already configured, the wrapper chains to it so the user's line still renders;
otherwise it renders a minimal built-in line. Never blocks the terminal: the
POST to the daemon is fire-and-forget with a hard 400ms timeout, and the script
always exits 0 even when the daemon is down.

Supports a local daemon (default) or a remote/multi-device server via
``--server`` (+ optional ``--token``). Each render is tagged with a device
name so a shared server can separate machines.
"""

from __future__ import annotations

import json
import shlex
import socket
import time
from pathlib import Path

from .. import config

MANAGED_MARKER = "claude-quota statusline wrapper (managed)"


def _ingest_endpoint(server: str | None) -> str:
    if server:
        return server.rstrip("/") + "/ingest/statusline"
    return f"http://{config.DAEMON_HOST}:{config.DAEMON_PORT}/ingest/statusline"


def build_otel_env(server: str | None, token: str | None, device: str) -> dict[str, str]:
    """The telemetry env block. Local uses gRPC; remote uses HTTP/protobuf.

    delta temporality is the default and is what the accumulator wants, so it
    is left unset intentionally.
    """
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
        "OTEL_METRICS_INCLUDE_SESSION_ID": "true",
        "OTEL_RESOURCE_ATTRIBUTES": f"device.name={device}",
    }
    if server:
        env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = server.rstrip("/")
        if token:
            env["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Bearer {token}"
    else:
        env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://localhost:{config.OTLP_GRPC_PORT}"
    return env


def build_wrapper(
    original_cmd: str | None, endpoint: str, token: str | None, device: str
) -> str:
    original = original_cmd or ""
    url = f"{endpoint}?device={device}"
    auth = f"-H {shlex.quote('Authorization: Bearer ' + token)} " if token else ""
    default_render = (
        "python3 -c "
        + shlex.quote(
            "import sys,json\n"
            "try:\n"
            "    d=json.load(sys.stdin)\n"
            "except Exception:\n"
            "    sys.exit(0)\n"
            "m=(d.get('model') or {}).get('display_name') or (d.get('model') or {}).get('id') or ''\n"
            "rl=d.get('rate_limits') or {}\n"
            "parts=[m] if m else []\n"
            "fh=(rl.get('five_hour') or {}).get('used_percentage')\n"
            "sd=(rl.get('seven_day') or {}).get('used_percentage')\n"
            "if fh is not None: parts.append('5h %d%%'%round(fh))\n"
            "if sd is not None: parts.append('7d %d%%'%round(sd))\n"
            "sys.stdout.write(' | '.join(parts))\n"
        )
    )
    return f"""#!/usr/bin/env bash
# {MANAGED_MARKER}; do not edit by hand. Rewritten by `claude-quota install`.
input=$(cat)

# Fire-and-forget post to the daemon. Hard 400ms cap, output discarded, never
# blocks, never prints an error, always leaves the status line responsive.
printf '%s' "$input" | curl -m 0.4 -s -X POST {shlex.quote(url)} \\
  {auth}--data-binary @- >/dev/null 2>&1 &

ORIGINAL_CMD={shlex.quote(original)}
if [ -n "$ORIGINAL_CMD" ]; then
  printf '%s' "$input" | eval "$ORIGINAL_CMD"
else
  printf '%s' "$input" | {default_render}
fi
exit 0
"""


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def _extract_existing_command(settings: dict, wrapper_path: Path) -> str | None:
    sl = settings.get("statusLine")
    if isinstance(sl, dict) and sl.get("type") == "command":
        cmd = sl.get("command")
        if isinstance(cmd, str) and cmd and str(wrapper_path) not in cmd:
            return cmd
    return None


def install(
    server: str | None = None,
    token: str | None = None,
    device: str | None = None,
    dry_run: bool = False,
) -> dict:
    actions: list[str] = []
    device = device or socket.gethostname().split(".")[0]
    claude_dir = config.claude_config_dir()
    wrapper_path = config.statusline_wrapper_path()
    settings_path = config.settings_path()

    settings = _load_settings(settings_path)
    existing_cmd = _extract_existing_command(settings, wrapper_path)

    endpoint = _ingest_endpoint(server)
    wrapper = build_wrapper(existing_cmd, endpoint, token, device)
    otel_env = build_otel_env(server, token, device)

    if not dry_run:
        claude_dir.mkdir(parents=True, exist_ok=True)
        wrapper_path.write_text(wrapper)
        wrapper_path.chmod(0o755)
    actions.append(
        f"wrote statusline wrapper -> {endpoint} (device={device})"
        + (f", chaining to {existing_cmd}" if existing_cmd else "")
    )

    if settings_path.exists() and not dry_run:
        backup = settings_path.with_suffix(
            settings_path.suffix + f".bak-{int(time.time())}"
        )
        backup.write_text(settings_path.read_text())
        actions.append(f"backed up settings.json to {backup.name}")

    settings.setdefault("env", {})
    # Drop any stale keys from a previous mode (e.g. switching local<->remote).
    for k in ("OTEL_EXPORTER_OTLP_HEADERS",):
        if k not in otel_env:
            settings["env"].pop(k, None)
    changed = [k for k, v in otel_env.items() if settings["env"].get(k) != v]
    settings["env"].update(otel_env)
    actions.append(
        f"set OTel env ({'remote http' if server else 'local grpc'}): "
        + (", ".join(changed) if changed else "already configured")
    )

    desired_sl = {"type": "command", "command": str(wrapper_path)}
    if settings.get("statusLine") != desired_sl:
        settings["statusLine"] = desired_sl
        actions.append("pointed statusLine at the wrapper")
    else:
        actions.append("statusLine already points at the wrapper")

    if not dry_run:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        config.save_config(
            {"server": server, "token": token, "device": device}
        )

    note = (
        f"Restart Claude Code sessions to pick up telemetry. Viewing at {server}"
        if server
        else "Restart Claude Code sessions to pick up telemetry, then `claude-quota daemon start`."
    )
    return {
        "dry_run": dry_run,
        "mode": "remote" if server else "local",
        "server": server,
        "device": device,
        "wrapper_path": str(wrapper_path),
        "settings_path": str(settings_path),
        "chained_command": existing_cmd,
        "actions": actions,
        "note": note,
    }
