"""``claude-quota doctor``: verify all three integrations independently.

Each of the three integrations can silently fail on its own, so each gets its
own check and its own concrete fix line. Returns a list of check dicts and an
overall ok flag; the CLI renders them and sets the exit code.
"""

from __future__ import annotations

import json
import time

from .. import config, ship
from . import client, process


def _ship_check() -> dict:
    """Local check: has `claude-quota ship` run and posted recently?"""
    try:
        st = json.loads(ship.state_path().read_text())
    except (OSError, ValueError):
        return {
            "name": "shipper",
            "ok": False,
            "detail": "has never run on this machine",
            "fix": "Run `claude-quota install --ship` (or `claude-quota ship` once by hand).",
        }
    last_ok = st.get("last_ok")
    if st.get("last_error") and (not last_ok or st.get("last_error_at", 0) > last_ok):
        return {
            "name": "shipper",
            "ok": False,
            "detail": f"last post failed: {st['last_error']}",
            "fix": "Check --server and the token (`claude-quota install`), then `claude-quota ship`.",
        }
    age = time.time() - float(last_ok or 0)
    res = st.get("last_result") or {}
    bad = res.get("bad_lines") or 0
    ok = age < 15 * 60
    return {
        "name": "shipper",
        "ok": ok,
        "detail": (
            f"last shipped {int(age // 60)}m ago ({res.get('requests', 0)} requests"
            + (f", {bad} unreadable lines" if bad else "")
            + ")"
        ),
        "fix": None if ok else "Is the launchd agent loaded? `claude-quota install --ship`.",
    }


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
    env_ok = env.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1" and bool(
        env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    # Telemetry is optional once the shipper runs: transcripts carry attribution.
    env_ok = env_ok or _ship_check()["ok"]
    ok = sl_ok and env_ok
    missing = []
    if not sl_ok:
        missing.append("statusLine not pointed at the wrapper")
    if not env_ok:
        missing.append("telemetry env missing and no shipper")
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
    shipper = _ship_check()
    checks.append(shipper)

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
    # Attribution arrives through either OTel or the shipper; one is enough.
    ot = src["otel_metrics"]
    ot_ok = ot["live"] or shipper["ok"]
    checks.append(
        {
            "name": "otel stream",
            "ok": ot_ok,
            "detail": (
                f"metrics arriving ({ot['data_points']} data points)"
                if ot["live"]
                else "no OTel metrics (attribution comes from the shipper)"
                if shipper["ok"]
                else "no OTel metrics received recently"
            ),
            "fix": None
            if ot_ok
            else (
                "Confirm the OTel env keys are set (`claude-quota install`) and restart "
                "your Claude Code sessions so they re-read settings.json."
            ),
        }
    )

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}
