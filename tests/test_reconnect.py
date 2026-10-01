"""PgConn reopens a connection the server closed, and retries the call once."""

import psycopg
import pytest

from claude_quota.db import PgConn


class FakeRaw:
    """Stands in for a psycopg connection: fails while closed, records calls."""

    def __init__(self, closed: bool = False) -> None:
        self.closed = closed
        self.calls: list[tuple] = []

    def execute(self, sql, params=None):
        if self.closed:
            raise psycopg.OperationalError("the connection is closed")
        self.calls.append((sql, params))
        return "cursor"


def test_closed_connection_is_reopened_and_retried():
    dead = FakeRaw(closed=True)
    fresh = FakeRaw()
    opened = []

    def reconnect():
        opened.append(fresh)
        return fresh

    conn = PgConn(dead, reconnect=reconnect)
    assert conn.execute("SELECT MAX(ts) FROM quota_sample WHERE ts > ?", (1,)) == "cursor"
    assert opened == [fresh]
    assert fresh.calls == [("SELECT MAX(ts) FROM quota_sample WHERE ts > %s", (1,))]

    # Later calls use the new connection without reconnecting again.
    conn.execute("SELECT 1")
    assert len(opened) == 1
    assert fresh.calls[-1] == ("SELECT 1", None)


def test_other_operational_errors_are_not_retried():
    class Broken(FakeRaw):
        def execute(self, sql, params=None):
            raise psycopg.OperationalError("deadlock detected")

    opened = []
    conn = PgConn(Broken(), reconnect=lambda: opened.append(1) or FakeRaw())
    with pytest.raises(psycopg.OperationalError):
        conn.execute("SELECT 1")
    assert opened == []


def test_a_retry_that_fails_again_raises():
    conn = PgConn(FakeRaw(closed=True), reconnect=lambda: FakeRaw(closed=True))
    with pytest.raises(psycopg.OperationalError):
        conn.execute("SELECT 1")
