# claude-quota dashboard — redesign handoff

Implementation spec for the mockup in `Quota dashboard.dc.html` (6 artboards:
`1a` desktop SHORT dark / `1b` light / `1c` CLEAR / `1d` attribution-off ·
projection collecting / `1e` degraded + capped / `1f` mobile).

Target files: `src/claude_quota/web/{index.html,style.css,app.js}`. The data
contract does not change — everything below is rendered from the existing
`/events` SSE frame plus `/api/breakdown` and `/api/history`.

---

## 1. Design tokens

Replace the `:root` block. Same variable *names* as today where possible.

```css
:root {                              /* light */
  --bg:#f4f5f7; --panel:#ffffff; --panel2:#f7f8fa;
  --ink:#0e1116; --ink2:#4a5361; --ink3:#7b8492;
  --line:#e3e6ea; --line2:#d3d8df;
  --accent:#3b5bdb; --accent-soft:#eaeefc;
  --short:#c9352e; --short-soft:#fbe6e4;
  --clear:#12854a; --clear-soft:#ddf0e5;
  --warn:#a9761a;  --warn-soft:#fbf1dc;
  --grey:#9aa3af;  --grey-soft:#eef0f3;      /* unattributed + empty track */
  --font:'Space Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0b0d11; --panel:#101318; --panel2:#161a21;
    --ink:#e8ecf1; --ink2:#9aa3b0; --ink3:#646d7a;
    --line:#1e232c; --line2:#2a313c;
    --accent:#7c8cff; --accent-soft:#1a2036;
    --short:#ff6b5f; --short-soft:#31191c;
    --clear:#3ecf82; --clear-soft:#12301f;
    --warn:#e0a83a;  --warn-soft:#2e2411;
    --grey:#4e5763;  --grey-soft:#191d24;
  }
}
```

Session palette (keyed to `color_idx`, stable across renders). Slot 2 is teal,
not green, so no session can be mistaken for the CLEAR semantic:

| idx | light | dark |
|---|---|---|
| 0 | `#3b5bdb` | `#7c8cff` |
| 1 | `#c65a12` | `#f0873c` |
| 2 | `#0d8f83` | `#2dd4bf` |
| 3 | `#6d4de0` | `#a78bfa` |
| 4 | `#a8452f` | `#fb9d6a` |
| 5 | `#0e7490` | `#38bdf8` |
| 6 | `#b3149a` | `#e879cf` |
| 7 | `#8a6a08` | `#d9bb4a` |
| 8 | `#1f6fa8` | `#5eb0e8` |
| 9 | `#d43e72` | `#f4728f` |
| 10 | `#4d7a17` | `#a3c853` |
| 11 | `#6455d6` | `#9b8cf7` |

Type: two families only. `--font` for labels and prose; `--mono` for **every
number, time, id, device and model name**. Base 13px / 1.4. Tile labels 10.5px
uppercase, `letter-spacing:.07em`, `--ink3`. Big KPI numerals 30px/600 mono.
Nothing on the desktop board is larger than 30px — this is an instrument, not a
poster.

Chrome: hairline panes, not cards. One outer container with
`background:var(--line)` and `gap:1px` between `background:var(--panel)`
children produces every divider. Radius 10px on the outer shell only.

---

## 2. Layout

```
┌ header ─ brand · window pill ······················ feed pills · live ┐
├ slicer bar ─ scope [this window|5h|7d|14d] · device · model · agent ··
│              ················· active filter chips · clear all       │
├ KPI row (5) ─ VERDICT | CUTOFF | RESET | BURN | USED ────────────────┤
├ Projection (1.62fr) ─────────────────┬ Composition ring (1fr) ───────┤
│  burn line to 100% + ±1σ cone        │  ring of the whole 100 pts    │
│  runway bar (usable | capped)        │  legend incl. unattributed    │
├ Attribution (full width) ────────────┴───────────────────────────────┤
│  group-by [session|device|model|agent] · composition bar · table     │
│  · unattributed row · residual callout                               │
├ EXPLORE ─────────────────────────────────────────────────────────────┤
│  Recent windows (1.6fr) │ By device (1fr)                            │
│  Spend map │ By model │ Calibration & feeds                          │
└ footer ─ updated · streaming /events ················ localhost:7789 ┘
```

Breakpoints: `>1200px` as above · `900–1200px` KPI row wraps to 3+2, projection
and composition stack · `<720px` artboard `1f`: single column, KPI pairs as a
2-up, chart 110px tall, table becomes stacked rows (name + share on line one,
pts/$/hr and device/idle as a dim mono sub-line).

---

## 3. Panels, field by field

### KPI row
| tile | value | sub | source |
|---|---|---|---|
| VERDICT | chip `SHORT`/`CLEAR`/`CAPPED`/dashed `NOT YET` | "caps 28m before reset" | `projection.verdict`, `margin_seconds` |
| PROJECTED CUTOFF | `19:10` in `--short`/`--ink2` | `18:55 – 19:32 ±1σ` | `cutoff_at`, `ci_low_cutoff`, `ci_high_cutoff` |
| WINDOW RESET | `19:38` | `in 2h 23m` | `five_hour.resets_at` |
| BURN RATE | `14.7 %/hr` | `± 1.9 · fit over last 40m` | `burn_rate_pct_per_hour`, `burn_rate_stderr` |
| QUOTA USED | `71.9%` | `exact · status line · 28.1 pts left` | `five_hour.used_percentage`, `.estimated` |

Cutoff is coloured; usage is `--ink2` — deliberately the quieter number.

### Projection chart
X axis = oldest retained sample → reset (or reset + slack when CLEAR pushes the
cutoff past it). Y = 0…110% (0…120% when capped). Draw:
- solid `--ink` polyline over the measured `series`;
- dashed `--short`/`--clear` line from `now` to 100%, flat afterwards;
- `±1σ` cone as a `--short`/`--clear` polygon at 0.16 opacity between the
  `stderr`-fast and `stderr`-slow lines;
- vertical rule at cutoff (`--short`) and at reset (`--accent`), dotted rule at
  `now`;
- tinted `--short-soft` rect over the capped span.

Toggle `[burn line | by session]` swaps the polyline for a stacked area of the
same window keyed by session colour.

### Runway bar
26px, `now → resets_at`. SHORT: `--clear-soft` usable segment (width =
`(cutoff−now)/(reset−now)`) + `--short-soft` capped remainder, each with a 2px
left border in the solid semantic and an inline mono label. CLEAR: one full
usable segment. Suppressed: one neutral `--grey-soft` bar reading
"2h 23m left in the window · usable / capped split unknown". Capped: one full
`--short-soft` bar.

### Composition ring
The ring is the **whole 100 points**, not just what was used: session arcs,
then the grey unattributed arc, then `--grey-soft` for unused. Centre label =
`used_percentage`. Legend lists every session in points, then unattributed,
then unused.

### Attribution table
Columns: `session | share of window (bar + %) | pts of 100 | $/hr | tokens |
device | last active`. Grid `210px 1fr 78px 76px 86px 104px 92px`.

- Share % is share of the **measured window**, i.e. `est_pct / used_percentage`
  — so the five sessions plus unattributed sum to 100%, never 100% of only the
  attributed part.
- The in-row bar is normalised to the largest row (ranked-bar reading).
- `pts of 100` is `est_pct` verbatim — the honest quota unit.
- Unattributed is a real row: grey swatch, `--grey-soft` row background, share
  and points filled, `$/hr`, tokens, device, last-active all `—`.
- Below the table, the residual callout (always rendered when `> 0`):
  "**29.7 pts (41% of everything spent)** is measured quota that local
  telemetry can't explain — claude.ai chat, Cowork, or a machine that isn't
  reporting…"

Group-by switch re-aggregates the same rows from `/api/breakdown?by=…`; the
unattributed row survives every grouping.

### Explore zone
- **Recent windows** — `/api/history`, column per window, height = `peak_pct`,
  `hit_cap` bars solid `--short`, current window outlined dashed `--accent`.
- **By device** — 7d spend bars, cost + share + tokens per device. No sparkline:
  there is no per-device series in the API, so don't draw one.
- **Spend map** — treemap of session cost this window (`cost_usd`), footnote
  "local cost only — the unattributed N pts has no cost data".
- **By model** — single split bar in accent tints + rows (%, `$`).
- **Calibration & feeds** — `pct_per_usd` per model, `r²`, `n`, status samples,
  otel buckets, uptime.

---

## 4. Interaction

- **Hover a session row** → every other session's row, ring arc, composition bar
  segment and treemap cell drop to 0.22 opacity. Keyed on `data-sid`, exactly
  the mechanism already in `app.js`.
- **Click a row** → sticky cross-filter: adds a filter chip in the slicer bar
  and re-scopes every panel; click again or use "clear all" to release.
- **Slicers** — scope (`this window` / `5h` / `7d` / `14d`), device, model,
  agent. Active = `--accent` border + `--accent-soft` fill.
- Respect `prefers-reduced-motion`; transitions are 150ms opacity / 500ms width
  only.

---

## 5. Honesty rules (these are the point of the redesign)

1. **No verdict without a projection.** `projection.available === false` →
   dashed `NOT YET` chip, cutoff `—`, burn `—`, and the suppression reason shown
   verbatim with progress when it's `collecting data` (a 4px accent progress bar,
   `6m / 10m`). Never colour the frame red or green in this state.
2. **Never draw a projected line when the fit is suppressed.** The chart keeps
   its axes and the measured polyline, and states in words why nothing is drawn.
3. **Estimated ≠ measured.** When `degraded || five_hour.estimated`: a
   `--warn` banner naming the reason and its age, an `est` tag on the tile
   label, and a dashed underline under every estimated numeral. While estimated,
   the attribution panel shows **shares only** — points of quota are withheld,
   because the denominator is itself a guess.
4. **Capped keeps counting.** `>100%` renders as `112%` with
   `capped 14m ago`, the chart Y axis extends to 120% with the over-cap band
   tinted, and the runway is fully red.
5. **The residual is never hidden.** If `unattributed_pct > 0` it is a row, a
   ring arc, a bar segment and a sentence. If attribution is off entirely, the
   panel teaches (`claude-quota doctor`, restart sessions) *and* lists what is
   still exact — 5h %, 7d %, reset time — so the screen never reads as broken.
6. **No invented series.** If the API has no per-device or per-session history,
   don't draw a sparkline for it.

---

## 6. Copy

Verdict line SHORT: `caps {margin} before reset` · CLEAR: `resets {margin}
before the cap` · capped: `no headroom until {reset}`.
Suppressed: `a verdict needs 10m of history`, `not enough signal (too noisy)`,
`compaction spike, projection paused`, `flat or falling` — pass the daemon's
reason through, don't paraphrase it in the UI.
