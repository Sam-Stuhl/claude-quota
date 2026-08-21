# Redesign handoff — claude-quota dashboard

Drop this folder into the repo at `docs/redesign/handoff/`, then paste the
prompt below into Claude Code from the repo root.

## Contents

- `REDESIGN-HANDOFF.md` — the implementation spec: tokens, layout, per-panel
  field mapping to the existing API, interaction model, and the honesty rules.
- `mockup-new-dashboard.dc.html` — the design. Six artboards, ids `1a`–`1f`:
  `1a` desktop SHORT (primary, dark, full report) · `1b` same top zone in light
  · `1c` CLEAR · `1d` attribution off / projection collecting · `1e` degraded +
  capped at 112% · `1f` mobile. Open it in a browser; hover a session row on
  `1a` to see cross-isolation.
- `mockup-current-dashboard.dc.html` — today's UI rendered against
  `sample-data.json`, for before/after comparison.
- `support.js` — runtime the two mockup files load. Keep it next to them.

The mockups are static design artboards, not wired app code. Numbers in them
come from `sample-data.json`; the CLEAR and degraded boards use values derived
from the same shapes (same sessions, same calibration, consistent burn
arithmetic) because that file only contains one window.

## Prompt for Claude Code

> Read `docs/redesign/handoff/REDESIGN-HANDOFF.md` and open
> `docs/redesign/handoff/mockup-new-dashboard.dc.html` in a browser (six
> artboards, ids `1a`–`1f`; `1a` is the primary desktop screen). Also read
> `docs/redesign/BRIEF.md` for intent.
>
> Rebuild `src/claude_quota/web/{index.html,style.css,app.js}` to match. Keep
> the current data flow exactly as-is: SSE `/events` plus `/api/breakdown` and
> `/api/history` polling. No build step, no framework, no dependencies — same
> vanilla HTML/CSS/JS.
>
> Order of work: (1) tokens + hairline-pane shell + header/slicer bar; (2) KPI
> row and the projection chart with the ±1σ cone and runway bar; (3)
> attribution panel with group-by, the unattributed row and residual callout;
> (4) Explore zone; (5) the state machine in §5 of the handoff.
>
> Section 5 is non-negotiable — no verdict when `projection.available` is
> false, no projected line when the fit is suppressed, estimates dashed and
> tagged with points-of-quota withheld, `>100%` keeps counting, and the
> unattributed residual always visible. Where the API has no series for
> something, render the honest empty/absent state rather than inventing one.
>
> Charts are hand-written inline SVG sized in a viewBox — no chart library.
> Verify against light and dark and at 390px wide, then run the test suite.
