"""Entry point for the ``claude-quotad`` console script.

Runs the FastAPI app (which itself boots the OTLP receivers and background
loops) under uvicorn, logging to the runtime directory.
"""

from __future__ import annotations

import atexit
import logging
import os

import uvicorn

from .. import config
from .app import create_app


def main() -> None:
    config.ensure_runtime_dir()
    # Record our own PID so `claude-quota daemon status/stop` works regardless of
    # how we were launched (CLI, launchd, or by hand).
    pidfile = config.pid_path()
    pidfile.write_text(str(os.getpid()))
    atexit.register(lambda: pidfile.unlink(missing_ok=True))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    uvicorn.run(
        app,
        host=config.DAEMON_HOST,
        port=config.DAEMON_PORT,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
