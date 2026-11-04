"""Rendering for the CLI: bars, the `now` block, tables, and the live TUI.

Respects NO_COLOR and non-TTY output automatically (Rich handles both), so the
commands pipe cleanly into scripts.
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

_EIGHTHS = " ▏▎▍▌▋▊▉"


def console() -> Console:
    return Console()


def bar(pct: float | None, width: int = 20) -> str:
    """A unicode meter with eighth-block resolution; caps the fill at 100%."""
    if pct is None:
        return "?" * width
    pct = max(0.0, pct)
    units = min(pct, 100.0) / 100.0 * width * 8
    full = int(units // 8)
    rem = int(units % 8)
    s = "█" * min(full, width)
    used = min(full, width)
    if used < width and rem:
        s += _EIGHTHS[rem]
        used += 1
    s += "░" * (width - used)
    return s


def fmt_time(ts: int | None) -> str:
    if not ts:
        return "?"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%-I:%M %p")


def fmt_day_time(ts: int | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%a %-I:%M %p")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(abs(seconds))
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _shares_line(summary: dict, top: int = 4) -> str:
    sessions = summary.get("sessions", [])
    unattributed = summary.get("unattributed_pct", 0.0)
    shown = sessions[:top]
    rest = sessions[top:]
    other = unattributed + sum(s["est_pct"] for s in rest)
    parts = [f"{s['label']} {s['est_pct']:.0f}%" for s in shown]
    parts.append(f"other {other:.0f}%")
    return "    " + " · ".join(parts)


def render_now(summary: dict) -> Group:
    fh = summary["five_hour"]
    sd = summary["seven_day"]
    proj = summary["projection"]
    degraded = summary.get("degraded")

    lines: list[Text] = []

    # 5h line.
    fh_pct = fh["used_percentage"]
    fh_txt = Text()
    fh_txt.append("5h  ")
    fh_txt.append(bar(fh_pct))
    label = f"  {fh_pct:.0f}%"
    if fh.get("capped"):
        label += f"  capped since {fmt_time(fh.get('capped_since'))}"
    fh_txt.append(label)
    fh_txt.append("   ")
    if proj.get("available") and proj.get("verdict"):
        verdict = proj["verdict"]
        color = "red" if verdict == "SHORT" else "green"
        fh_txt.append(
            f"cutoff {fmt_time(int(proj['cutoff_at']))} · "
            f"resets {fmt_time(fh.get('resets_at'))} · "
            f"{fmt_duration(proj.get('margin_seconds'))} "
        )
        fh_txt.append(verdict, style=f"bold {color}")
    elif proj.get("suppressed_reason"):
        annot = f"resets {fmt_time(fh.get('resets_at'))} · {proj['suppressed_reason']}"
        fh_txt.append(annot, style="dim")
    elif fh.get("resets_at"):
        fh_txt.append(f"resets {fmt_time(fh.get('resets_at'))}", style="dim")
    if degraded or fh.get("estimated"):
        fh_txt.append("  (estimated)", style="yellow")
    lines.append(fh_txt)

    # 7d line.
    sd_pct = sd["used_percentage"]
    sd_txt = Text()
    sd_txt.append("7d  ")
    if sd_pct is None:
        sd_txt.append(bar(None))
        sd_txt.append("   n/a")
    else:
        sd_txt.append(bar(sd_pct))
        sd_txt.append(f"  {sd_pct:.0f}%")
        sd_txt.append("   ")
        if sd.get("resets_at"):
            sd_txt.append(f"resets {fmt_day_time(sd.get('resets_at'))}", style="dim")
        else:
            sd_txt.append("on pace", style="dim")
    lines.append(sd_txt)

    lines.append(Text(_shares_line(summary), style="cyan"))
    if degraded:
        lines.append(
            Text(
                f"    ! estimated from local telemetry: {summary.get('degraded_reason')}",
                style="yellow",
            )
        )
    return Group(*lines)


def render_sessions(data: dict) -> Table:
    t = Table(title="Sessions (this 5h window)")
    t.add_column("session")
    t.add_column("share", justify="right")
    t.add_column("est %", justify="right")
    t.add_column("cost", justify="right")
    t.add_column("$/hr", justify="right")
    t.add_column("last", justify="right")
    t.add_column("", justify="left")
    for s in data.get("sessions", []):
        t.add_row(
            s["label"],
            f"{s['share_of_sessions_pct']:.0f}%",
            f"{s['est_pct']:.1f}",
            f"${s['cost_usd']:.2f}",
            f"${s['burn_usd_per_hour']:.2f}",
            fmt_duration(s["last_active_ago_s"]) + " ago",
            "idle" if s.get("idle") else "active",
        )
    if data.get("unattributed_pct"):
        t.add_row(
            "[dim]unattributed (claude.ai / Cowork)[/dim]",
            "",
            f"{data['unattributed_pct']:.1f}",
            "",
            "",
            "",
            "",
        )
    return t


def render_history(data: dict) -> Table:
    t = Table(title=f"Window history ({data.get('days')}d)")
    t.add_column("window reset")
    t.add_column("peak", justify="right")
    t.add_column("cap", justify="center")
    t.add_column("cost", justify="right")
    t.add_column("sessions", justify="right")
    for w in data.get("windows", []):
        t.add_row(
            fmt_day_time(w["reset_at"]),
            f"{w['peak_pct']:.0f}%",
            "HIT" if w["hit_cap"] else "-",
            f"${w['total_cost']:.2f}",
            str(w["session_count"]),
        )
    return t


def render_cost(data: dict) -> Table:
    t = Table(title=f"Cost by {data.get('by')} ({data.get('window')})")
    t.add_column(data.get("by", "key"))
    t.add_column("cost", justify="right")
    t.add_column("in", justify="right")
    t.add_column("out", justify="right")
    t.add_column("cache r/w", justify="right")
    for item in data.get("items", []):
        t.add_row(
            item["key"],
            f"${item['cost_usd']:.2f}",
            f"{item['tok_input']:,}",
            f"{item['tok_output']:,}",
            f"{item['tok_cache_read']:,}/{item['tok_cache_write']:,}",
        )
    return t


def render_usage(data: dict) -> Group:
    crumbs = " / ".join(c["label"][:50] for c in data.get("breadcrumbs", []))
    node = data["node"]
    head = Text()
    head.append(crumbs, style="bold")
    head.append(f"   {data['window']} · ${node['cost_usd']:.2f} list price · {node['requests']} requests", style="dim")
    parts: list = [head]

    def table(title: str, items: list[dict], keyed: bool) -> Table:
        t = Table(title=title, title_justify="left")
        if keyed:
            t.add_column("key", style="dim", overflow="fold", max_width=14)
        t.add_column("name", overflow="ellipsis", max_width=60, no_wrap=True)
        t.add_column("share", justify="right")
        t.add_column("cost", justify="right")
        t.add_column("5h pts", justify="right")
        t.add_column("reqs", justify="right")
        t.add_column("vs prev", justify="right")
        for c in items:
            chg = "" if c.get("change") is None else f"{c['change'] * 100:+.0f}%"
            pts = "" if c.get("est_5h_points") is None else f"{c['est_5h_points']:.1f}"
            row = [c["label"], f"{c['share'] * 100:.1f}%", f"${c['cost_usd']:.2f}", pts, str(c["requests"]), chg]
            t.add_row(*([c["key"][:14]] if keyed else []) + row)
        return t

    if data.get("requests") is not None:
        t = Table(title="requests", title_justify="left")
        for col in ("time", "model", "source", "in", "out", "cache r", "cache w", "cost"):
            t.add_column(col, justify="right" if col not in ("model", "source") else "left")
        for r in data["requests"]:
            t.add_row(fmt_time(r["ts"]), (r["model"] or "").replace("claude-", ""),
                      r.get("agent_type") or r.get("mcp_server") or "main",
                      f"{r['tok_input']:,}", f"{r['tok_output']:,}", f"{r['tok_cache_r']:,}",
                      f"{r['tok_cache_w']:,}", f"${r['cost_usd']:.3f}")
        parts.append(t)
    elif data.get("sections"):
        for name, items in data["sections"].items():
            if items:
                parts.append(table(name, items[:25], keyed=(name == "turns")))
    else:
        parts.append(table(data.get("by") or "children", data.get("children", [])[:40],
                           keyed=len(data.get("path", [])) >= 2))
        res = data.get("residual")
        if res:
            if res.get("est_5h_points") is not None:
                parts.append(Text(f"{res['label']}: about {res['est_5h_points']:.1f} pts (estimated)", style="dim"))
            else:
                parts.append(Text(f"{res['label']}: not measurable here ({res.get('note')})", style="dim"))
    return Group(*parts)


def render_doctor(result: dict) -> Group:
    lines: list[Text] = []
    for c in result["checks"]:
        mark = "✓" if c["ok"] else "✗"
        style = "green" if c["ok"] else "red"
        line = Text()
        line.append(f"{mark} ", style=f"bold {style}")
        line.append(f"{c['name']}: ", style="bold")
        line.append(c["detail"])
        lines.append(line)
        if not c["ok"] and c.get("fix"):
            lines.append(Text(f"    fix: {c['fix']}", style="dim"))
    return Group(*lines)
