"""Parse Claude Code transcripts into per-request usage records.

The transcript format is internal to Claude Code and changes between versions,
so this is the only module that reads it. It is deliberately tolerant: an
unknown line is skipped, a missing field becomes None, and nothing here raises
on bad input. ``ship`` calls it incrementally, carrying a little state per file
between reads so a read that stops mid-turn picks up correctly.

What one file yields:

* requests: one per API response (deduplicated by requestId, since a response
  with several content blocks is written as several lines), with tokens, model,
  list-price cost and what Claude Code attributes it to (subagent, skill, MCP
  server).
* conversations: per session, the folder, launcher, title and opening prompt.
* turns: per prompt (``promptId``), when it started, where it came from (typed,
  sdk, queued, system) and a short label.
* subagents: from the ``agent-*.meta.json`` beside a subagent transcript.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import pricing

LABEL_CHARS = 80
_QS_MEMORY = 64  # api-request ids remembered across reads, per file


@dataclass
class Carry:
    """Per-file state that survives between incremental reads."""

    offset: int = 0
    prompt_id: str | None = None
    query_sources: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"offset": self.offset, "prompt_id": self.prompt_id, "qs": self.query_sources}

    @classmethod
    def from_json(cls, d: dict | None) -> Carry:
        d = d or {}
        return cls(
            offset=int(d.get("offset") or 0),
            prompt_id=d.get("prompt_id"),
            query_sources=dict(d.get("qs") or {}),
        )


@dataclass
class Parsed:
    requests: dict[str, dict] = field(default_factory=dict)
    conversations: dict[str, dict] = field(default_factory=dict)
    turns: dict[str, dict] = field(default_factory=dict)
    subagents: dict[str, dict] = field(default_factory=dict)
    lines: int = 0
    bad_lines: int = 0

    def merge(self, other: Parsed) -> None:
        self.requests.update(other.requests)
        for sid, conv in other.conversations.items():
            _merge_conv(self.conversations, sid, conv)
        for pid, t in other.turns.items():
            self.turns.setdefault(pid, t)
        self.subagents.update(other.subagents)
        self.lines += other.lines
        self.bad_lines += other.bad_lines


def _ts(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def label(text: str | None) -> str | None:
    if not text:
        return None
    t = re.sub(r"<[^>]{1,40}>", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:LABEL_CHARS] or None


# ATLAS stamps each assistant-thread turn: "[Tuesday 2026-11-03 14:00 EST via
# schedule] Hourly watch. ...". The door (schedule, web, voice, telegram, deck)
# says what kind of turn it is, and a scheduled turn's first sentence names the
# routine. Searched near the start, after any thread-recap preamble.
_ATLAS_MARK = re.compile(r"\[[A-Z][a-z]+day [^\]\n]{0,40}? via ([a-z][a-z _-]{0,30})\]\s*(.*)", re.S)


def atlas_marker(text: str | None) -> tuple[str | None, str | None, str | None]:
    """(door, routine, rest) from an ATLAS turn stamp, or Nones."""
    if not text:
        return None, None, None
    m = _ATLAS_MARK.search(text[:4000])
    if not m:
        return None, None, None
    door, rest = m.group(1).strip(), m.group(2)
    routine = None
    if door == "schedule":
        body = rest.strip()
        if body.startswith("["):  # "[Scheduled check-in: morning. Nobody ...]"
            first = re.split(r"[.\]\n]", body[1:], maxsplit=1)[0]
        else:
            first = re.split(r"(?<=[.!?:])\s|\n", body, maxsplit=1)[0]
        routine = first.rstrip(".:!? ")[:60] or None
    return door, routine, rest


def _prompt_text(message: Any) -> str | None:
    """The human/SDK text of a user line, or None for a tool result."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(p for p in parts if p) or None
    return None


def _merge_conv(store: dict[str, dict], sid: str, new: dict) -> None:
    cur = store.get(sid)
    if cur is None:
        store[sid] = dict(new)
        return
    for k, v in new.items():
        if v is None:
            continue
        if k == "first_ts":
            cur[k] = min(cur.get(k) or v, v)
        elif k == "last_ts":
            cur[k] = max(cur.get(k) or v, v)
        elif k in ("title",):
            cur[k] = v  # the latest title wins (sessions get renamed)
        elif cur.get(k) is None:
            cur[k] = v


def source_of(path: Path) -> str:
    return "cowork" if "local-agent-mode-sessions" in str(path) else "code"


def read_subagent_meta(path: Path) -> dict | None:
    """agent-<id>.meta.json beside a subagent transcript, if there is one."""
    if not path.name.startswith("agent-"):
        return None
    meta = path.with_name(path.stem + ".meta.json")
    try:
        d = json.loads(meta.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def parse_lines(
    lines: list[str],
    carry: Carry,
    *,
    source: str = "code",
    with_labels: bool = True,
) -> Parsed:
    out = Parsed()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        out.lines += 1
        try:
            d = json.loads(raw)
        except ValueError:
            out.bad_lines += 1
            continue
        if not isinstance(d, dict):
            out.bad_lines += 1
            continue
        try:
            _one(d, carry, out, source, with_labels)
        except Exception:  # tolerate any shape change in a single line
            out.bad_lines += 1
    # Keep the remembered api-request ids bounded.
    if len(carry.query_sources) > _QS_MEMORY:
        keep = list(carry.query_sources.items())[-_QS_MEMORY:]
        carry.query_sources = dict(keep)
    return out


def _one(d: dict, carry: Carry, out: Parsed, source: str, with_labels: bool) -> None:
    kind = d.get("type")
    sid = d.get("sessionId")
    ts = _ts(d.get("timestamp"))

    if kind == "api-request":
        rid, qs = d.get("id"), d.get("querySource")
        if isinstance(rid, str) and isinstance(qs, str):
            carry.query_sources[rid] = qs
        return

    if kind in ("custom-title", "agent-name") and sid:
        title = d.get("customTitle") or d.get("agentName")
        if isinstance(title, str) and title.strip():
            _merge_conv(out.conversations, sid, {"title": title.strip()[:200]})
        return

    if not sid or kind not in ("user", "assistant"):
        return

    agent_id = d.get("agentId")
    conv = {
        "source": source,
        "cwd": d.get("cwd"),
        "entrypoint": d.get("entrypoint"),
        "git_branch": d.get("gitBranch"),
        "cc_version": d.get("version"),
        "first_ts": ts,
        "last_ts": ts,
    }

    if kind == "user":
        pid = d.get("promptId")
        if isinstance(pid, str):
            carry.prompt_id = pid
            text = _prompt_text(d.get("message"))
            if text is not None and pid not in out.turns:
                door, routine, rest = atlas_marker(text)
                shown = rest if door else text
                out.turns[pid] = {
                    "prompt_id": pid,
                    "session_id": sid,
                    "agent_id": agent_id,
                    "ts": ts,
                    "origin": d.get("promptSource") or d.get("turnOrigin"),
                    "door": door,
                    "routine": routine,
                    "label": label(shown) if with_labels else None,
                }
                if not agent_id and not d.get("isSidechain"):
                    # The opening prompt identifies background helpers, so it
                    # is kept (as a label) whenever labels are on.
                    conv["first_prompt"] = label(shown) if with_labels else None
        _merge_conv(out.conversations, sid, conv)
        return

    # assistant
    msg = d.get("message") or {}
    usage = msg.get("usage")
    rid = d.get("requestId") or msg.get("id")
    model = msg.get("model")
    if not isinstance(usage, dict) or not rid or model == "<synthetic>":
        _merge_conv(out.conversations, sid, conv)
        return

    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else None
    cache_w = _int(usage.get("cache_creation_input_tokens"))
    if cc is not None:
        w5, w1h = _int(cc.get("ephemeral_5m_input_tokens")), _int(cc.get("ephemeral_1h_input_tokens"))
        if w5 + w1h != cache_w:  # older shapes: trust the total
            w5, w1h = cache_w, 0
    else:
        w5, w1h = cache_w, 0
    tok_in, tok_out = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
    cache_r = _int(usage.get("cache_read_input_tokens"))

    qs = carry.query_sources.get(d.get("requestRef") or "")
    if qs is None:
        qs = f"agent:{d['attributionAgent']}" if d.get("attributionAgent") else (
            "subagent" if agent_id else "main"
        )
    agent_type = qs.rsplit(":", 1)[-1] if qs.startswith("agent:") else (d.get("attributionAgent") or None)

    out.requests[rid] = {
        "request_id": rid,
        "ts": ts,
        "session_id": sid,
        "agent_id": agent_id,
        "prompt_id": carry.prompt_id,
        "model": model,
        "query_source": qs,
        "agent_type": agent_type,
        "skill": d.get("attributionSkill"),
        "mcp_server": d.get("attributionMcpServer"),
        "mcp_tool": d.get("attributionMcpTool"),
        "effort": d.get("effort"),
        "speed": usage.get("speed"),
        "tok_input": tok_in,
        "tok_output": tok_out,
        "tok_cache_r": cache_r,
        "tok_cache_w": cache_w,
        "cost_usd": round(pricing.cost_usd(model, tok_in, tok_out, cache_r, w5, w1h), 6),
    }
    _merge_conv(out.conversations, sid, conv)


def read_incremental(
    path: Path, carry: Carry, *, with_labels: bool = True, max_bytes: int = 64 << 20
) -> Parsed:
    """Read complete lines appended since ``carry.offset`` and parse them.

    A partially written last line is left for the next read. A file that shrank
    (rewritten or truncated) is read again from the start.
    """
    size = path.stat().st_size
    if size < carry.offset:
        carry.offset = 0
        carry.prompt_id = None
    if size == carry.offset:
        return Parsed()
    with open(path, "rb") as f:
        f.seek(carry.offset)
        chunk = f.read(max_bytes)
    end = chunk.rfind(b"\n")
    if end < 0:
        if len(chunk) >= max_bytes:
            # One line longer than a whole read: skip it rather than stick here.
            carry.offset += len(chunk)
            return Parsed(bad_lines=1)
        return Parsed()
    body = chunk[: end + 1]
    carry.offset += len(body)
    lines = body.decode("utf-8", errors="replace").splitlines()
    parsed = parse_lines(lines, carry, source=source_of(path), with_labels=with_labels)
    meta = read_subagent_meta(path)
    if meta is not None:
        agent_id = path.stem[len("agent-") :]
        sid = next((r["session_id"] for r in parsed.requests.values()), None) or path.parent.parent.name
        parsed.subagents[agent_id] = {
            "agent_id": agent_id,
            "session_id": sid,
            "agent_type": meta.get("agentType"),
            "description": (meta.get("description") or "")[:200] or None,
            "model": meta.get("model"),
        }
    return parsed
