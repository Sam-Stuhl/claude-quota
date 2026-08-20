"use strict";

// Live quota dashboard. The status line alone gives us real 5h/7d percentages
// and a percentage history (the sparkline); OpenTelemetry adds per-session
// attribution on top. The UI is designed to be informative from the status
// line alone and to teach how to turn attribution on when it is missing.

const SVGNS = "http://www.w3.org/2000/svg";
const PALETTE = 12;
const $ = (id) => document.getElementById(id);

const color = (i) => `var(--s${((i % PALETTE) + PALETTE) % PALETTE})`;
const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "—";
}
function fmtDayTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : "—";
}
function fmtDur(sec) {
  if (sec == null) return "—";
  sec = Math.abs(Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function fmtTokens(n) {
  if (n == null) return "0";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + "K";
  return String(n);
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function el(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

// ---- Feed status row -------------------------------------------------------

function setFeed(id, state, label) {
  const n = $(id);
  n.className = "feed " + state + (id === "feed-conn" ? " conn" : "");
  if (label !== undefined) {
    const txt = n.querySelector("span") || n.lastChild;
    if (id === "feed-conn") $("conn-text").textContent = label;
  }
}

function renderFeeds(summary) {
  const s = summary.sources || {};
  const sl = s.statusline || {}, ot = s.otel || {};
  setFeed("feed-status", sl.live ? (sl.rate_limits ? "on" : "warn") : "down");
  $("feed-status").childNodes[1].nodeValue = sl.live
    ? (sl.rate_limits ? "status line" : "status line (estimating)")
    : "status line down";
  setFeed("feed-otel", ot.live ? "on" : (ot.ever ? "warn" : "off"));
  $("feed-otel").childNodes[1].nodeValue = ot.live ? "telemetry" : (ot.ever ? "telemetry stalled" : "telemetry off");
  setFeed("feed-calib", summary.calibrated ? "on" : "off");
  $("feed-calib").childNodes[1].nodeValue = summary.calibrated ? "calibrated" : "uncalibrated";
}

// ---- Hero: number, verdict, sparkline, stats -------------------------------

function renderHero(summary) {
  const five = summary.five_hour;
  const proj = summary.projection || {};
  const used = five.used_percentage || 0;

  $("five-pct").textContent = Math.round(used);
  const capNote = $("cap-note");
  if (five.capped && five.capped_since) {
    capNote.hidden = false;
    capNote.textContent = `capped ${fmtDur(summary.now - five.capped_since)} ago`;
  } else if (summary.degraded || five.estimated) {
    capNote.hidden = false;
    capNote.textContent = "estimated";
  } else {
    capNote.hidden = true;
  }

  // Cutoff is the headline: when you hit the cap, and whether that's before or
  // after the window resets.
  const chip = $("verdict"), time = $("cutoff-time"), detail = $("cutoff-detail");
  if (proj.available && proj.cutoff_at) {
    const v = proj.verdict, cls = v === "SHORT" ? "short" : "clear";
    time.textContent = fmtTime(proj.cutoff_at);
    time.className = "cutoff-time " + cls;
    chip.textContent = v;
    chip.className = "verdict " + cls;
    const reset = fmtTime(five.resets_at);
    detail.textContent = v === "SHORT"
      ? `runs out ${fmtDur(proj.margin_seconds)} before the ${reset} reset`
      : `${fmtDur(proj.margin_seconds)} of headroom — resets ${reset} first`;
  } else {
    time.textContent = "—";
    time.className = "cutoff-time muted";
    chip.textContent = "";
    chip.className = "verdict";
    detail.textContent = summary.degraded
      ? "quota estimated from telemetry"
      : (proj.suppressed_reason || "gathering history…");
  }

  drawSpark(summary);
  renderStats(summary);
}

function drawSpark(summary) {
  const svg = $("spark");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const series = summary.series || [];
  const five = summary.five_hour;
  const proj = summary.projection || {};
  const start = summary.window_start;
  const reset = five.resets_at;
  const now = summary.now;
  const end = reset && reset > start ? reset : now;

  if (series.length < 2 || !start) {
    $("chart-empty").hidden = false;
    $("ax-start").textContent = start ? fmtTime(start) : "";
    $("ax-reset").textContent = reset ? fmtTime(reset) : "";
    positionNow(start, end, now);
    return;
  }
  $("chart-empty").hidden = true;

  const W = 1000, H = 100, PAD = 6;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const x = (t) => clamp((t - start) / (end - start), 0, 1) * W;
  const y = (p) => PAD + (1 - clamp(p, 0, 100) / 100) * (H - 2 * PAD);

  // Cap line (100%) and reset marker.
  svg.appendChild(el("line", { x1: 0, y1: y(100), x2: W, y2: y(100),
    stroke: "var(--line-2)", "stroke-width": 1, "stroke-dasharray": "3 4", "vector-effect": "non-scaling-stroke" }));
  if (reset && reset > start) {
    svg.appendChild(el("line", { x1: x(reset), y1: 0, x2: x(reset), y2: H,
      stroke: "var(--accent)", "stroke-width": 1.5, "stroke-dasharray": "2 3", "vector-effect": "non-scaling-stroke" }));
  }

  // Actual line + soft area fill.
  const pts = series.map(([t, p]) => `${x(t).toFixed(1)},${y(p).toFixed(1)}`);
  const last = series[series.length - 1];
  svg.appendChild(el("polygon", {
    points: `${x(series[0][0]).toFixed(1)},${H} ${pts.join(" ")} ${x(last[0]).toFixed(1)},${H}`,
    fill: "var(--accent)", "fill-opacity": 0.10,
  }));
  svg.appendChild(el("polyline", {
    points: pts.join(" "), fill: "none", stroke: "var(--accent)",
    "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke",
  }));

  // Projected continuation to the cap, coloured by verdict.
  if (proj.available && proj.cutoff_at) {
    const col = proj.verdict === "SHORT" ? "var(--short)" : "var(--clear)";
    svg.appendChild(el("line", {
      x1: x(last[0]), y1: y(last[1]), x2: x(proj.cutoff_at), y2: y(100),
      stroke: col, "stroke-width": 1.5, "stroke-dasharray": "4 4", "vector-effect": "non-scaling-stroke",
    }));
    svg.appendChild(el("circle", { cx: x(proj.cutoff_at), cy: y(100), r: 3.2, fill: col }));
  }
  // Now dot on the actual line.
  svg.appendChild(el("circle", { cx: x(last[0]), cy: y(last[1]), r: 3, fill: "var(--accent)" }));

  $("ax-start").textContent = fmtTime(start);
  $("ax-reset").textContent = reset ? fmtTime(reset) : fmtTime(now);
  positionNow(start, end, now);
}

function positionNow(start, end, now) {
  const axNow = $("ax-now");
  if (!start || !end || end <= start) { axNow.style.display = "none"; return; }
  axNow.style.display = "";
  axNow.style.left = `${clamp((now - start) / (end - start), 0, 1) * 100}%`;
}

function renderStats(summary) {
  const five = summary.five_hour, proj = summary.projection || {}, now = summary.now;
  const reset = five.resets_at;
  $("s-reset").textContent = fmtTime(reset);
  $("s-remaining").textContent = reset ? `${fmtDur(reset - now)} left` : "";

  if (proj.available) {
    $("s-burn").textContent = `${proj.burn_rate_pct_per_hour.toFixed(1)} %/hr`;
    $("s-burn-ci").textContent = proj.burn_rate_stderr ? `± ${proj.burn_rate_stderr.toFixed(1)}` : "";
    const hrs = reset ? (reset - now) / 3600 : 0;
    const projPct = (five.used_percentage || 0) + proj.burn_rate_pct_per_hour * hrs;
    $("s-proj").textContent = `${Math.round(clamp(projPct, 0, 999))}%`;
    $("s-proj-sub").textContent = proj.verdict === "SHORT" ? `caps ${fmtTime(proj.cutoff_at)}` : "under the cap";
  } else {
    $("s-burn").textContent = "—";
    $("s-burn-ci").textContent = proj.suppressed_reason || "";
    $("s-proj").textContent = "—";
    $("s-proj-sub").textContent = "";
  }
}

// ---- Attribution -----------------------------------------------------------

function renderAttribution(summary, sessionsData) {
  const body = $("attr-body"), hint = $("attr-hint"), bar = $("attr-bar");
  const rows = (sessionsData && sessionsData.sessions) || [];
  if (rows.length > 0) {
    hint.textContent = "this 5-hour window";

    // Composition bar: session-coloured segments + grey unattributed, summing
    // to the measured window percentage. Attribution at a glance.
    const measured = summary.five_hour.used_percentage || 1;
    const unattr = sessionsData.unattributed_pct || 0;
    const seg = (w, col, sid, title) =>
      `<i style="width:${clamp(100 * w / measured, 0, 100).toFixed(1)}%;background:${col}" data-sid="${sid}" title="${title}"></i>`;
    bar.hidden = false;
    bar.innerHTML =
      rows.map((s) => seg(s.est_pct, color(s.color_idx), esc(s.session_id), `${esc(s.label)} · ${s.est_pct.toFixed(1)}%`)).join("") +
      (unattr > 0 ? seg(unattr, "var(--grey-slice)", "__u__", `unattributed · ${unattr.toFixed(1)}%`) : "");

    const t = ['<table class="attr-table">'];
    for (const s of rows) {
      t.push(`<tr class="attr-row" data-sid="${esc(s.session_id)}">
        <td class="attr-name"><span class="swatch" style="background:${color(s.color_idx)}"></span>${esc(s.label)}${s.idle ? '<span class="badge">idle</span>' : ""}</td>
        <td class="attr-share">${s.share_of_sessions_pct.toFixed(0)}%</td>
        <td class="attr-num">$${s.cost_usd.toFixed(2)}</td>
        <td class="attr-num dim">$${s.burn_usd_per_hour.toFixed(2)}/hr</td>
        <td class="attr-num dim">${fmtTokens(s.tokens)}</td></tr>`);
    }
    if (sessionsData.unattributed_pct > 0) {
      t.push(`<tr class="attr-row" data-sid="__u__">
        <td class="attr-name"><span class="swatch" style="background:var(--grey-slice)"></span><span style="color:var(--ink-3)">unattributed · claude.ai / Cowork</span></td>
        <td class="attr-share">${sessionsData.unattributed_pct.toFixed(0)}%</td>
        <td class="attr-num"></td><td class="attr-num"></td><td class="attr-num"></td></tr>`);
    }
    t.push(`</table>`);
    body.innerHTML = t.join("");
    return;
  }

  // Empty state that teaches how to enable attribution.
  bar.hidden = true;
  hint.textContent = `100% unattributed`;
  const ot = (summary.sources && summary.sources.otel) || {};
  const headline = ot.ever ? "Telemetry has stalled" : "Per-session attribution is off";
  body.innerHTML = `
    <div class="teach">
      <div class="bar-mini"></div>
      <div class="teach-body">
        <h3>${headline}</h3>
        <p>The 5-hour and 7-day numbers are exact (they come from Claude Code's status line). But splitting usage <em>by session</em> needs OpenTelemetry, and Claude Code reads that setting only when a session starts.</p>
        <ol class="steps">
          <li>Confirm setup: <code>claude-quota doctor</code></li>
          <li>Restart your Claude Code sessions (or start new ones)</li>
        </ol>
      </div>
    </div>`;
}

// ---- Side panels -----------------------------------------------------------

function renderSeven(summary) {
  const s = summary.seven_day;
  if (s.used_percentage == null) {
    $("seven-pct").textContent = "—";
    $("seven-fill").style.width = "0%";
    $("seven-reset").textContent = "no 7-day data yet";
    return;
  }
  $("seven-pct").textContent = Math.round(s.used_percentage);
  $("seven-fill").style.width = `${clamp(s.used_percentage, 0, 100)}%`;
  $("seven-reset").textContent = s.resets_at ? `resets ${fmtDayTime(s.resets_at)}` : "";
}

function renderModels(data) {
  const items = (data.items || []).filter((i) => i.cost_usd > 0);
  const panel = $("models-panel");
  if (!items.length) { panel.hidden = true; return; }
  panel.hidden = false;
  const max = Math.max(...items.map((i) => i.cost_usd));
  $("models-body").innerHTML = items.slice(0, 6).map((i) => `
    <div class="mrow">
      <span class="mname">${esc(i.key)}</span>
      <span class="mcost">$${i.cost_usd.toFixed(2)}</span>
      <span class="track"><i style="width:${(100 * i.cost_usd / max).toFixed(0)}%"></i></span>
    </div>`).join("");
}

function renderHistory(data) {
  const w = data.windows || [];
  const panel = $("history-panel");
  if (!w.length) { panel.hidden = true; return; }
  panel.hidden = false;
  const items = w.slice(0, 24).reverse();
  $("history-body").innerHTML = items.map((x) => {
    const h = clamp(x.peak_pct, 2, 100);
    return `<div class="hbar ${x.hit_cap ? "cap" : ""}" style="height:${h}%" title="${fmtDayTime(x.reset_at)} · peak ${x.peak_pct.toFixed(0)}%${x.hit_cap ? " · hit cap" : ""}"></div>`;
  }).join("");
}

// ---- Hover-to-isolate ------------------------------------------------------

document.body.addEventListener("mouseover", (e) => {
  const r = e.target.closest("[data-sid]");
  if (!r) return;
  document.querySelectorAll("[data-sid]").forEach((n) => n.classList.toggle("is-dimmed", n.dataset.sid !== r.dataset.sid));
});
document.body.addEventListener("mouseout", (e) => {
  if (e.target.closest("[data-sid]")) document.querySelectorAll(".is-dimmed").forEach((n) => n.classList.remove("is-dimmed"));
});

// ---- Data flow -------------------------------------------------------------

function render(data) {
  const { summary, sessions } = data;
  renderFeeds(summary);
  renderHero(summary);
  renderAttribution(summary, sessions);
  renderSeven(summary);
  const cost = (sessions.sessions || []).reduce((a, s) => a + s.cost_usd, 0);
  const toks = (sessions.sessions || []).reduce((a, s) => a + (s.tokens || 0), 0);
  if (summary.has_attribution) {
    $("s-cost").textContent = `$${cost.toFixed(2)}`;
    $("s-tokens").textContent = `${fmtTokens(toks)} tokens`;
  } else {
    $("s-cost").textContent = "—";
    $("s-tokens").textContent = "needs telemetry";
  }
  $("foot-updated").textContent = "updated " + fmtTime(summary.now);
  $("foot-window").textContent = summary.window_start
    ? `window ${fmtTime(summary.window_start)} → ${fmtTime(summary.five_hour.resets_at)}` : "";
}

async function pollPanels() {
  try {
    const [m, h] = await Promise.all([
      fetch("/api/breakdown?by=model&window=5h").then((r) => r.json()),
      fetch("/api/history?days=14").then((r) => r.json()),
    ]);
    renderModels(m);
    renderHistory(h);
  } catch (_) { /* daemon momentarily unreachable */ }
}

function connect() {
  const es = new EventSource("/events");
  es.onopen = () => setFeed("feed-conn", "on", "live");
  es.onmessage = (ev) => {
    setFeed("feed-conn", "on", "live");
    try { render(JSON.parse(ev.data)); } catch (_) { /* ignore a bad frame */ }
  };
  es.onerror = () => setFeed("feed-conn", "down", "reconnecting");
}

setFeed("feed-conn", "off", "connecting");
connect();
pollPanels();
setInterval(pollPanels, 12000);
