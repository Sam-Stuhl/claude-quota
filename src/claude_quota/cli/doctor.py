"""``claude-quota doctor``: verify all three integrations independently.

Each of the three integrations can silently fail on its own, so each gets its
own check and its own concrete fix line. Returns a list of check dicts and an
overall ok flag; the CLI renders them and sets the exit code.
"""

from __future__ import annotations

import json

from .. import config
from . import client, install, process


def _settings_check() -> dict:
    """Local check: is settings.json wired up the way install would leave it?"""
    path = config.settings_path()
    wrapper = config.statusline_wrapper_path()
    if not path.exists():
        return {
            "name": "settings.json",
            "ok": False,
            "detail": f"{path} does not exist",
            "fix": "Run `claude-quota install`.",
        }
    try:
        settings = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return {
            "name": "settings.json",
            "ok": False,
            "detail": f"{path} is not valid JSON",
            "fix": "Fix the JSON, then run `claude-quota install`.",
        }
    sl = settings.get("statusLine") or {}
    sl_ok = isinstance(sl, dict) and str(wrapper) in str(sl.get("command", ""))
    env = settings.get("env") or {}
    env_ok = all(env.get(k) == v for k, v in install.OTEL_ENV.items())
    ok = sl_ok and env_ok
    missing = []
    if not sl_ok:
        missing.append("statusLine not pointed at the wrapper")
    if not env_ok:
        missing.append("OTel env keys missing or wrong")
    return {
        "name": "settings.json",
        "ok": ok,
        "detail": "configured" if ok else "; ".join(missing),
        "fix": None if ok else "Run `claude-quota install`.",
    }


def run() -> dict:
    checks: list[dict] = []

    # 1. Daemon reachable.
    up = client.is_up(timeout=1.0)
    running = process.is_running()
    checks.append(
        {
            "name": "daemon",
            "ok": up,
            "detail": (
                f"responding on {config.DAEMON_BASE_URL}"
                if up
                else ("process running but not responding" if running else "not running")
            ),
            "fix": None if up else "Run `claude-quota daemon start`.",
        }
    )

    checks.append(_settings_check())

    if not up:
        # Without the daemon we cannot verify the live streams.
        checks.append(
            {
                "name": "statusline stream",
                "ok": False,
                "detail": "cannot verify: daemon is down",
                "fix": "Start the daemon, then re-run doctor.",
            }
        )
        checks.append(
            {
                "name": "otel stream",
                "ok": False,
                "detail": "cannot verify: daemon is down",
                "fix": "Start the daemon, then re-run doctor.",
            }
        )
        return {"ok": False, "checks": checks}

    health = client.health()
    src = health["sources"]

    # 2. statusline posting, and whether rate_limits are present.
    sl = src["statusline"]
    if sl["live"] and sl["rate_limits_present"]:
        detail, ok = "arriving with rate_limits", True
    elif sl["live"]:
        detail, ok = (
            "arriving, but rate_limits absent (quota will be estimated)",
            False,
        )
    else:
        detail, ok = "no statusline posts received recently", False
    checks.append(
        {
            "name": "statusline stream",
            "ok": ok,
            "detail": detail,
            "fix": None
            if ok
            else (
                "Ensure `claude-quota install` ran and you have an active Claude Code "
                "session. rate_limits needs OAuth (Pro/Max) auth and at least one API "
                "response."
            ),
        }
    )

    # 3. OTel arriving.
    ot = src["otel_metrics"]
    checks.append(
        {
            "name": "otel stream",
            "ok": ot["live"],
            "detail": (
                f"metrics arriving ({ot['data_points']} data points)"
                if ot["live"]
                else "no OTel metrics received recently"
            ),
            "fix": None
            if ot["live"]
            else (
                "Confirm the OTel env keys are set (`claude-quota install`) and restart "
                "your Claude Code sessions so they re-read settings.json."
            ),
        }
    )

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}
