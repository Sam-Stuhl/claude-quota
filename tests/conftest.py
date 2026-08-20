import pytest

from claude_quota import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "quota.db")
    yield c
    c.close()
