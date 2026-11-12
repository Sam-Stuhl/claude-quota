"""The FastAPI application: routes, background tasks, and OTLP receivers.

Everything lives in one process and one event loop. The lifespan opens the
database, starts the ingest processor and the periodic calibration/maintenance
loops, and boots both OTLP receivers (gRPC and HTTP). If an OTLP port cannot be
bound the daemon still serves its API: the missing stream is simply reported as
down by ``doctor``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import config, db
from . import (
    breakdown,
    budget,
    calibration,
    events,
    health,
    history,
    ingest,
    otlp,
    overview,
    sessions,
    summary,
    usage_ingest,
    usage_tree,
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
            await asyncio.to_thread(calibration.refit, state.conn)
        except Exception:
            log.exception("calibration refit failed")
        await asyncio.sleep(CALIBRATION_INTERVAL)


async def _maintenance_loop(state: AppState) -> None:
    while True:
        try:
            await asyncio.to_thread(history.close_windows, state.conn)
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
    state = AppState(conn=conn, read_conn=db.connect_reader())
    app.state.qstate = state

    tasks = [
        asyncio.create_task(ingest.processor(state)),
        asyncio.create_task(_heartbeat_loop(state)),
    ]
    if not config.static_mode():
        tasks.append(asyncio.create_task(_calibration_loop(state)))
        tasks.append(asyncio.create_task(_maintenance_loop(state)))

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
        if state.read_conn is not None:
            state.read_conn.close()


def create_app() -> FastAPI:
    app = FastAPI(title="claude-quota", lifespan=lifespan)

    @app.middleware("http")
    async def revalidate(request: Request, call_next):
        # Cloudflare caches .js and .css for hours when the origin says nothing,
        # which left browsers on a stale index.html and app.js after a deploy.
        # no-cache still allows a cheap conditional request (the ETag), but never
        # a copy served without asking the origin.
        resp = await call_next(request)
        if request.method == "GET" and "cache-control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    def state(request: Request) -> AppState:
        return request.app.state.qstate

    @app.post("/ingest/statusline")
    async def ingest_statusline(request: Request):
        # Return before doing any work: this is on the critical path of a
        # status line render. Queue the raw body (+device) and respond 204;
        # parsing happens in the background processor.
        if not config.token_ok(request.headers.get("authorization")):
            return Response(status_code=401)
        raw = await request.body()
        device = request.query_params.get("device") or request.headers.get("x-device")
        try:
            request.app.state.qstate.ingest_queue.put_nowait((raw, device))
        except Exception:
            pass
        return Response(status_code=204)

    @app.post("/ingest/limits")
    async def ingest_limits(request: Request):
        # An atlas-deck daemon posts a session's usageLimits after each turn.
        # Off any render path, so it is stored inline and a bad body is a 400.
        if not config.token_ok(request.headers.get("authorization")):
            return Response(status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return Response(status_code=400)
        sample = ingest.parse_limits(payload) if isinstance(payload, dict) else None
        if sample is None:
            return Response(status_code=400)
        s = state(request)
        ingest.store_sample(s.conn, sample, entrypoint="atlas-deck")
        await s.notify_change()
        return Response(status_code=204)

    @app.post("/ingest/usage")
    async def ingest_usage(request: Request):
        # `claude-quota ship` posts transcript-derived usage here, in batches.
        # The upserts run in a worker thread so a big batch never stalls SSE,
        # the status line ingest or OTLP on the event loop.
        if not config.token_ok(request.headers.get("authorization")):
            return Response(status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        s = state(request)
        counts = await asyncio.to_thread(usage_ingest.store, s.conn, payload)
        if counts.get("requests"):
            await s.notify_change()
        return JSONResponse(counts)

    # Reads are plain `def` handlers, so they run in the thread pool and never
    # block the event loop that serves ingest. Each answer is cached for a few
    # seconds (one compute shared by every client) on a separate connection.
    @app.get("/api/usage")
    def api_usage(
        request: Request,
        window: str = Query("7d"),
        p: Annotated[list[str] | None, Query()] = None,
        by: str = Query(""),
    ):
        # The path is repeated ?p= segments, since groups (ATLAS folders) can
        # themselves contain slashes.
        path = tuple(p or [])
        return JSONResponse(state(request).read(
            ("usage", window, path, by), 10,
            lambda c: usage_tree.build(c, window=window, path=list(path), by=by or None)))

    @app.get("/api/overview")
    def api_overview(request: Request, window: str = Query("5h")):
        return JSONResponse(state(request).read(
            ("overview", window), 5, lambda c: overview.build(c, window=window)))

    # The weekly budget. /api/budget is the dashboard's (behind the proxy's
    # access control, like the rest of /api); /ingest/budget is the same answer
    # for machines (ATLAS, the CLI on another host), behind the ingest token,
    # because /ingest/* is the token-authenticated path a proxy lets through.
    def _budget(request: Request) -> JSONResponse:
        return JSONResponse(state(request).read(("budget",), 20, budget.build))

    async def _budget_plan(request: Request) -> JSONResponse:
        try:
            change = await request.json()
        except Exception:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(change, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        s = state(request)
        try:
            await asyncio.to_thread(budget.save_plan, s.conn, change)
        except budget.PlanError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        s.cache.drop("budget")
        await s.notify_change()
        return _budget(request)

    @app.get("/api/budget")
    def api_budget(request: Request):
        return _budget(request)

    @app.put("/api/budget/plan")
    async def api_budget_plan(request: Request):
        return await _budget_plan(request)

    @app.get("/ingest/budget")
    def ingest_budget(request: Request):
        if not config.token_ok(request.headers.get("authorization")):
            return Response(status_code=401)
        return _budget(request)

    @app.put("/ingest/budget/plan")
    async def ingest_budget_plan(request: Request):
        if not config.token_ok(request.headers.get("authorization")):
            return Response(status_code=401)
        return await _budget_plan(request)

    @app.get("/api/summary")
    def api_summary(request: Request):
        return JSONResponse(state(request).read(("summary",), 5, summary.build))

    @app.get("/api/sessions")
    def api_sessions(request: Request):
        return JSONResponse(state(request).read(("sessions",), 5, sessions.build))

    @app.get("/api/breakdown")
    def api_breakdown(
        request: Request,
        by: str = Query("model"),
        window: str = Query("5h"),
    ):
        return JSONResponse(state(request).read(
            ("breakdown", by, window), 10, lambda c: breakdown.build(c, by=by, window=window)))

    @app.get("/api/history")
    def api_history(request: Request, days: int = Query(14)):
        return JSONResponse(state(request).read(
            ("history", days), 30, lambda c: history.build(c, days=days)))

    @app.get("/api/health")
    def api_health(request: Request):
        s = state(request)
        return JSONResponse(s.read(("health",), 5, lambda c: health.build(c, s)))

    @app.get("/events")
    async def sse(request: Request):
        return StreamingResponse(
            events.event_stream(state(request)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # OTLP over HTTP on the main port too, so a single reverse-proxied domain
    # serves the API, web, statusline ingest AND telemetry (remote/container
    # mode). Locally, Claude Code uses the gRPC receiver; these are harmless.
    @app.post("/v1/metrics")
    async def otlp_http_metrics(request: Request):
        if not config.token_ok(request.headers.get("authorization")):
            return Response(status_code=401)
        from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2

        req = metrics_service_pb2.ExportMetricsServiceRequest()
        req.ParseFromString(await request.body())
        s = state(request)
        s.otlp.note_metrics(otlp.handle_metrics_request(s.conn, req))
        await s.notify_change()
        return Response(
            metrics_service_pb2.ExportMetricsServiceResponse().SerializeToString(),
            media_type="application/x-protobuf",
        )

    @app.post("/v1/logs")
    async def otlp_http_logs(request: Request):
        if not config.token_ok(request.headers.get("authorization")):
            return Response(status_code=401)
        from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

        req = logs_service_pb2.ExportLogsServiceRequest()
        req.ParseFromString(await request.body())
        s = state(request)
        s.otlp.note_logs(otlp.handle_logs_request(s.conn, req))
        return Response(
            logs_service_pb2.ExportLogsServiceResponse().SerializeToString(),
            media_type="application/x-protobuf",
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
