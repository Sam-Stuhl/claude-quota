"""Work shifts from Cedar, so the budget knows which days are work days.

Sam's Lumasoft shifts are the event occurrences of one Cedar record (the same
record work-dash reads). Optional: with no ``CEDAR_API_KEY`` the budget falls
back to its weekday pattern. Fetched at most every 30 minutes, and the last
good list is kept when Cedar is down, so a Cedar outage never blanks the plan.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from . import classify

log = logging.getLogger("claude_quota.cedar")

CACHE_SECONDS = 30 * 60
DEFAULT_URL = "https://cedar.kosmoskit.com"
DEFAULT_RECORD = "bff591b6-46fe-4e49-ac34-47e96a72db0f"


@dataclass(frozen=True)
class Shift:
    day: date
    start: int
    end: int


_lock = threading.Lock()
_cache: dict = {"at": 0.0, "shifts": None, "error": ""}


def configured() -> bool:
    return bool(os.environ.get("CEDAR_API_KEY"))


def _fetch() -> list[Shift]:
    url = os.environ.get("CEDAR_URL", DEFAULT_URL).rstrip("/")
    record = os.environ.get("CEDAR_WORK_RECORD", DEFAULT_RECORD)
    r = httpx.get(
        f"{url}/api/occurrences",
        params={"record": record},
        headers={"Authorization": f"Bearer {os.environ['CEDAR_API_KEY']}"},
        timeout=10,
    )
    r.raise_for_status()
    tz = classify.tz()
    out = []
    for o in r.json():
        if o.get("kind") != "event" or o.get("status") not in (None, "active") or not o.get("starts_at"):
            continue
        start = datetime.fromisoformat(o["starts_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat((o.get("ends_at") or o["starts_at"]).replace("Z", "+00:00"))
        out.append(Shift(start.astimezone(tz).date(), int(start.timestamp()), int(end.timestamp())))
    return sorted(out, key=lambda s: s.start)


def shifts() -> tuple[list[Shift] | None, str]:
    """(shifts, error). None when Cedar is not configured or has never answered."""
    if not configured():
        return None, ""
    with _lock:
        if time.time() - _cache["at"] < CACHE_SECONDS and _cache["shifts"] is not None:
            return _cache["shifts"], _cache["error"]
        try:
            got = _fetch()
            _cache.update(at=time.time(), shifts=got, error="")
        except Exception as e:  # noqa: BLE001 - Cedar down must not break the budget
            log.warning("cedar fetch failed: %s", e)
            # Keep the last good list; retry in 5 minutes rather than 30.
            _cache.update(at=time.time() - CACHE_SECONDS + 300, error=f"Cedar unreachable: {e}")
        return _cache["shifts"], _cache["error"]
