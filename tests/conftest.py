import pytest

from claude_quota import config, db

_TABLES = (
    "quota_sample", "raw_statusline", "usage_bucket", "session",
    "calibration", "window_history", "event_log",
)


@pytest.fixture
def conn(tmp_path):
    """A clean database connection.

    Uses SQLite by default; when DATABASE_URL points at Postgres the whole
    suite runs against Postgres instead (each test starts from empty tables).
    """
    if config.is_postgres():
        c = db.connect()
        for t in _TABLES:
            c.execute(f"DELETE FROM {t}")
        c.commit()
        yield c
        c.close()
    else:
        c = db.connect(tmp_path / "quota.db")
        yield c
        c.close()
