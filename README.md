# claude-quota

A local quota dashboard for parallel Claude Code sessions. One daemon, two clients (a CLI and a web view).

It exists to answer a single question:

> **Do I have enough headroom left in this 5-hour window to start the thing I'm about to start, and which session is eating it?**

> [!NOTE]
> **Status: early development.** The design is complete (see [`SPEC.md`](SPEC.md)) and the repository is being built out milestone by milestone. The commands and endpoints below describe the target interface, not all of which is wired up yet. Follow the [roadmap](#roadmap) for what actually works today.

## Why

Claude Code enforces a rolling 5-hour usage window and a 7-day window. `/usage` gives you a point-in-time number, but it cannot tell you where the window is heading, how fast you are burning it, or which of several concurrent sessions is responsible. When you run three or four sessions at once, "can I start this next big task" becomes a real question with no good answer.

claude-quota watches those windows over time. It reads the true utilization percentage straight from Claude Code, attributes consumption to individual sessions, projects when you will hit the cap, and compares that against the actual reset time.

## What it does

- **True quota, not a guess.** The 5-hour and 7-day percentages come from Claude Code's own status line payload, not from counting tokens and estimating a limit.
- **Per-session attribution.** OpenTelemetry metrics rank concurrent sessions by real cost impact, so you can see which one is draining the window.
- **Time-to-exhaustion projection.** A burn-rate regression projects the cap-hit time and compares it to the real reset, with a confidence range rather than a single misleading number.
- **History.** Closed windows are retained so daily and weekly patterns are visible.
- **Headless and remote-friendly.** Runs as one daemon on a home server, reachable over Tailscale, with a CLI that works cleanly over SSH.

## Design principles

- **Not a proxy.** It never sits between Claude Code and the API.
- **No cloud, no auth, no multi-user.** Single operator, LAN or Tailscale only.
- **It reports, it never enforces.** It will not block or kill a session.
- **The status line is the only source of quota truth.** Everything else (telemetry, transcripts) is attribution or backfill, and is calibrated against that truth rather than trusted on its own.

## Architecture

```
Claude Code sessions (N concurrent)
  |- OTel metrics/logs ---------> OTLP receiver (in-daemon, :4317 grpc + :4318 http)
  |- statusLine hook -----------> POST /ingest/statusline
                                       |
                    +------------------v------------------+
                    |   claude-quotad   (FastAPI, :7788)  |
                    |   ingest . calibrate . project      |
                    +------------------+------------------+
                                       |  SQLite (WAL)
                    +------------------+------------------+
                    |                                     |
          GET /api/* + /events (SSE)          claude-quota / ccq (CLI)
                    |                                     |
               web client                            Rich TUI
```

One process. The OTLP receiver is embedded in the daemon rather than run as a separate collector, so there is nothing else to keep alive.

### Three data sources, three distinct jobs

| Source | Job |
|---|---|
| **statusLine hook** | The only source of quota truth. A fire-and-forget wrapper posts each render's JSON to the daemon. It never blocks your terminal and exits cleanly even when the daemon is down. |
| **OpenTelemetry** | The only source of attribution. Per-session, per-model cost and token metrics rank who is consuming the window. |
| **Transcripts** | Backfill only, quarantined to a single module. A one-shot importer seeds history so the trends view is not empty on day one. |

## Stack

- Python 3.12, FastAPI + uvicorn, SQLite with WAL.
- CLI: Typer + Rich.
- Web: a single page served by the daemon, vanilla JS, live updates over SSE.
- Packaged as a [`uv`](https://github.com/astral-sh/uv) project.

## Install

> Not yet published. Once the first milestone lands:

```bash
git clone https://github.com/Sam-Stuhl/claude-quota.git
cd claude-quota
uv sync
```

`claude-quota install` writes the status line wrapper and patches `~/.claude/settings.json` for you (idempotent). `claude-quota doctor` then verifies that all three integrations are actually arriving.

## Usage

```
claude-quota                    # live TUI, full-screen Rich, ~1s refresh
claude-quota now                # one-shot ~6-line summary, for SSH and scripts
claude-quota sessions           # session leaderboard, sorted by share
claude-quota history [--days N] # per-window peaks and cap hits
claude-quota cost [--by model|agent|mcp|skill] [--window 5h|7d|today]
claude-quota watch --alert 85   # exit 0 when a threshold is crossed
claude-quota install            # write the statusline wrapper + patch settings.json
claude-quota backfill [--days N]
claude-quota doctor             # verify telemetry, statusline, and rate_limits
claude-quota daemon [start|stop|status|logs]
```

`ccq` is installed as a shorthand for the same entry point. Every subcommand supports `--json`, respects `NO_COLOR`, and drops to plain output when it detects a non-TTY so it pipes cleanly.

`claude-quota now` is the command that gets read most:

```
5h  ############+.......  62%   cutoff 3:58 PM . resets 4:41 PM . 43m SHORT
7d  ######+.............  31%   on pace
    atlas 34% . banking-dash 21% . planner 5% . bsi-intake 2% . other 0%
```

## Roadmap

Built in milestones. Milestone 2 is the first genuinely useful cut.

1. **Ingest.** Daemon, SQLite, status line wrapper, `install`, `doctor`. Prove both streams land.
2. **Truth.** `/api/summary` and `claude-quota now` showing real 5h/7d from the status line.
3. **Attribution.** OTLP receiver, usage buckets, session leaderboard, `claude-quota sessions`.
4. **Calibration and projection.** The non-negative least-squares fit, burn rate, cutoff verdict, confidence range.
5. **Web.** SSE, the time-axis rail, the full page.
6. **History.** Closed-window history, the backfill importer, trends, `claude-quota watch`.

## Contributing

Issues and pull requests are welcome. The full design lives in [`SPEC.md`](SPEC.md); read it before proposing changes to the ingest, calibration, or projection logic, since those pieces depend on assumptions that are documented there.

## License

[MIT](LICENSE)
