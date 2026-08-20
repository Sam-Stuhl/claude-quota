from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.metrics.v1 import metrics_pb2

from claude_quota.daemon import otlp


def _kv(key, *, s=None, i=None, d=None):
    v = common_pb2.AnyValue()
    if s is not None:
        v.string_value = s
    elif i is not None:
        v.int_value = i
    elif d is not None:
        v.double_value = d
    return common_pb2.KeyValue(key=key, value=v)


def _metric(name, points):
    m = metrics_pb2.Metric(name=name)
    m.sum.data_points.extend(points)
    return m


def _dp(attrs, *, as_double=None, as_int=None):
    dp = metrics_pb2.NumberDataPoint()
    dp.attributes.extend(attrs)
    if as_double is not None:
        dp.as_double = as_double
    if as_int is not None:
        dp.as_int = as_int
    return dp


def _request(metrics):
    req = metrics_service_pb2.ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    sm = rm.scope_metrics.add()
    sm.metrics.extend(metrics)
    return req


def test_cost_and_tokens_folded_into_buckets(conn):
    cost = _metric(
        otlp.COST_METRIC,
        [_dp([_kv("session.id", s="sess1"), _kv("model", s="claude-opus-5")], as_double=0.5)],
    )
    tokens = _metric(
        otlp.TOKEN_METRIC,
        [
            _dp(
                [_kv("session.id", s="sess1"), _kv("model", s="claude-opus-5"), _kv("type", s="input")],
                as_int=1200,
            )
        ],
    )
    n = otlp.handle_metrics_request(conn, _request([cost, tokens]), now=1000)
    assert n == 2
    row = conn.execute(
        "SELECT * FROM usage_bucket WHERE session_id='sess1'"
    ).fetchone()
    assert abs(row["cost_usd"] - 0.5) < 1e-9
    assert row["tok_input"] == 1200
    assert row["ts"] == 1000  # floored to a 10s bucket


def test_agents_view_session_filtered(conn):
    m = _metric(
        otlp.SESSION_METRIC,
        [
            _dp([_kv("session.id", s="ui"), _kv("start_type", s="agents_view")], as_int=1),
            _dp([_kv("session.id", s="real"), _kv("start_type", s="fresh")], as_int=1),
        ],
    )
    otlp.handle_metrics_request(conn, _request([m]), now=1000)
    ids = {r["session_id"] for r in conn.execute("SELECT session_id FROM session")}
    assert "real" in ids
    assert "ui" not in ids
