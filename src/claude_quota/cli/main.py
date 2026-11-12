"""The claude-quota / ccq command-line interface (Typer)."""

from __future__ import annotations

import json as jsonlib
import time
from typing import Annotated

import typer
from rich.console import Group
from rich.text import Text

from . import client, process, render
from . import doctor as doctor_mod
from . import install as install_mod

app = typer.Typer(
    add_completion=False,
    help="A local quota dashboard for parallel Claude Code sessions.",
    no_args_is_help=False,
)
daemon_app = typer.Typer(help="Manage the background daemon.")
app.add_typer(daemon_app, name="daemon")

con = render.console()


def _emit_json(data) -> None:
    typer.echo(jsonlib.dumps(data, indent=2))


def _require_daemon() -> None:
    if not client.is_up():
        con.print(
            "[red]daemon not reachable[/red] on "
            f"{client.config.client_base_url()}. Start it with "
            "`claude-quota daemon start` (or check --server/--token).",
        )
        raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Run the live TUI when invoked with no subcommand."""
    if ctx.invoked_subcommand is not None:
        return
    _live_tui()


def _live_tui() -> None:
    from rich.live import Live

    try:
        with Live(refresh_per_second=4, screen=True, console=con) as live:
            while True:
                try:
                    s = client.summary()
                    ss = client.sessions()
                    header = Text("claude-quota", style="bold")
                    header.append(f"   {time.strftime('%H:%M:%S')}", style="dim")
                    body = Group(
                        header,
                        Text(""),
                        render.render_now(s),
                        Text(""),
                        render.render_sessions(ss),
                    )
                except client.DaemonError:
                    body = Text(
                        "daemon not reachable. Start it with "
                        "`claude-quota daemon start`.",
                        style="red",
                    )
                live.update(body)
                time.sleep(1)
    except KeyboardInterrupt:
        pass


@app.command()
def now(json: bool = typer.Option(False, "--json", help="Emit raw JSON.")) -> None:
    """One-shot summary, ~6 lines. The command you run most."""
    _require_daemon()
    data = client.summary()
    if json:
        _emit_json(data)
        return
    con.print(render.render_now(data))


@app.command()
def sessions(json: bool = typer.Option(False, "--json")) -> None:
    """Session leaderboard, sorted by share."""
    _require_daemon()
    data = client.sessions()
    if json:
        _emit_json(data)
        return
    con.print(render.render_sessions(data))


@app.command()
def history(
    days: int = typer.Option(14, "--days"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Per-window peaks and cap hits."""
    _require_daemon()
    data = client.history(days=days)
    if json:
        _emit_json(data)
        return
    con.print(render.render_history(data))


@app.command()
def cost(
    by: str = typer.Option("model", "--by", help="model | agent | mcp | skill | plugin | effort | device"),
    window: str = typer.Option("5h", "--window", help="5h | 7d | today"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Cost breakdown by dimension and window."""
    _require_daemon()
    data = client.breakdown(by=by, window=window)
    if json:
        _emit_json(data)
        return
    con.print(render.render_cost(data))


@app.command()
def usage(
    path: Annotated[
        list[str] | None, typer.Argument(help="Drill down: category, group, session id, turn id.")
    ] = None,
    window: str = typer.Option("7d", "--window", help="5h | today | 24h | 7d | 30d"),
    by: str = typer.Option(None, "--by", help="Pivot: model | mcp | skill | subagent | device | effort"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Where usage went: categories first, then groups, sessions, turns, requests."""
    _require_daemon()
    data = client.usage(window=window, path=path or [], by=by)
    if json:
        _emit_json(data)
        return
    con.print(render.render_usage(data))


@app.command()
def budget(
    changes: Annotated[
        list[str] | None,
        typer.Argument(help="Plan changes: work=1.6 school=1 light=0.4 reserve=10 bg=25 guard=70 "
                            "mon=work sat=light 2026-11-27=light (or =auto) cedar=off"),
    ] = None,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Today's share of the weekly limit, what is left, and the week's plan. With arguments, change the plan."""
    try:
        data = client.set_budget(_plan_change(changes)) if changes else client.budget()
    except (client.DaemonError, ValueError) as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    if json:
        _emit_json(data)
        return
    con.print(render.render_budget(data))


def _plan_change(args: list[str]) -> dict:
    out: dict = {}
    days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    for a in args:
        if "=" not in a:
            raise ValueError(f"expected key=value, got {a!r}")
        k, v = a.split("=", 1)
        k = k.strip().lower()
        if k in ("work", "school", "light"):
            out.setdefault("weights", {})[k] = float(v)
        elif k[:3] in days and not k[0].isdigit():
            out.setdefault("week", {})[k[:3]] = v
        elif k[:1].isdigit():
            out.setdefault("overrides", {})[k] = v
        elif k == "reserve":
            out["reserve_pct"] = float(v)
        elif k in ("bg", "background"):
            out["background_share"] = float(v)
        elif k in ("guard", "five_h_guard"):
            out["five_h_guard"] = float(v)
        elif k == "cedar":
            out["use_cedar"] = v.lower() in ("on", "true", "yes", "1")
        else:
            raise ValueError(f"unknown plan key {k!r}")
    return out


@app.command()
def ship(
    since_days: float = typer.Option(30, "--since-days", help="How far back a never-seen transcript may be."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and count, post nothing."),
    no_labels: bool = typer.Option(False, "--no-labels", help="Send no prompt text at all."),
    quiet: bool = typer.Option(False, "--quiet", help="Print only on failure (for launchd)."),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Send this machine's transcript usage to the daemon (incremental)."""
    from .. import ship as ship_mod

    result = ship_mod.run(since_days=since_days, dry_run=dry_run, with_labels=False if no_labels else None)
    if json:
        _emit_json(result)
    elif result.get("error"):
        con.print(f"[red]ship failed[/red]: {result['error']}")
    elif not quiet:
        con.print(
            f"shipped {result['requests']} requests, {result['turns']} turns, "
            f"{result['conversations']} sessions, {result['atlas_sessions']} ATLAS records"
            + (" (dry run)" if dry_run else "")
            + (f"; {result['bad_lines']} unreadable lines" if result["bad_lines"] else "")
        )
    if result.get("error"):
        raise typer.Exit(1)


@app.command()
def watch(
    alert: float = typer.Option(85.0, "--alert", help="5h %% threshold to alert on."),
    interval: float = typer.Option(30.0, "--interval", help="Poll seconds."),
    once: bool = typer.Option(False, "--once", help="Check once and exit."),
) -> None:
    """Exit 0 when the 5h threshold is crossed; for shell/notification hooks."""
    _require_daemon()
    while True:
        pct = client.summary()["five_hour"]["used_percentage"]
        if pct >= alert:
            con.print(f"5h at {pct:.0f}% (>= {alert:.0f}%)")
            raise typer.Exit(0)
        if once:
            con.print(f"5h at {pct:.0f}% (< {alert:.0f}%)")
            raise typer.Exit(1)
        time.sleep(interval)


@app.command()
def install(
    server: str = typer.Option(
        None, "--server", help="Remote server URL (e.g. https://claude-quota.example.com). Omit for local."
    ),
    token: str = typer.Option(None, "--token", help="Shared secret for a remote server."),
    token_file: str = typer.Option(
        None, "--token-file", help="Read the secret from a file (/dev/stdin for a pipe), keeping it out of argv."
    ),
    device: str = typer.Option(None, "--device", help="Device name (default: hostname)."),
    no_settings: bool = typer.Option(
        False, "--no-settings", help="Leave settings.json alone (when another tool manages it)."
    ),
    ship: bool = typer.Option(False, "--ship", help="Also load a launchd agent that ships transcripts every minute."),
    json: bool = typer.Option(False, "--json"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show actions, change nothing."),
) -> None:
    """Write the statusline wrapper and patch settings.json (idempotent)."""
    if token_file:
        with open(token_file) as f:
            token = f.read().strip() or None
    result = install_mod.install(
        server=server, token=token, device=device, dry_run=dry_run,
        patch_settings=not no_settings, ship=ship,
    )
    if json:
        _emit_json(result)
        return
    con.print(
        f"[bold]claude-quota install[/bold] ({result['mode']})" + (" (dry run)" if dry_run else "")
    )
    for a in result["actions"]:
        con.print(f"  • {a}")
    con.print(f"[dim]{result['note']}[/dim]")


@app.command()
def doctor(json: bool = typer.Option(False, "--json")) -> None:
    """Verify the daemon, statusline, and OTel integrations independently."""
    result = doctor_mod.run()
    if json:
        _emit_json(result)
        raise typer.Exit(0 if result["ok"] else 1)
    con.print(render.render_doctor(result))
    raise typer.Exit(0 if result["ok"] else 1)


@app.command()
def backfill(
    days: int = typer.Option(30, "--days"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Ship up to N days of transcript history (same as `ship --since-days N`)."""
    ship(since_days=days, dry_run=False, no_labels=False, quiet=False, json=json)


@daemon_app.command("start")
def daemon_start() -> None:
    """Start the daemon in the background."""
    result = process.start()
    con.print(f"daemon: {result['status']}" + (f" (pid {result['pid']})" if result.get("pid") else ""))
    if result["status"] in ("failed",):
        raise typer.Exit(1)


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the daemon."""
    con.print(f"daemon: {process.stop()['status']}")


@daemon_app.command("status")
def daemon_status(json: bool = typer.Option(False, "--json")) -> None:
    """Show daemon status."""
    st = process.status()
    if json:
        _emit_json(st)
        return
    state = "running" if st["running"] else "stopped"
    resp = "responding" if st["responding"] else "not responding"
    con.print(f"daemon: [bold]{state}[/bold]" + (f" (pid {st['pid']}, {resp})" if st["running"] else ""))
    con.print(f"[dim]db:  {st['db']}[/dim]")
    con.print(f"[dim]log: {st['log']}[/dim]")


@daemon_app.command("logs")
def daemon_logs(lines: int = typer.Option(40, "--lines", "-n")) -> None:
    """Print the tail of the daemon log."""
    typer.echo(process.tail(lines).rstrip())


if __name__ == "__main__":
    app()
