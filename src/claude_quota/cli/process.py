"""Daemon process management: start / stop / status / logs via a pidfile.

Deliberately simple and dependency-free so it works cleanly over SSH. The
daemon is spawned in its own session (setsid) so it survives the launching
shell, with stdout/stderr appended to the runtime log.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections import deque

from .. import config
from . import client


def _read_pid() -> int | None:
    p = config.pid_path()
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_running() -> bool:
    pid = _read_pid()
    return pid is not None and _alive(pid)


def start() -> dict:
    if is_running():
        return {"status": "already-running", "pid": _read_pid()}

    config.ensure_runtime_dir()
    log = open(config.log_path(), "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "claude_quota.daemon.runner"],
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    config.pid_path().write_text(str(proc.pid))

    # Give it a moment and confirm it actually came up.
    for _ in range(30):
        if client.is_up(timeout=0.5):
            return {"status": "started", "pid": proc.pid}
        if proc.poll() is not None:
            return {"status": "failed", "pid": proc.pid, "exit_code": proc.returncode}
        time.sleep(0.2)
    return {"status": "started-unconfirmed", "pid": proc.pid}


def stop() -> dict:
    pid = _read_pid()
    if pid is None or not _alive(pid):
        config.pid_path().unlink(missing_ok=True)
        return {"status": "not-running"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        config.pid_path().unlink(missing_ok=True)
        return {"status": "not-running"}
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    config.pid_path().unlink(missing_ok=True)
    return {"status": "stopped", "pid": pid}


def status() -> dict:
    pid = _read_pid()
    running = pid is not None and _alive(pid)
    return {
        "running": running,
        "pid": pid,
        "responding": client.is_up(timeout=0.5) if running else False,
        "log": str(config.log_path()),
        "db": str(config.db_path()),
    }


def tail(lines: int = 40) -> str:
    p = config.log_path()
    if not p.exists():
        return ""
    with open(p, "rb") as f:
        return "".join(
            x.decode("utf-8", "replace")
            for x in deque(f.readlines(), maxlen=lines)
        )
