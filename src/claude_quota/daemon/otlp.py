"""OpenTelemetry ingest: the only source of attribution.

Claude Code exports metrics and logs over OTLP. We stand up both transports the
env block configures (gRPC on :4317, HTTP/protobuf on :4318), decode the
protobuf directly with the generated ``opentelemetry-proto`` stubs, and fold
the datapoints into ``usage_bucket`` (attribution) and ``event_log``
(annotations like compaction spikes).

We intentionally do not run a full OpenTelemetry Collector: a single machine
does not need one.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from typing import Any

import grpc
from opentelemetry.proto.collector.logs.v1 import (
    logs_service_pb2,
    logs_service_pb2_grpc,
)
from opentelemetry.proto.collector.metrics.v1 import (
    metrics_service_pb2,
    metrics_service_pb2_grpc,
)

from .. import config
from .state import AppState

# Metrics we care about. Everything else is ignored.
COST_METRIC = "claude_code.cost.usage"
TOKEN_METRIC = "claude_code.token.usage"
SESSION_METRIC = "claude_code.session.count"
ACTIVE_METRIC = "claude_code.active_time.total"


def _any_value(v: Any) -> Any:
    """Unwrap an OTLP AnyValue oneof into a plain Python value."""
    which = v.WhichOneof("value")
    if which is None:
        return None
    if which == "string_value":
        return v.string_value
    if which == "bool_value":
        return v.bool_value
    if which == "int_value":
        return v.int_value
    if which == "double_value":
        return v.double_value
    if which == "bytes_value":
        return v.bytes_value
    if which == "array_value":
        return [_any_value(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _any_value(kv.value) for kv in v.kvlist_value.values}
    return None


def _attrs(attributes) -> dict[str, Any]:
    return {kv.key: _any_value(kv.value) for kv in attributes}


def _first(attrs: dict, *keys: str) -> Any:
    """Return the first present attribute among dotted/underscore variants."""
    for k in keys:
        if k in attrs and attrs[k] is not None:
            return attrs[k]
    return None


def _number(dp) -> float:
    which = dp.WhichOneof("value")
    if which == "as_double":
        return float(dp.as_double)
    if which == "as_int":
        return float(dp.as_int)
    return 0.0


def _bucket_key(session_id: str, attrs: dict) -> tuple:
    # NULLs are distinct in SQLite unique indexes, which would defeat the
    # WITHOUT ROWID accumulation. Use "" as the sentinel for "none" so the
    # primary key merges rows the way we want.
    model = _first(attrs, "model") or ""
    query_source = _first(attrs, "query_source", "query.source") or ""
    agent_name = _first(attrs, "agent.name", "agent_name") or ""
    skill_name = _first(attrs, "skill.name", "skill_name") or ""
    mcp_server = _first(attrs, "mcp_server.name", "mcp_server", "mcp.server.name") or ""
    plugin_name = _first(attrs, "plugin.name", "plugin_name") or ""
    effort = _first(attrs, "effort") or ""
    return (session_id, model, query_source, agent_name, skill_name, mcp_server,
            plugin_name, effort)


def _session_id(attrs: dict) -> str | None:
    sid = _first(attrs, "session.id", "session_id")
    return str(sid) if sid else None


def _device(resource_attrs: dict) -> str | None:
    d = _first(
        resource_attrs, "device.name", "device", "host.name",
        "service.instance.id", "host.arch",
    )
    return str(d) if d else None


def handle_metrics_request(
    conn: sqlite3.Connection, req, now: int | None = None
) -> int:
    """Fold an ExportMetricsServiceRequest into usage_bucket. Returns datapoints."""
    from .. import db

    now = now or int(time.time())
    bucket_ts = now - (now % config.BUCKET_SECONDS)

    # Accumulate per (bucket_ts, key) so one request is one batch of upserts.
    acc: dict[tuple, dict[str, float]] = defaultdict(
        lambda: {
            "cost_usd": 0.0,
            "tok_input": 0.0,
            "tok_output": 0.0,
            "tok_cache_r": 0.0,
            "tok_cache_w": 0.0,
            "active_ms": 0.0,
        }
    )
    seen_sessions: dict[str, dict] = {}
    session_device: dict[str, str] = {}
    n_points = 0

    for rm in req.resource_metrics:
        device = _device(_attrs(rm.resource.attributes))
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                which = metric.WhichOneof("data")
                if which == "sum":
                    points = metric.sum.data_points
                elif which == "gauge":
                    points = metric.gauge.data_points
                else:
                    continue
                for dp in points:
                    n_points += 1
                    attrs = _attrs(dp.attributes)
                    sid = _session_id(attrs)
                    value = _number(dp)
                    if sid and device and sid not in session_device:
                        session_device[sid] = device
                    if metric.name == SESSION_METRIC:
                        start_type = _first(attrs, "start_type", "start.type")
                        # agents_view is a UI process, not a conversation.
                        if start_type == "agents_view":
                            continue
                        if sid:
                            seen_sessions[sid] = {"start_type": start_type}
                        continue
                    if not sid:
                        continue
                    key = (bucket_ts, *_bucket_key(sid, attrs))
                    row = acc[key]
                    if metric.name == COST_METRIC:
                        row["cost_usd"] += value
                    elif metric.name == TOKEN_METRIC:
                        ttype = _first(attrs, "type") or ""
                        if ttype == "input":
                            row["tok_input"] += value
                        elif ttype == "output":
                            row["tok_output"] += value
                        elif ttype in ("cacheRead", "cache_read"):
                            row["tok_cache_r"] += value
                        elif ttype in ("cacheCreation", "cache_creation"):
                            row["tok_cache_w"] += value
                    elif metric.name == ACTIVE_METRIC:
                        row["active_ms"] += value * 1000.0

    for sid, meta in seen_sessions.items():
        db.upsert_session(
            conn, sid, now, device=session_device.get(sid), entrypoint="otel",
            start_type=meta.get("start_type"),
        )

    if acc:
        rows = []
        upserted: set[str] = set()
        for (bts, sid, model, qs, agent, skill, mcp, plugin, effort), v in acc.items():
            if sid not in upserted:
                db.upsert_session(conn, sid, now, device=session_device.get(sid), entrypoint="otel")
                upserted.add(sid)
            rows.append(
                (
                    bts, sid, model, qs, agent, skill, mcp, plugin, effort,
                    v["cost_usd"], int(v["tok_input"]), int(v["tok_output"]),
                    int(v["tok_cache_r"]), int(v["tok_cache_w"]), int(v["active_ms"]),
                )
            )
        conn.executemany(
            """INSERT INTO usage_bucket
               (ts, session_id, model, query_source, agent_name, skill_name,
                mcp_server, plugin_name, effort, cost_usd, tok_input, tok_output,
                tok_cache_r, tok_cache_w, active_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ts, session_id, model, query_source, agent_name,
                           skill_name, mcp_server, plugin_name, effort)
               DO UPDATE SET
                 cost_usd    = cost_usd + excluded.cost_usd,
                 tok_input   = tok_input + excluded.tok_input,
                 tok_output  = tok_output + excluded.tok_output,
                 tok_cache_r = tok_cache_r + excluded.tok_cache_r,
                 tok_cache_w = tok_cache_w + excluded.tok_cache_w,
                 active_ms   = active_ms + excluded.active_ms""",
            rows,
        )
    conn.commit()
    return n_points


# Events we surface. compaction spikes burn rate and must be annotatable so
# projections do not look broken.
INTERESTING_EVENTS = {
    "claude_code.api_request",
    "claude_code.api_error",
    "claude_code.compaction",
    "claude_code.subagent_completed",
}


def handle_logs_request(conn: sqlite3.Connection, req, now: int | None = None) -> int:
    """Fold an ExportLogsServiceRequest into event_log. Returns record count."""
    import json

    now = now or int(time.time())
    rows: list[tuple] = []
    for rl in req.resource_logs:
        for sl in rl.scope_logs:
            for rec in sl.log_records:
                attrs = _attrs(rec.attributes)
                name = _first(attrs, "event.name", "name")
                if name not in INTERESTING_EVENTS:
                    continue
                sid = _session_id(attrs)
                ts = (
                    int(rec.time_unix_nano // 1_000_000_000)
                    if rec.time_unix_nano
                    else now
                )
                detail = {
                    k: v
                    for k, v in attrs.items()
                    if k not in ("event.name", "name", "session.id", "session_id")
                }
                rows.append((ts, sid, name, json.dumps(detail, default=str)))
    if rows:
        conn.executemany(
            "INSERT INTO event_log (ts, session_id, kind, detail) VALUES (?,?,?,?)",
            rows,
        )
        conn.commit()
    return len(rows)


def _grpc_authed(context) -> bool:
    md = dict(context.invocation_metadata() or ())
    return config.token_ok(md.get("authorization"))


class MetricsServicer(metrics_service_pb2_grpc.MetricsServiceServicer):
    def __init__(self, state: AppState) -> None:
        self.state = state

    async def Export(self, request, context):  # noqa: N802 (gRPC method name)
        if not _grpc_authed(context):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid token")
        points = handle_metrics_request(self.state.conn, request)
        self.state.otlp.note_metrics(points)
        await self.state.notify_change()
        return metrics_service_pb2.ExportMetricsServiceResponse()


class LogsServicer(logs_service_pb2_grpc.LogsServiceServicer):
    def __init__(self, state: AppState) -> None:
        self.state = state

    async def Export(self, request, context):  # noqa: N802
        if not _grpc_authed(context):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid token")
        n = handle_logs_request(self.state.conn, request)
        self.state.otlp.note_logs(n)
        return logs_service_pb2.ExportLogsServiceResponse()


async def serve_grpc(state: AppState) -> grpc.aio.Server:
    server = grpc.aio.server()
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(
        MetricsServicer(state), server
    )
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(
        LogsServicer(state), server
    )
    server.add_insecure_port(f"{config.DAEMON_HOST}:{config.OTLP_GRPC_PORT}")
    await server.start()
    return server


def build_http_app(state: AppState):
    """A tiny ASGI app for OTLP/HTTP on :4318 (POST /v1/metrics, /v1/logs)."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    async def metrics(request: Request) -> Response:
        if not config.token_ok(request.headers.get("authorization")):
            return Response(status_code=401)
        body = await request.body()
        req = metrics_service_pb2.ExportMetricsServiceRequest()
        req.ParseFromString(body)
        points = handle_metrics_request(state.conn, req)
        state.otlp.note_metrics(points)
        await state.notify_change()
        return Response(
            metrics_service_pb2.ExportMetricsServiceResponse().SerializeToString(),
            media_type="application/x-protobuf",
        )

    async def logs(request: Request) -> Response:
        if not config.token_ok(request.headers.get("authorization")):
            return Response(status_code=401)
        body = await request.body()
        req = logs_service_pb2.ExportLogsServiceRequest()
        req.ParseFromString(body)
        n = handle_logs_request(state.conn, req)
        state.otlp.note_logs(n)
        return Response(
            logs_service_pb2.ExportLogsServiceResponse().SerializeToString(),
            media_type="application/x-protobuf",
        )

    return Starlette(
        routes=[
            Route("/v1/metrics", metrics, methods=["POST"]),
            Route("/v1/logs", logs, methods=["POST"]),
        ]
    )
