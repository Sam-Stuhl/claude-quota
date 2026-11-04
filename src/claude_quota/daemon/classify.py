"""Put every request in a category, a group within it, and an item (a session).

The order of evidence, strongest first:

1. ATLAS's own stamp on the turn ("[Tuesday ... via schedule] Hourly watch"):
   the door says chat, voice or scheduled, and a scheduled turn names its
   routine. This is per turn, because one assistant thread mixes all three.
2. ATLAS's record of the session (``atlas_session``, from its database): who or
   what started it (Sam, the coordinator, a routine, the health detector) and
   which folder it was filed in.
3. Cowork's own transcript folder.
4. The opening prompt of ATLAS's background helpers, which never vary (memory
   gardener, portrait, job reviewer, quota probe).
5. The working folder: ``~/atlas/assistant`` is the assistant thread, and
   ``~/agents/<role>`` or ``~/atlas/roles/<role>`` is a role thread.
6. The launcher: an interactive ``claude`` (entrypoint ``cli``) is a terminal
   session. What is left is uncategorized, shown with its folder, never hidden.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

CATEGORIES: list[tuple[str, str]] = [
    ("atlas_chat", "ATLAS chat"),
    ("atlas_voice", "ATLAS voice"),
    ("roles", "Role threads"),
    ("scheduled", "Scheduled runs"),
    ("tasks_sam", "Tasks you started"),
    ("tasks_agent", "Tasks agents started"),
    ("event", "Event-triggered"),
    ("helpers", "Background helpers"),
    ("terminal", "Terminal sessions"),
    ("cowork", "Cowork"),
    ("other", "Uncategorized"),
]
CATEGORY_LABEL = dict(CATEGORIES)
RESIDUAL_KEY = "claude_app"
RESIDUAL_LABEL = "Claude app (chat, voice, mobile)"

HELPERS: list[tuple[str, str]] = [
    ("You are the memory gardener", "Memory gardener"),
    ("Below is a file written on", "Memory gardener"),
    ("You are building a portrait of Sam", "Portrait"),
    ("A background agent is doing a task", "Job reviewer"),
    ("/usage", "Quota probe"),
]

_STARTED_BY = {
    "sam": "tasks_sam",
    "terminal": "tasks_sam",
    "routine": "scheduled",
    "trigger": "event",
    "network-health": "event",
    "health": "event",
    "job": "event",
}

_DOOR = {"schedule": "scheduled", "voice": "atlas_voice", "voice session end": "atlas_voice"}


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("CLAUDE_QUOTA_TZ", "America/New_York"))
    except Exception:
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class Place:
    category: str
    group: str
    item: str  # session id
    estimated: bool = False  # True when only a heuristic placed it


def _day(ts: int | None) -> str:
    if not ts:
        return "(unknown day)"
    return datetime.fromtimestamp(ts, tz()).strftime("%a %b %-d")


def _home_rel(path: str | None) -> str:
    if not path:
        return "(unknown folder)"
    p = re.sub(r"^/Users/[^/]+", "~", path)
    p = re.sub(r"^/private/tmp", "/tmp", p)
    return p


def repo_name(cwd: str | None) -> str:
    """The repo a folder belongs to, folding worktrees into their repo."""
    if not cwd:
        return "(unknown folder)"
    p = cwd.split("/.claude/worktrees/", 1)[0]
    p = re.sub(r"/atlas/workspaces/s-[0-9a-f]+.*$", "/atlas/workspaces", p)
    base = os.path.basename(p.rstrip("/")) or p
    return base or "(unknown folder)"


def _role(cwd: str | None) -> str | None:
    m = re.search(r"/(?:agents|atlas/roles)/([^/]+)", cwd or "")
    return m.group(1) if m else None


def _helper(first_prompt: str | None) -> str | None:
    fp = (first_prompt or "").lstrip()
    for prefix, name in HELPERS:
        if fp.startswith(prefix):
            return name
    return None


def _is_assistant(cwd: str | None) -> bool:
    return bool(re.search(r"/atlas/assistant(/|$)|atlas-[a-z-]*assistant", cwd or ""))


def session_place(conv: dict | None, atlas: dict | None, sid: str) -> Place:
    """Where a session goes before looking at individual turns."""
    conv = conv or {}
    cwd = conv.get("cwd")

    if atlas:
        kind = atlas.get("kind")
        by = (atlas.get("started_by") or "").lower()
        folder = atlas.get("folder") or atlas.get("repo") or repo_name(cwd)
        if kind == "outside":
            return Place("terminal", repo_name(cwd), sid)
        if kind == "job":
            cat = "event" if (atlas.get("profile") == "ops" or by in _STARTED_BY) else "tasks_agent"
            return Place(cat, by or "job", sid)
        cat = _STARTED_BY.get(by, "tasks_agent")
        if cat == "scheduled":
            return Place(cat, atlas.get("series_key") or atlas.get("title") or "routine", sid)
        if cat == "event":
            return Place(cat, by, sid)
        return Place(cat, folder, sid)

    if conv.get("source") == "cowork":
        return Place("cowork", "Cowork", sid)

    helper = _helper(conv.get("first_prompt"))
    if helper:
        return Place("helpers", helper, sid)

    role = _role(cwd)
    if role:
        return Place("roles", role, sid)

    if _is_assistant(cwd):
        return Place("atlas_chat", _day(conv.get("first_ts")), sid, estimated=True)

    if conv.get("entrypoint") == "cli":
        return Place("terminal", repo_name(cwd), sid)

    return Place("other", _home_rel(cwd), sid, estimated=True)


def request_place(base: Place, door: str | None, routine: str | None, ts: int | None, model: str | None) -> Place:
    """Refine a session's place with the turn's own ATLAS stamp."""
    if door:
        cat = _DOOR.get(door, "atlas_chat")
        if base.category == "roles":
            return base  # a role thread's turns stay with the role
        if cat == "scheduled":
            return Place(cat, routine or "routine", base.item)
        return Place(cat, _day(ts), base.item)
    if base.category == "atlas_chat" and base.estimated and (model or "").startswith("claude-haiku"):
        # Before stamps, voice turns were the assistant's Haiku turns.
        return Place("atlas_voice", base.group, base.item, estimated=True)
    return base


def item_label(conv: dict | None, atlas: dict | None, sid: str) -> str:
    if atlas and atlas.get("title"):
        n = atlas.get("number")
        return f"#{n} {atlas['title']}" if n else atlas["title"]
    conv = conv or {}
    for k in ("title", "first_prompt"):
        if conv.get(k):
            return conv[k]
    return sid[:8]
