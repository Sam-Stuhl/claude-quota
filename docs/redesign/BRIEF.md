# claude-quota dashboard — redesign brief

Feed this file (plus [`sample-data.json`](sample-data.json) and a screenshot or
two of the current dashboard at `http://localhost:7789/`) to Claude Design. It
is a mockup canvas and cannot reach the live daemon, so this is the content and
meaning to design against.

## The one question

> Do I have enough headroom left in this 5-hour window to start the thing I'm
> about to start, and which session is eating it?

Everything else is secondary to that. **The two elements that matter most are
the projected cutoff and the per-session attribution** — lead with them.

## What the data means

Sourced live from Claude Code on the machine(s). Two streams:

- **Status line** → the exact account-wide **5-hour and 7-day quota %** and
  their reset times. Also per-render state: model, cumulative cost, context-
  window %, lines changed.
- **OpenTelemetry** → **per-session attribution**: cost/tokens by session,
  model, agent, skill, MCP server, plugin, effort, and **device**.

Key derived values:

- **Projected cutoff** — when, at the current burn rate, you'll hit 100%.
- **Verdict** — `SHORT` (you'll cap before the window resets) or `CLEAR` (the
  window resets first, with headroom). This is the headline answer.
- **Burn rate** — %/hour, with a ± standard error (never a bare point estimate).
- **Unattributed residual** — the part of the measured quota that local cost
  can't explain (claude.ai chat, Cowork, other devices). Real usage, shown as a
  distinct grey slice; hiding it would make per-session shares lie.

## States to design for (each element)

The design must handle every state honestly, not just the happy path:

- **Projection**: available (SHORT / CLEAR, with margin + cutoff time) · or
  suppressed with a reason ("collecting data (6m of 10m)", "not enough signal
  (too noisy)", "compaction spike, projection paused", "flat or falling"). When
  suppressed, show the state, never a fake verdict.
- **Attribution**: populated (session rows + composition + unattributed) · or
  empty/off — telemetry isn't arriving, so 100% is unattributed and the panel
  should teach how to turn it on (restart sessions), not just show "nothing".
- **Quota number**: exact (from the status line) · or `estimated` (degraded
  mode: `rate_limits` absent > 10 min, driven by local telemetry — must be
  visibly marked as an estimate) · or `capped` (>100%, keep counting: "112% —
  capped 14m ago").
- **Calibration**: calibrated · or uncalibrated (shares fall back to raw cost).
- **Feeds**: status line / telemetry / calibration each live · stalled · off.
- **Multi-device**: sessions can carry a `device`; the dashboard should be able
  to separate machines.
- **Windows roll over** every 5 hours: usage % and the runway reset; the closed
  window drops into history.

## Current layout (what exists, to react to)

Top: feed-status pills + live dot. Hero: the projected **cutoff** (large,
verdict-coloured) with usage % as a supporting stat, then a **runway** timeline
(now → reset) split into green "usable" and red "capped", then a 4-up stat strip
(reset, burn, projected-at-reset, cost). Below: **attribution** (composition bar
+ table), a 7-day meter, a **by-model split bar**, and a **recent-windows** bar
chart. It's functional but visually flat; that's the thing to elevate.

## Constraints

- Product/tool register: earned familiarity, information density, restraint.
- Works in **light and dark**, responsive down to mobile.
- One accent colour; semantic red/green for SHORT/CLEAR; grey for unattributed;
  a stable per-session palette (colours keyed to `color_idx`).
- Honest by construction: never present an estimate as truth, never invent a
  projection, always surface the unattributed residual.

## Example data

[`sample-data.json`](sample-data.json) holds real responses from every endpoint
(`/api/summary`, `/api/sessions`, `/api/breakdown` by model/device/agent,
`/api/history`, `/api/health`) for a live SHORT window with ~30% unattributed,
5 active sessions across 3 devices, and ~2 weeks of window history. Design
against these actual shapes and values.

## After the mockup

Claude Design produces the *look* (static artboards), not the wired app. Once
you have a direction you like, it gets translated back into the live
`src/claude_quota/web/` (HTML/CSS/JS on the SSE feed). Keep that handoff in mind:
favor structures that map to real, streamable data.
