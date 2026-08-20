"""The claude-quota / ccq command-line interface (Typer)."""

from __future__ import annotations

import json as jsonlib
import time

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
    by: str = typer.Option("model", "--by", help="model | agent | mcp | skill"),
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
    device: str = typer.Option(None, "--device", help="Device name (default: hostname)."),
    json: bool = typer.Option(False, "--json"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show actions, change nothing."),
) -> None:
    """Write the statusline wrapper and patch settings.json (idempotent)."""
    result = install_mod.install(server=server, token=token, device=device, dry_run=dry_run)
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
    """Import history from Claude Code transcripts (one-shot, best-effort)."""
    from .. import backfill as backfill_mod

    result = backfill_mod.run(days=days)
    if json:
        _emit_json(result)
        return
    con.print(
        f"[bold]backfill[/bold]: imported {result['records']} usage records from "
        f"{result['files_ok']} files ({result['files_failed']} skipped)."
    )


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
