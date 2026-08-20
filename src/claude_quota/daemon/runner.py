"""Entry point for the ``claude-quotad`` console script.

Runs the FastAPI app (which itself boots the OTLP receivers and background
loops) under uvicorn, logging to the runtime directory.
"""

from __future__ import annotations

import logging

import uvicorn

from .. import config
from .app import create_app


def main() -> None:
    config.ensure_runtime_dir()
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
