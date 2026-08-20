"""The FastAPI application: routes, background tasks, and OTLP receivers.

Everything lives in one process and one event loop. The lifespan opens the
database, starts the ingest processor and the periodic calibration/maintenance
loops, and boots both OTLP receivers (gRPC and HTTP). If an OTLP port cannot be
bound the daemon still serves its API: the missing stream is simply reported as
down by ``doctor``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import config, db
from . import (
    breakdown,
    calibration,
    events,
    health,
    history,
    ingest,
    otlp,
    sessions,
    summary,
)
from .state import AppState

log = logging.getLogger("claude_quota.daemon")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CALIBRATION_INTERVAL = 15 * 60
MAINTENANCE_INTERVAL = 60
HEARTBEAT_INTERVAL = 15


async def _calibration_loop(state: AppState) -> None:
    # A short initial delay so a fresh daemon has a little data first.
    await asyncio.sleep(30)
    while True:
        try:
            calibration.refit(state.conn)
        except Exception:
            log.exception("calibration refit failed")
        await asyncio.sleep(CALIBRATION_INTERVAL)


async def _maintenance_loop(state: AppState) -> None:
    while True:
        try:
            history.close_windows(state.conn)
        except Exception:
            log.exception("window close-out failed")
        await asyncio.sleep(MAINTENANCE_INTERVAL)


async def _heartbeat_loop(state: AppState) -> None:
    """Nudge SSE subscribers periodically so time-based fields stay fresh."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        await state.notify_change()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_runtime_dir()
    conn = db.connect()
    db.apply_retention(conn)
    state = AppState(conn=conn)
    app.state.qstate = state

    tasks = [
        asyncio.create_task(ingest.processor(state)),
        asyncio.create_task(_calibration_loop(state)),
        asyncio.create_task(_maintenance_loop(state)),
        asyncio.create_task(_heartbeat_loop(state)),
    ]

    grpc_server = None
    http_server = None
    if config.otlp_enabled():
        try:
            grpc_server = await otlp.serve_grpc(state)
            log.info("OTLP gRPC listening on :%d", config.OTLP_GRPC_PORT)
        except Exception as e:
            log.warning("OTLP gRPC receiver unavailable: %s", e)

        try:
            http_cfg = uvicorn.Config(
                otlp.build_http_app(state),
                host=config.DAEMON_HOST,
                port=config.OTLP_HTTP_PORT,
                log_level="warning",
            )
            http_server = uvicorn.Server(http_cfg)
            tasks.append(asyncio.create_task(http_server.serve()))
            log.info("OTLP HTTP listening on :%d", config.OTLP_HTTP_PORT)
        except Exception as e:
            log.warning("OTLP HTTP receiver unavailable: %s", e)

    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        if grpc_server is not None:
            await grpc_server.stop(0.5)
        if http_server is not None:
            http_server.should_exit = True
        conn.close()


def create_app() -> FastAPI:
    app = FastAPI(title="claude-quota", lifespan=lifespan)

    def state(request: Request) -> AppState:
        return request.app.state.qstate

    @app.post("/ingest/statusline")
    async def ingest_statusline(request: Request):
        # Return before doing any work: this is on the critical path of a
        # status line render. Parse cheaply, queue, respond 204.
        raw = await request.body()
        try:
            payload = json.loads(raw)
            request.app.state.qstate.ingest_queue.put_nowait(payload)
        except Exception:
            pass
        return Response(status_code=204)

    @app.get("/api/summary")
    async def api_summary(request: Request):
        return JSONResponse(summary.build(state(request).conn))

    @app.get("/api/sessions")
    async def api_sessions(request: Request):
        return JSONResponse(sessions.build(state(request).conn))

    @app.get("/api/breakdown")
    async def api_breakdown(
        request: Request,
        by: str = Query("model"),
        window: str = Query("5h"),
    ):
        return JSONResponse(breakdown.build(state(request).conn, by=by, window=window))

    @app.get("/api/history")
    async def api_history(request: Request, days: int = Query(14)):
        return JSONResponse(history.build(state(request).conn, days=days))

    @app.get("/api/health")
    async def api_health(request: Request):
        s = state(request)
        return JSONResponse(health.build(s.conn, s))

    @app.get("/events")
    async def sse(request: Request):
        return StreamingResponse(
            events.event_stream(state(request)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    async def index():
        idx = WEB_DIR / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return JSONResponse({"service": "claude-quota", "web": "not installed"})

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app
