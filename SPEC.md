# claude-quota: build spec

A local quota dashboard for parallel Claude Code sessions. One daemon, two clients (CLI and web).

The single question it exists to answer: **do I have enough headroom left in this 5-hour window to start the thing I'm about to start, and which session is eating it?**

---

## 1. Goals and non-goals

### Goals

- Show true 5-hour and 7-day quota utilization, sourced from Claude Code itself, not guessed.
- Attribute consumption to individual concurrent sessions, ranked by real cost impact.
- Project time-to-exhaustion and compare it against the actual reset time.
- Retain history so daily and weekly patterns are visible.
- Run headless on the Mac mini, reachable over Tailscale, with a CLI that works over SSH.

### Non-goals

- Not a proxy. Never sit between Claude Code and the API.
- No cloud, no auth, no multi-user. Single operator, LAN/Tailscale only.
- Do not replicate `/usage`. This is for *watching over time*, not point checks.
- No enforcement. It reports; it never blocks or kills a session.

---

## 2. Architecture

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
          GET /api/* + /events (SSE)          claude-quota (CLI)
                    |                                     |
               web client                            Rich TUI
```

One process. The OTLP receiver is embedded in the daemon rather than a separate collector, so there is nothing else to keep alive. Use `opentelemetry-proto` generated stubs and handle `ExportMetricsServiceRequest` / `ExportLogsServiceRequest` directly; a full Collector is overkill for one machine.

### Stack

- Python 3.12, FastAPI + uvicorn, SQLite via `sqlite3` with WAL enabled.
- CLI: Typer + Rich.
- Web: single-page, served by the daemon from `/`. Vanilla JS is fine and preferred; there is no state complexity that earns a framework. Live updates over SSE, not polling.
- Package as a `uv` project. `claude-quotad` (daemon) and `ccq` (CLI) as the two console entry points.

---

## 3. Data sources

Three sources, three distinct jobs. Do not let them bleed into each other.

### 3.1 statusLine: the ONLY source of quota truth

Claude Code pipes a JSON blob to the configured `statusLine` command on each render. For OAuth (Pro/Max) sessions it contains:

```json
{
  "session_id": "...",
  "cwd": "/Users/sam/dev/atlas",
  "model": { "id": "claude-opus-5", "display_name": "Opus 5" },
  "workspace": { "current_dir": "...", "project_dir": "...", "git_worktree": "router-v3" },
  "version": "2.1.90",
  "cost": { "total_cost_usd": 4.82, "total_duration_ms": 0, "total_lines_added": 0, "total_lines_removed": 0 },
  "context_window": { "used_percentage": 20, "context_window_size": 200000, "total_input_tokens": 0, "total_output_tokens": 0 },
  "exceeds_200k_tokens": false,
  "rate_limits": {
    "five_hour": { "used_percentage": 62.0, "resets_at": 1738425600 },
    "seven_day": { "used_percentage": 31.0, "resets_at": 1738857600 }
  }
}
```

Install a wrapper script at `~/.claude/claude-quota-statusline.sh`. It must:

1. Read stdin once into a variable.
2. `curl -m 0.4 -s -X POST localhost:7788/ingest/statusline --data-binary @-` in the **background**, output discarded.
3. Render the user's actual status line to stdout and exit.

The wrapper must never block, never print daemon errors, and must exit 0 even when the daemon is down. A status line that hangs makes the whole terminal feel broken. Hard timeout of 400ms, fire-and-forget.

**Critical caveat to design around:** `rate_limits` is absent entirely (not null, not empty) in several documented cases: API-key auth instead of OAuth, before the first API response of a session, and intermittently after server-side changes (see anthropics/claude-code issues #40094 and #45133). Treat its presence as optional on every single ingest. When it has been missing for more than 10 minutes across all sessions, the UI must switch to a clearly-labelled degraded mode driven by the OTel estimate alone, showing "estimated" rather than a hard number. Never silently present an estimate as truth.

`resets_at` has appeared as both a unix epoch int and an ISO 8601 string across versions. Parse both.

### 3.2 OpenTelemetry: the ONLY source of attribution

Set in `~/.claude/settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
    "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    "OTEL_METRICS_INCLUDE_SESSION_ID": "true"
  }
}
```

Consume these metrics:

| Metric | Use |
|---|---|
| `claude_code.cost.usage` | **Primary ranking signal.** Attributes: `model`, `query_source` (main/subagent/auxiliary), `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `effort` |
| `claude_code.token.usage` | Secondary. `type` = input / output / cacheRead / cacheCreation |
| `claude_code.session.count` | Session start. `start_type` = fresh/resume/continue/agents_view. **filter out `agents_view`**, it is a UI process, not a conversation |
| `claude_code.active_time.total` | Distinguishes idle sessions from working ones |

Consume these events (logs signal): `claude_code.api_request` (has `cost_usd`, `duration_ms`, token counts, `request_id`), `claude_code.api_error`, `claude_code.compaction` (spikes burn rate, must be annotated on the timeline or projections look broken), `claude_code.subagent_completed`.

Note: `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` defaults to `delta`. Do not set it to cumulative. Delta is what the accumulator wants.

Do **not** enable `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_CONTENT`, or `OTEL_LOG_RAW_API_BODIES`. None of them serve this dashboard and they write conversation content to disk.

### 3.3 Transcripts: backfill only, quarantined

One-shot importer: `claude-quota backfill [--days 30]` reads `~/.claude/projects/*/*.jsonl` and populates history so the trends view is not empty on day one.

The docs state plainly that this format is internal to Claude Code and changes between versions. So: **the importer lives in exactly one module** (`claude-quota/backfill.py`), the daemon never imports it, and a parse failure prints a warning and skips the file. Nothing else in the codebase may read a transcript. When a release breaks it, the cost is one broken script.

---

## 4. Schema

```sql
PRAGMA journal_mode=WAL;

-- One row per statusline ingest. The quota ground-truth stream.
CREATE TABLE quota_sample (
  id            INTEGER PRIMARY KEY,
  ts            INTEGER NOT NULL,           -- unix seconds
  session_id    TEXT NOT NULL,
  five_h_pct    REAL,                       -- NULL when rate_limits absent
  five_h_reset  INTEGER,
  seven_d_pct   REAL,
  seven_d_reset INTEGER,
  cc_version    TEXT,
  had_limits    INTEGER NOT NULL            -- 0/1, for degraded-mode detection
);
CREATE INDEX idx_quota_ts ON quota_sample(ts);

-- Sessions seen, from either source.
CREATE TABLE session (
  session_id    TEXT PRIMARY KEY,
  first_seen    INTEGER NOT NULL,
  last_seen     INTEGER NOT NULL,
  cwd           TEXT,
  project_dir   TEXT,
  git_worktree  TEXT,
  entrypoint    TEXT,
  start_type    TEXT,
  color_idx     INTEGER NOT NULL            -- stable palette slot, assigned on insert
);

-- Accumulated OTel deltas, bucketed to 10s to keep the table small.
CREATE TABLE usage_bucket (
  ts            INTEGER NOT NULL,           -- floor to 10s
  session_id    TEXT NOT NULL,
  model         TEXT NOT NULL,
  query_source  TEXT,                       -- main | subagent | auxiliary
  agent_name    TEXT,
  skill_name    TEXT,
  mcp_server    TEXT,
  cost_usd      REAL NOT NULL DEFAULT 0,
  tok_input     INTEGER NOT NULL DEFAULT 0,
  tok_output    INTEGER NOT NULL DEFAULT 0,
  tok_cache_r   INTEGER NOT NULL DEFAULT 0,
  tok_cache_w   INTEGER NOT NULL DEFAULT 0,
  active_ms     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ts, session_id, model, query_source, agent_name, skill_name, mcp_server)
) WITHOUT ROWID;
CREATE INDEX idx_bucket_ts ON usage_bucket(ts);

-- Fitted cost->quota conversion, one row per model, rewritten by the calibrator.
CREATE TABLE calibration (
  model         TEXT PRIMARY KEY,
  pct_per_usd   REAL NOT NULL,
  r2            REAL,
  n_samples     INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);

-- Closed 5h windows, for the history view.
CREATE TABLE window_history (
  reset_at      INTEGER PRIMARY KEY,
  peak_pct      REAL NOT NULL,
  hit_cap       INTEGER NOT NULL,
  total_cost    REAL NOT NULL,
  session_count INTEGER NOT NULL
);

CREATE TABLE event_log (
  ts INTEGER NOT NULL, session_id TEXT, kind TEXT NOT NULL, detail TEXT
);
```

Retention: `usage_bucket` and `quota_sample` roll off at 90 days on daemon start. `window_history` is kept forever, it is tiny.

---

## 5. Calibration: the part that makes this better than a token ratio

The problem: quota percentage is server-side and account-wide. OTel cost is local and per-session. Nothing tells you the exchange rate, and it differs by model.

The solution: you observe both streams simultaneously, so you can fit it.

**Every 60 seconds:**

1. Take consecutive `quota_sample` rows with `had_limits = 1`. Compute `dpct` between them.
2. Discard the pair if a reset occurred in the interval (`five_h_reset` changed, or pct decreased).
3. Sum `usage_bucket.cost_usd` per model over the same interval into vector **c**.
4. Append the observation (**c**, dpct) to a rolling window of the last 14 days.

**Every 15 minutes**, refit: non-negative least squares of dpct against per-model cost. Coefficients are `pct_per_usd` per model. Write to `calibration` with the R2 and sample count.

Cold start: seed with a flat prior of `1.0 pct_per_usd` for every model, mark `n_samples = 0`, and label all derived figures "uncalibrated" in both clients until n >= 50 and R2 >= 0.7.

**Residual, and why it matters:** measured dpct will consistently exceed what local cost explains, because claude.ai chat and Cowork draw from the same pool. Track that residual explicitly as `unattributed`. Surface it in the UI as its own grey slice on the rail. It is real usage, not error, and hiding it makes per-session shares silently wrong.

---

## 6. Projection

```
burn_rate  = ordinary least squares slope of five_h_pct over the last 20 minutes
             of quota_sample rows, in pct/hour
```

Twenty minutes, not five. Five-minute windows swing wildly on a single compaction.

```
seconds_to_cap = (100 - current_pct) / burn_rate * 3600
cutoff_at      = now + seconds_to_cap
verdict        = cutoff_at < five_h_reset ? SHORT : CLEAR
margin         = |cutoff_at - five_h_reset|
```

**Confidence interval:** compute the standard error of the regression slope and publish the projection as a range. A single point estimate on a noisy signal is a lie with a decimal place. The UI shows the point value and the +/- in a smaller weight.

**Suppress the projection entirely** when: fewer than 4 samples in the window, burn_rate <= 0, or a `claude_code.compaction` event landed in the window (annotate instead: "compaction spike, projection paused"). Show "not enough signal" rather than a garbage number.

---

## 7. CLI surface

```
claude-quota                    # default: live TUI, full-screen Rich, ~1s refresh
claude-quota now                # one-shot summary, ~6 lines, for SSH and scripts
claude-quota sessions           # session leaderboard, sorted by share desc
claude-quota history [--days N] # per-window peaks, cap hits
claude-quota cost [--by model|agent|mcp|skill] [--window 5h|7d|today]
claude-quota watch --alert 85   # exit 0 when threshold crossed; for shell/notification hooks
claude-quota install            # writes statusline wrapper + patches settings.json, idempotent
claude-quota backfill [--days N]
claude-quota doctor             # verify OTel is arriving, statusline is posting, rate_limits present
claude-quota daemon [start|stop|status|logs]
```

Install `ccq` as a shorthand alias for the same entry point. `claude-quota now` is the command that gets run most and it should not cost ten keystrokes.

`claude-quota now` output shape. This is the one that gets read most, keep it tight:

```
5h  ############+.......  62%   cutoff 3:58 PM . resets 4:41 PM . 43m SHORT
7d  ######+.............  31%   on pace
    atlas 34% . banking-dash 21% . planner 5% . bsi-intake 2% . other 0%
```

`--json` on every subcommand. Respect `NO_COLOR`. Detect non-TTY and drop to plain output automatically so it pipes cleanly.

`claude-quota doctor` matters more than it looks. Three independent integrations can each silently fail. It should check and report on each one separately with a concrete fix line.

---

## 8. HTTP API

```
POST /ingest/statusline      body: raw statusline JSON, returns 204 immediately
GET  /api/summary            current 5h + 7d, projection, verdict, degraded flag
GET  /api/sessions           live sessions with share, cost, burn, last_active
GET  /api/breakdown?by=model|agent|mcp|skill&window=5h|7d
GET  /api/history?days=14
GET  /api/health             calibration state, source liveness, sample counts
GET  /events                 SSE, pushes {summary, sessions} on change, ~1s max rate
```

The ingest endpoint must return before doing any work. Push onto a queue, process in a background task. It is on the critical path of a status line render.

---

## 9. Web UI

**A working visual reference ships with this spec: `claude-quota-preview.html`.** Open it. Match its structure and information hierarchy. Reproduce the design tokens (the `:root` block), the dark-mode variant, the type treatment, and the hover-to-isolate interaction. Replace the hardcoded fixtures with live data from `/events`.

The signature element is the rail: the 5-hour window drawn as a time axis rather than a progress bar, with the projected cutoff marked against the actual reset. That comparison is the product. Do not downgrade it to a percentage bar with a number next to it.

Required behaviours not visible in the static preview:

- Degraded mode: when `rate_limits` has been absent >10 min, the big percentage gets a hatched fill and the label reads "estimated from local telemetry".
- The `unattributed` residual renders as a distinct grey segment on the rail with a tooltip explaining it is claude.ai and Cowork usage.
- Session colours come from `session.color_idx` so a session keeps its colour across reloads.
- Reconnect SSE with backoff; show a dot state change, not a modal.
- Cap the rail at 100% but keep counting past it, showing "112% capped 14m ago".

---

## 10. What to lift from Claude-Code-Usage-Monitor

Reference: `github.com/Maciek-roboblog/Claude-Code-Usage-Monitor`. Read it before writing the projection module. Worth porting:

- Its burn-rate smoothing and the way it handles the rolling window boundary.
- Its handling of overlapping concurrent sessions within one window.
- Its terminal rendering approach for the live view.

Deliberately different here: it is transcript-driven and estimates the limit; claude-quota reads the real percentage from statusLine and *calibrates against it*. Do not copy its token-threshold-guessing logic, it is solving a problem this design avoids.

---

## 11. Failure modes to handle explicitly

| Condition | Behaviour |
|---|---|
| `rate_limits` absent from payload | Accept ingest, set `had_limits=0`, fall back to estimate after 10 min |
| Daemon down when statusline fires | Wrapper exits 0 silently, status line renders normally |
| OTel arriving, statusline not | Attribution works, quota is estimated, `doctor` flags it loudly |
| statusline arriving, OTel not | Quota is exact, per-session breakdown is unavailable, say so |
| Clock skew between reset and local time | Trust `resets_at` from the payload, never compute reset locally |
| Session ends without notice | Mark idle after 5 min of no activity, drop from live list after 30 min, keep its usage in the window total |
| Two windows overlap | Windows are account-level and defined by `resets_at`; key on it, do not infer from session start |
| Backfill parse error | Warn, skip file, continue |

---

## 12. Milestones

1. **Ingest.** Daemon, SQLite, statusline wrapper, `claude-quota install`, `claude-quota doctor`. Prove both streams land.
2. **Truth.** `/api/summary` + `claude-quota now` showing real 5h/7d from statusline. Useful on its own from here.
3. **Attribution.** OTLP receiver, `usage_bucket`, session leaderboard, `claude-quota sessions`.
4. **Calibration + projection.** The NNLS fit, burn rate, cutoff verdict, confidence range.
5. **Web.** SSE, the rail, the full page per the preview.
6. **History.** `window_history`, backfill importer, trends panel, `claude-quota watch`.

Milestone 2 is genuinely shippable. Do not build past it without running it for a day first: the calibration design depends on assumptions about the two streams that real data will either confirm or wreck.

## 13. Acceptance

- Four concurrent sessions, shares sum to the measured total within the stated residual.
- Killing the daemon mid-session leaves every status line rendering normally.
- Projection stays within +/-10% of the actual cap-hit time across 5 real windows.
- `claude-quota now` returns in under 150ms over SSH.
- Removing `rate_limits` from the ingest payload degrades visibly and does not crash anything.
