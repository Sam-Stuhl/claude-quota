"use strict";

// claude-quota dashboard. Rendered entirely from the existing /events SSE frame
// ({summary, sessions}) plus /api/breakdown and /api/history polling. No build
// step, no framework. Charts are hand-written inline SVG. The honesty rules in
// the redesign handoff (no verdict without a projection, no projected line when
// suppressed, estimates dashed, capped keeps counting, residual always shown)
// are enforced in the render branches below.

const SVGNS = "http://www.w3.org/2000/svg";
const PAL = 12;
const $ = (id) => document.getElementById(id);
const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
const color = (i) => `var(--s${(((i | 0) % PAL) + PAL) % PAL})`;

// ---- formatting ------------------------------------------------------------
const _d = (ts) => new Date(ts * 1000);
function hm(ts) {
  if (!ts) return "—";
  const d = _d(ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function hmDay(ts, ref) {
  // Adds "+1d" when ts falls on a later calendar day than ref.
  let s = hm(ts);
  if (ref && _d(ts).getDate() !== _d(ref).getDate() && ts > ref) s += " +1d";
  return s;
}
function dur(sec) {
  if (sec == null) return "—";
  sec = Math.abs(Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function tok(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "K";
  return String(n);
}
function money(n) { return "$" + (n || 0).toFixed(2); }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function shortId(sid) { return String(sid || "").replace(/[^a-z0-9]/gi, "").slice(-4); }
function el(t, a, kids) {
  const n = document.createElementNS(SVGNS, t);
  for (const k in a) if (a[k] != null) n.setAttribute(k, a[k]);
  if (kids) n.textContent = kids;
  return n;
}

// ---- state -----------------------------------------------------------------
const D = { summary: null, sessions: null, byModel: null, byDevice: null, byGroup: null, history: null };
const UI = { scope: "this", isolate: null, hover: null, groupBy: "session", projMode: "burn", compMode: "ring" };

const SCOPE_WINDOW = { this: "5h", "5h": "5h", "7d": "7d", "14d": "7d" };

// ---------------------------------------------------------------------------
// Feeds + header
// ---------------------------------------------------------------------------
function renderHeader(s) {
  const src = s.sources || {}, sl = src.statusline || {}, ot = src.otel || {};
  $("window-pill").textContent = s.window_start
    ? `window ${hm(s.window_start)} → ${hm(s.five_hour.resets_at)} · 5h`
    : "window …";

  const feeds = [];
  if (s.degraded) feeds.push(["warn", "status line · no rate limits"]);
  else feeds.push([sl.live ? "on" : "down", sl.live ? "status line" : "status line down"]);
  feeds.push([ot.live ? "on" : "off", ot.live ? "telemetry" : (ot.ever ? "telemetry stalled" : "telemetry off")]);
  feeds.push([s.calibrated ? "on" : "off", s.calibrated ? "calibrated" : "uncalibrated"]);
  feeds.push(["live", _live ? "live" : "reconnecting"]);
  $("feeds").innerHTML = feeds
    .map(([c, t]) => `<span class="feed ${c}"><i></i>${esc(t)}</span>`)
    .join("");
}

// ---------------------------------------------------------------------------
// Slicer bar
// ---------------------------------------------------------------------------
function renderSlicer(s) {
  const scopes = ["this", "5h", "7d", "14d"];
  const labels = { this: "this window", "5h": "5h", "7d": "7d", "14d": "14d" };
  const devices = [...new Set((s.sessions || []).map((x) => x.device).filter(Boolean))];

  let h = `<div class="grp"><span class="eyebrow">scope</span><div class="seg" id="scope-seg">`;
  h += scopes.map((k) => `<button data-scope="${k}" class="${UI.scope === k ? "on" : ""}">${labels[k]}</button>`).join("");
  h += `</div></div>`;

  if (devices.length) {
    h += `<div class="sep"></div><div class="grp"><span class="eyebrow">device</span><div class="chipset" id="device-chips">`;
    h += `<button class="chip ${!UI.isolate || UI.isolate.type !== "device" ? "on" : ""}" data-device="">all</button>`;
    h += devices.map((d) => `<button class="chip mono ${UI.isolate && UI.isolate.type === "device" && UI.isolate.key === d ? "on" : ""}" data-device="${esc(d)}">${esc(d)}</button>`).join("");
    h += `</div></div>`;
  }

  h += `<div class="filters" id="filters">`;
  if (UI.isolate) {
    h += `<span class="fchip">${esc(UI.isolate.type)} = ${esc(UI.isolate.label || UI.isolate.key)} <button data-clear="1">✕</button></span>`;
    h += `<button class="clear-all" data-clear="1">clear</button>`;
  } else {
    h += `<span class="hint">hover a row to isolate · click to hold</span>`;
  }
  h += `</div>`;
  $("slicer").innerHTML = h;

  $("scope-seg").querySelectorAll("button").forEach((b) =>
    (b.onclick = () => { UI.scope = b.dataset.scope; pollPanels(); render(); }));
  const dc = $("device-chips");
  if (dc) dc.querySelectorAll("button").forEach((b) => (b.onclick = () => {
    UI.isolate = b.dataset.device ? { type: "device", key: b.dataset.device } : null;
    render();
  }));
  $("filters").querySelectorAll("[data-clear]").forEach((b) => (b.onclick = () => { UI.isolate = null; render(); }));
}

// isolate/hover opacity for a session id
function dimSid(sid, device) {
  const iso = UI.hover || UI.isolate;
  if (!iso) return false;
  if (iso.type === "session") return iso.key !== sid;
  if (iso.type === "device") return iso.key !== device;
  return false;
}

// ---------------------------------------------------------------------------
// KPI row
// ---------------------------------------------------------------------------
function collectingProgress(reason) {
  const m = /(\d+)\s*m\s*(?:of|\/)\s*(\d+)\s*m/i.exec(reason || "");
  return m ? { a: +m[1], b: +m[2] } : null;
}

function renderKpi(s) {
  const five = s.five_hour, p = s.projection || {}, now = s.now;
  const used = five.used_percentage || 0;
  const capped = !!five.capped;
  const est = !!(s.degraded || five.estimated);
  const reset = five.resets_at;
  const tiles = [];

  // VERDICT
  if (capped) {
    tiles.push(tile("verdict",
      `<span class="verdict capped">CAPPED</span>`,
      `no headroom until <b class="mono">${hm(reset)}</b>`));
  } else if (p.available && p.verdict) {
    const cls = p.verdict.toLowerCase();
    const sub = p.verdict === "SHORT"
      ? `caps <b class="mono" style="color:var(--short)">${dur(p.margin_seconds)}</b> before reset`
      : `resets <b class="mono" style="color:var(--clear)">${dur(p.margin_seconds)}</b> before the cap`;
    tiles.push(tile("verdict", `<span class="verdict ${cls}">${p.verdict}</span>`, sub));
  } else {
    tiles.push(tile("verdict", `<span class="verdict notyet">NOT YET</span>`,
      `<span style="color:var(--ink3)">${esc(p.suppressed_reason || "gathering history")}</span>`));
  }

  // CUTOFF (or cap-reached when capped)
  if (capped) {
    tiles.push(tile("cap reached",
      `<span class="num short ${est ? "est-under" : ""}">${hm(five.capped_since)}</span>`,
      `<span class="mono">${five.capped_since ? dur(now - five.capped_since) + " ago" : "—"}${est ? " · estimated" : ""}</span>`));
  } else if (p.available && p.cutoff_at) {
    const cls = p.verdict === "SHORT" ? "short" : "ink2";
    tiles.push(tile("projected cutoff",
      `<span class="num ${cls}">${hmDay(p.cutoff_at, now)}</span>`,
      `<span class="mono">${hm(p.ci_low_cutoff)} – ${hm(p.ci_high_cutoff)}</span> <span>±1σ${p.verdict === "CLEAR" ? " · after reset" : ""}</span>`));
  } else {
    const prog = collectingProgress(p.suppressed_reason);
    const sub = prog
      ? `<span class="progress"><span class="track"><i style="width:${clamp(100 * prog.a / prog.b, 0, 100)}%"></i></span><span class="mono">${prog.a}m / ${prog.b}m</span></span>`
      : `<span>${esc(p.suppressed_reason || "collecting")}</span>`;
    tiles.push(tile("projected cutoff", `<span class="num dim">—</span>`, sub));
  }

  // WINDOW RESET
  tiles.push(tile("window reset",
    `<span class="num ${p.verdict === "CLEAR" && !capped ? "clear" : ""}">${hm(reset)}</span>`,
    `<span class="mono">${reset ? "in " + dur(reset - now) : "—"}${p.verdict === "CLEAR" ? " · arrives first" : ""}</span>`));

  // BURN RATE
  if (p.available) {
    tiles.push(tile("burn rate",
      `<span class="num">${p.burn_rate_pct_per_hour.toFixed(1)}<span class="u">%/hr</span></span>`,
      `<span class="mono">± ${(p.burn_rate_stderr || 0).toFixed(1)} · fit over last 20m</span>`));
  } else {
    tiles.push(tile("burn rate", `<span class="num dim">—</span>`,
      `<span>${capped ? "projection paused — already at the cap" : esc(p.suppressed_reason || "collecting data")}</span>`));
  }

  // QUOTA USED
  const usedLab = est ? `quota used <span class="esttag">est</span>` : "quota used";
  const usedNum = `<span class="num ${capped ? "short" : "ink2"} ${est ? "est-under" : ""}">${used.toFixed(used >= 100 ? 0 : 1)}<span class="u" ${capped ? 'style="color:inherit"' : ""}>%</span></span>`;
  const usedSub = capped
    ? `<span style="color:var(--short)">capped ${five.capped_since ? dur(now - five.capped_since) + " ago" : ""} · still counting</span>`
    : est
      ? `<span style="color:var(--warn)">estimated from telemetry</span>`
      : `<span>exact · status line · <span class="mono">${(100 - used).toFixed(1)}</span> pts left</span>`;
  tiles.push(tile(usedLab, usedNum, usedSub));

  $("kpi").innerHTML = tiles.join("");
}
function tile(lab, big, sub) {
  return `<div class="tile"><span class="lab">${lab}</span>${big}<span class="sub">${sub}</span></div>`;
}

// ---------------------------------------------------------------------------
// Projection chart
// ---------------------------------------------------------------------------
function renderProjection(s) {
  const five = s.five_hour, p = s.projection || {}, now = s.now, reset = five.resets_at;
  const used = five.used_percentage || 0;
  const capped = !!five.capped;
  const est = !!(s.degraded || five.estimated);
  const series = (s.series || []).slice();
  const clearV = p.verdict === "CLEAR";

  const suppressed = !p.available && !capped;
  let headRight;
  if (capped) headRight = `<span style="color:var(--warn)">dashed line = estimate, no measured samples</span>`;
  else if (suppressed) headRight = `<span style="color:var(--warn);display:inline-flex;align-items:center;gap:6px"><i style="width:6px;height:6px;border-radius:50%;background:var(--warn);display:inline-block"></i>suppressed — ${esc(p.suppressed_reason || "collecting")}</span>`;
  else if (clearV) headRight = `<span>projected <b class="mono" style="color:var(--ink2)">${projAtReset(s).toFixed(1)}%</b> at reset — ${(100 - projAtReset(s)).toFixed(1)} pts spare</span>`;
  else headRight = `<span>shaded cone = ±1σ on burn rate</span>`;

  const title = capped ? "Estimated usage · past the cap" : "Projection to 100%";

  const html = `<div class="phead"><div class="phead-l"><span class="ptitle">${title}</span></div>${headRight}</div>
    <svg viewBox="0 0 700 220" class="chart" style="height:214px"></svg>
    <div class="runway-head"><span>runway · now → reset</span><span class="r">${reset ? dur(reset - now) + " remaining" : "—"}</span></div>
    <div id="runway"></div>`;
  const panel = $("projection-panel");
  panel.innerHTML = html;
  drawProjectionSVG(panel.querySelector("svg"), { s, five, p, now, reset, used, capped, est, series, suppressed, clearV });
  drawRunway(s);
}
function projAtReset(s) {
  const p = s.projection, now = s.now, reset = s.five_hour.resets_at;
  return clamp((s.five_hour.used_percentage || 0) + p.burn_rate_pct_per_hour * ((reset - now) / 3600), 0, 999);
}

function drawProjectionSVG(svg, c) {
  const { five, p, now, reset, used, capped, series, suppressed, clearV } = c;
  const PL = 46, PR = 690, PT = 12, PB = 186;
  const yMax = capped ? 120 : 110, cap100 = capped;
  // Linear map: 0% -> bottom, yMax% -> top.
  const Y = (pct) => PB - clamp(pct, 0, yMax) / yMax * (PB - PT);
  const y100 = Y(100);

  const xStart = series.length ? series[0][0] : (five.resets_at - 5 * 3600);
  let xEnd = reset;
  if (clearV && p.cutoff_at && p.cutoff_at > reset) {
    xEnd = reset + Math.min(p.cutoff_at - reset, (reset - now) * 1.4);
  }
  xEnd = Math.max(xEnd, now + 60);
  const X = (t) => PL + clamp((t - xStart) / (xEnd - xStart), 0, 1) * (PR - PL);

  const frag = document.createDocumentFragment();
  // capped over-band
  if (cap100) frag.appendChild(el("rect", { x: PL, y: PT, width: PR - PL, height: y100 - PT, fill: "var(--shortsoft)" }));
  // CLEAR fresh-window band from reset to end
  if (clearV) frag.appendChild(el("rect", { x: X(reset), y: PT, width: PR - X(reset), height: PB - PT, fill: "var(--clearsoft)", opacity: "0.35" }));
  // SHORT capped span band cutoff->reset
  if (p.available && !clearV && !capped && p.cutoff_at) frag.appendChild(el("rect", { x: X(p.cutoff_at), y: PT, width: X(reset) - X(p.cutoff_at), height: PB - PT, fill: "var(--shortsoft)" }));

  // gridlines + labels
  for (const g of (cap100 ? [120, 100, 50, 0] : [100, 50, 0])) {
    frag.appendChild(el("line", { x1: PL, y1: Y(g), x2: PR, y2: Y(g), stroke: g === 100 ? (cap100 ? "var(--short)" : "var(--line2)") : (g === 50 ? "var(--line)" : "var(--line2)"), "stroke-width": 1 }));
    frag.appendChild(el("text", { x: PL - 6, y: Y(g) + 3, "text-anchor": "end", fill: g === 100 && cap100 ? "var(--short)" : "var(--ink3)", "font-family": "JetBrains Mono, monospace", "font-size": 10 }, g + "%"));
  }

  // ±1σ cone
  if (p.available && !capped && p.cutoff_at && p.ci_low_cutoff && p.ci_high_cutoff) {
    const nx = X(now), ny = Y(used);
    const pts = `${nx},${ny} ${X(p.ci_low_cutoff)},${y100} ${X(p.ci_high_cutoff)},${y100}`;
    frag.appendChild(el("polygon", { points: pts, fill: clearV ? "var(--clear)" : "var(--short)", opacity: "0.16" }));
  }

  // measured polyline
  if (series.length >= 2) {
    const pts = series.map(([t, v]) => `${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
    frag.appendChild(el("polyline", { points: pts, fill: "none", stroke: capped ? "var(--short)" : "var(--ink)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-dasharray": capped ? "6 4" : null }));
  }
  const lastV = series.length ? series[series.length - 1][1] : used;
  const lastT = series.length ? series[series.length - 1][0] : now;

  // projected line
  if (p.available && !capped && p.cutoff_at) {
    const stroke = clearV ? "var(--clear)" : "var(--short)";
    const cx = X(Math.min(p.cutoff_at, xEnd));
    frag.appendChild(el("polyline", { points: `${X(now)},${Y(used)} ${cx},${y100}${clearV ? "" : ` ${X(reset)},${y100}`}`, fill: "none", stroke, "stroke-width": 2, "stroke-dasharray": "5 4" }));
  } else if (capped) {
    frag.appendChild(el("polyline", { points: `${X(lastT)},${Y(lastV)} ${PR},${Y(lastV)}`, fill: "none", stroke: "var(--ink3)", "stroke-width": 1.5, "stroke-dasharray": "2 5" }));
  }

  // now dot + rules
  frag.appendChild(el("circle", { cx: X(now), cy: Y(used), r: 3.5, fill: capped ? "var(--short)" : "var(--ink)" }));
  frag.appendChild(el("line", { x1: X(now), y1: PT, x2: X(now), y2: PB, stroke: "var(--ink3)", "stroke-width": 1, "stroke-dasharray": "3 3" }));
  if (capped && five.capped_since) frag.appendChild(el("line", { x1: X(five.capped_since), y1: PT, x2: X(five.capped_since), y2: PB, stroke: "var(--short)", "stroke-width": 1 }));
  if (p.available && !clearV && !capped && p.cutoff_at) frag.appendChild(el("line", { x1: X(p.cutoff_at), y1: PT, x2: X(p.cutoff_at), y2: PB, stroke: "var(--short)", "stroke-width": 1.5 }));
  frag.appendChild(el("line", { x1: X(reset), y1: PT, x2: X(reset), y2: PB, stroke: clearV ? "var(--clear)" : "var(--accent)", "stroke-width": 1.5 }));

  // labels
  frag.appendChild(el("text", { x: X(now) + 5, y: PT + 8, fill: "var(--ink2)", "font-family": "JetBrains Mono, monospace", "font-size": 10 }, `now ${hm(now)} · ${used.toFixed(1)}%${capped ? " est" : ""}`));
  if (p.available && !clearV && !capped && p.cutoff_at)
    frag.appendChild(el("text", { x: X(p.cutoff_at) - 5, y: PT + 8, "text-anchor": "end", fill: "var(--short)", "font-family": "JetBrains Mono, monospace", "font-size": 10, "font-weight": 600 }, `cutoff ${hm(p.cutoff_at)}`));
  frag.appendChild(el("text", { x: X(reset) + (clearV ? 6 : 0), y: clearV ? PT + 8 : PB + 19, "text-anchor": clearV ? "start" : "end", fill: clearV ? "var(--clear)" : "var(--accent)", "font-family": "JetBrains Mono, monospace", "font-size": 10, "font-weight": 600 }, clearV ? `reset ${hm(reset)} — quota returns to 0` : `reset ${hm(reset)}`));
  frag.appendChild(el("text", { x: PL, y: PB + 19, fill: "var(--ink3)", "font-family": "JetBrains Mono, monospace", "font-size": 10 }, hm(xStart)));

  if (suppressed) {
    frag.appendChild(el("text", { x: (PL + PR) / 2 + 20, y: Y(46), "text-anchor": "middle", fill: "var(--ink3)", "font-family": "Space Grotesk, sans-serif", "font-size": 12 }, "no projection drawn until the fit is stable"));
    const prog = collectingProgress(p.suppressed_reason);
    if (prog) frag.appendChild(el("text", { x: (PL + PR) / 2 + 20, y: Y(46) + 18, "text-anchor": "middle", fill: "var(--ink3)", "font-family": "JetBrains Mono, monospace", "font-size": 10 }, `${prog.a}m of history · needs ${prog.b}m`));
  }
  svg.appendChild(frag);
}

function drawRunway(s) {
  const five = s.five_hour, p = s.projection, now = s.now, reset = five.resets_at;
  const box = $("runway");
  if (!reset || reset <= now) { box.innerHTML = `<div class="runway"><div class="seg-n">waiting for reset time</div></div>`; return; }
  const span = reset - now;
  if (five.capped) {
    box.innerHTML = `<div class="runway capped"><div class="seg-c">${dur(span)} capped — nothing usable until ${hm(reset)}</div></div>`;
  } else if (p.available && p.cutoff_at && p.cutoff_at < reset) {
    const uw = clamp((p.cutoff_at - now) / span, 0, 1) * 100;
    box.innerHTML = `<div class="runway"><div class="seg-u" style="width:${uw.toFixed(1)}%">${dur(p.cutoff_at - now)} usable</div><div class="seg-c" style="width:${(100 - uw).toFixed(1)}%">${dur(reset - p.cutoff_at)} capped</div></div>`;
  } else if (p.available) {
    box.innerHTML = `<div class="runway"><div class="seg-u" style="width:100%">${dur(span)} usable — the whole window</div></div>`;
  } else {
    box.innerHTML = `<div class="runway"><div class="seg-n" style="width:100%">${dur(span)} left in the window · usable / capped split unknown</div></div>`;
  }
}

// ---------------------------------------------------------------------------
// Composition ring
// ---------------------------------------------------------------------------
function renderComposition(s) {
  const used = s.five_hour.used_percentage || 0;
  const est = !!(s.degraded || s.five_hour.estimated);
  const sessions = (s.sessions || []);
  const unattr = s.unattributed_pct || 0;
  const off = !s.has_attribution;

  const panel = $("composition-panel");
  const C = 2 * Math.PI * 52;
  const svg = [`<svg viewBox="0 0 130 130" class="ring"><g transform="rotate(-90 65 65)">`,
    `<circle cx="65" cy="65" r="52" fill="none" stroke="var(--greysoft)" stroke-width="17"></circle>`];
  let acc = 0;
  const segs = off ? [{ pct: used, col: "var(--grey)", key: "__u__" }]
    : sessions.map((x) => ({ pct: x.est_pct, col: color(x.color_idx), key: x.session_id, dev: x.device }))
      .concat(unattr > 0 ? [{ pct: unattr, col: "var(--grey)", key: "__u__" }] : []);
  for (const g of segs) {
    if (g.pct <= 0) continue;
    const len = g.pct / 100 * C;
    const op = g.key === "__u__" ? 1 : (dimSid(g.key, g.dev) ? 0.22 : 1);
    svg.push(`<circle class="rseg" data-sid="${esc(g.key)}" cx="65" cy="65" r="52" fill="none" stroke="${g.col}" stroke-width="17" stroke-dasharray="${len.toFixed(1)} ${(C - len).toFixed(1)}" stroke-dashoffset="${(-acc).toFixed(1)}" opacity="${op}"></circle>`);
    acc += len;
  }
  svg.push(`</g><text x="65" y="63" text-anchor="middle" fill="var(--ink)" font-family="JetBrains Mono, monospace" font-size="20" font-weight="600">${used.toFixed(1)}</text>`);
  svg.push(`<text x="65" y="77" text-anchor="middle" fill="var(--ink3)" font-family="Space Grotesk, sans-serif" font-size="9.5" letter-spacing="0.06em">USED</text></svg>`);

  let legend;
  if (off) {
    legend = `<div class="lrow"><span class="sw" style="background:var(--grey)"></span><span style="color:var(--ink2)">unattributed</span><span class="val">${used.toFixed(1)}</span></div>
      <div class="lrow" style="color:var(--ink3)"><span class="sw hollow"></span><span>unused</span><span class="val" style="color:var(--ink3)">${(100 - used).toFixed(1)}</span></div>
      <span class="note">The window total is real. Nothing is being split by session yet, so no slice is guessed.</span>`;
  } else {
    legend = sessions.map((x) => `<div class="lrow" data-sid="${esc(x.session_id)}" style="opacity:${dimSid(x.session_id, x.device) ? 0.22 : 1}"><span class="sw" style="background:${color(x.color_idx)}"></span><span>${esc(x.label)}</span><span class="val">${x.est_pct.toFixed(1)}</span></div>`).join("");
    if (unattr > 0) legend += `<div class="lrow" data-sid="__u__"><span class="sw" style="background:var(--grey)"></span><span style="color:var(--ink2)">unattributed</span><span class="val">${unattr.toFixed(1)}</span></div>`;
    legend += `<div class="lrow" style="color:var(--ink3)"><span class="sw hollow"></span><span>unused</span><span class="val" style="color:var(--ink3)">${(100 - used).toFixed(1)}</span></div>`;
  }

  const note = off ? "0 of " + used.toFixed(1) + " explained" : (est ? '<span style="color:var(--warn)">ratios, not points</span>' : `${sessions.length} sessions live`);
  panel.innerHTML = `<div class="phead"><span class="ptitle">Composition of the 100${est ? ' <span style="font-weight:400;color:var(--warn);font-size:11px">· of an estimate</span>' : ""}</span><span class="pnote">${note}</span></div>
    <div class="ring-row">${svg.join("")}<div class="legend">${legend}</div></div>`;
}

// ---------------------------------------------------------------------------
// Attribution
// ---------------------------------------------------------------------------
function renderAttribution(s) {
  const panel = $("attribution-panel");
  const used = s.five_hour.used_percentage || 0;
  const est = !!(s.degraded || s.five_hour.estimated);

  if (!s.has_attribution) return renderAttributionOff(panel, s);

  if (UI.groupBy !== "session") return renderAttributionGrouped(panel, s);

  const sess = (s.sessions || []).slice().sort((a, b) => b.est_pct - a.est_pct);
  const smap = new Map((D.sessions?.sessions || []).map((x) => [x.session_id, x]));
  const unattr = s.unattributed_pct || 0;
  const maxEst = Math.max(0.001, ...sess.map((x) => x.est_pct));
  const totalCost = sess.reduce((a, x) => a + x.cost_usd, 0);
  const totalTok = sess.reduce((a, x) => a + (x.tokens || 0), 0);
  const nDev = new Set(sess.map((x) => x.device).filter(Boolean)).size;

  const compBar = sess.map((x) => `<i data-sid="${esc(x.session_id)}" style="width:${(x.est_pct).toFixed(2)}%;background:${color(x.color_idx)};opacity:${dimSid(x.session_id, x.device) ? 0.22 : 1}"></i>`).join("")
    + (unattr > 0 ? `<i data-sid="__u__" style="width:${unattr.toFixed(2)}%;background:var(--grey)"></i>` : "");

  const head = `<div class="arow head"><span>session</span><span>share of window</span><span class="rt">pts of 100</span><span class="rt">$/hr</span><span class="rt">tokens</span><span>device</span><span class="rt">last active</span></div>`;

  const rows = sess.map((x) => {
    const live = smap.get(x.session_id) || {};
    const share = used > 0 ? (x.est_pct / used * 100) : 0;
    const la = x.idle ? `${dur(live.last_active_ago_s)} · idle` : "live";
    const laCol = x.idle ? "var(--ink3)" : "var(--clear)";
    return `<div class="arow body" data-sid="${esc(x.session_id)}" style="opacity:${dimSid(x.session_id, x.device) ? 0.22 : 1}">
      <span class="aname"><i class="sw" style="background:${color(x.color_idx)}"></i>${esc(x.label)}<span class="sid">${shortId(x.session_id)}</span></span>
      <span class="ashare"><span class="bar"><i style="width:${clamp(x.est_pct / maxEst * 100, 0, 100).toFixed(0)}%;background:${color(x.color_idx)}"></i></span><b>${share.toFixed(1)}%</b></span>
      <span class="acell pts">${est ? "—" : x.est_pct.toFixed(2)}</span>
      <span class="acell hr ink2">${money(live.burn_usd_per_hour || 0)}</span>
      <span class="acell tok ink2">${tok(x.tokens)}</span>
      <span class="acell dev ink2" style="font-size:11px">${esc(x.device || "—")}</span>
      <span class="acell la rt dim" style="color:${laCol}">${la}</span>
    </div>`;
  }).join("");

  const uShare = used > 0 ? (unattr / used * 100) : 0;
  const uRow = unattr > 0 ? `<div class="arow body unattr" data-sid="__u__">
      <span class="aname"><i class="sw" style="background:var(--grey)"></i><span style="color:var(--ink2)">unattributed</span></span>
      <span class="ashare"><span class="bar" style="background:var(--panel2)"><i style="width:100%;background:var(--grey)"></i></span><b style="color:var(--ink2)">${uShare.toFixed(1)}%</b></span>
      <span class="acell pts ink2">${est ? "—" : unattr.toFixed(2)}</span>
      <span class="acell hr dim rt">—</span><span class="acell tok dim rt">—</span>
      <span class="acell dev dim">not local</span><span class="acell la dim rt">—</span>
    </div>` : "";

  const callout = unattr > 0 ? (est ? estCallout() : `<div class="residual"><span><b>${unattr.toFixed(1)} pts (${uShare.toFixed(0)}% of everything spent)</b> is measured quota that local telemetry can't explain — claude.ai chat, Cowork, or a machine that isn't reporting. Session shares are shares of the whole window, not of the attributed part, so this slice is never hidden.</span></div>`) : "";

  panel.innerHTML = `<div class="phead"><div class="phead-l"><span class="ptitle">Attribution — who is spending this window</span>${groupByToggle()}</div>
      <span class="pnote mono">${sess.length} sessions · ${nDev} devices · ${money(totalCost)} · ${tok(totalTok)} tok</span></div>
    <div class="comp-bar">${compBar}</div>${head}${rows}${uRow}${callout}`;
  wireGroupBy(panel);
}

function groupByToggle() {
  const opts = ["session", "device", "model", "agent"];
  return `<div class="toggle" id="groupby">${opts.map((o) => `<button data-gb="${o}" class="${UI.groupBy === o ? "on" : ""}">${o}</button>`).join("")}</div>`;
}
function wireGroupBy(panel) {
  const gb = panel.querySelector("#groupby");
  if (gb) gb.querySelectorAll("button").forEach((b) => (b.onclick = () => { UI.groupBy = b.dataset.gb; pollPanels(); render(); }));
}

function renderAttributionGrouped(panel, s) {
  const data = D.byGroup;
  const used = s.five_hour.used_percentage || 0;
  const unattr = s.unattributed_pct || 0;
  const items = (data && data.items || []).filter((i) => i.cost_usd > 0);
  const totalCost = items.reduce((a, i) => a + i.cost_usd, 0) || 1;
  const attributedPts = Math.max(0, used - unattr);
  const rows = items.map((i, idx) => {
    const pts = i.cost_usd / totalCost * attributedPts;
    const share = used > 0 ? pts / used * 100 : 0;
    return `<div class="arow body" style="grid-template-columns:210px 1fr 78px 86px">
      <span class="aname"><i class="sw" style="background:${color(idx)}"></i>${esc(i.key || "(none)")}</span>
      <span class="ashare"><span class="bar"><i style="width:${clamp(i.cost_usd / items[0].cost_usd * 100, 0, 100).toFixed(0)}%;background:${color(idx)}"></i></span><b>${share.toFixed(1)}%</b></span>
      <span class="acell rt">${pts.toFixed(2)}</span>
      <span class="acell tok ink2 rt">${tok((i.tok_input || 0) + (i.tok_output || 0) + (i.tok_cache_read || 0) + (i.tok_cache_write || 0))}</span>
    </div>`;
  }).join("");
  const uShare = used > 0 ? unattr / used * 100 : 0;
  const uRow = unattr > 0 ? `<div class="arow body unattr" style="grid-template-columns:210px 1fr 78px 86px"><span class="aname"><i class="sw" style="background:var(--grey)"></i><span style="color:var(--ink2)">unattributed</span></span><span class="ashare"><span class="bar" style="background:var(--panel2)"><i style="width:100%;background:var(--grey)"></i></span><b style="color:var(--ink2)">${uShare.toFixed(1)}%</b></span><span class="acell rt ink2">${unattr.toFixed(2)}</span><span class="acell dim rt">—</span></div>` : "";
  panel.innerHTML = `<div class="phead"><div class="phead-l"><span class="ptitle">Attribution — grouped by ${esc(UI.groupBy)}</span>${groupByToggle()}</div><span class="pnote">${esc(SCOPE_WINDOW[UI.scope])} window · points scaled from local cost</span></div>
    <div class="arow head" style="grid-template-columns:210px 1fr 78px 86px"><span>${esc(UI.groupBy)}</span><span>share of window</span><span class="rt">pts of 100</span><span class="rt">tokens</span></div>${rows}${uRow}`;
  wireGroupBy(panel);
}

function estCallout() {
  return `<div class="residual" style="border-color:var(--warn);background:var(--warnsoft)"><span>While the quota is estimated, only <b style="color:var(--ink)">relative shares</b> are trustworthy. Points of quota are withheld — the denominator itself is a guess.</span></div>`;
}

function renderAttributionOff(panel, s) {
  const used = s.five_hour.used_percentage || 0;
  const seven = s.seven_day || {};
  const reset = s.five_hour.resets_at, now = s.now;
  panel.innerHTML = `<div class="phead"><span class="ptitle">Attribution — who is spending this window</span><span class="pnote mono">100% unattributed</span></div>
    <div class="comp-bar"><i style="width:${clamp(used, 0, 100)}%;background:var(--grey)"></i></div>
    <div class="teach">
      <div class="teach-main"><span class="accentbar"></span><div>
        <h3>Per-session attribution is off</h3>
        <p>The 5-hour and 7-day percentages above are exact — they come from Claude Code's status line. Splitting them <em>by session</em> needs OpenTelemetry, and Claude Code only reads that setting when a session starts. Two steps, then the rows fill in on their own.</p>
        <ol><li><span class="stepn">1</span><span>Confirm setup:</span><code>claude-quota doctor</code></li>
        <li><span class="stepn">2</span><span>Restart your Claude Code sessions (or start new ones)</span></li></ol>
      </div></div>
      <div class="teach-side"><span class="eyebrow">meanwhile, this is still true</span>
        <div class="kv"><span>5-hour used</span><span class="mono">${used.toFixed(1)}% · exact</span></div>
        <div class="kv"><span>7-day used</span><span class="mono">${seven.used_percentage != null ? seven.used_percentage.toFixed(0) + "% · exact" : "—"}</span></div>
        <div class="kv"><span>window resets</span><span class="mono">${hm(reset)} · in ${dur(reset - now)}</span></div>
        <div class="kv"><span>otel records</span><span class="mono" style="color:var(--ink3)">0</span></div>
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Explore
// ---------------------------------------------------------------------------
function renderExplore(s) {
  renderRecentWindows(s);
  renderByDevice();
  renderSpendMap(s);
  renderByModel();
  renderCalibration();
}
function renderRecentWindows(s) {
  const w = (D.history && D.history.windows || []).slice().reverse();
  const nCap = w.filter((x) => x.hit_cap).length;
  const bars = w.map((x) => `<div class="hbar ${x.hit_cap ? "cap" : ""}" style="height:${clamp(x.peak_pct, 2, 100)}%" title="${hmDay(x.reset_at)} · peak ${x.peak_pct.toFixed(0)}%${x.hit_cap ? " · hit cap" : ""}"></div>`).join("")
    + `<div class="hbar cur" style="height:${clamp(s.five_hour.used_percentage || 0, 2, 100)}%" title="this window · ${(s.five_hour.used_percentage || 0).toFixed(0)}%"></div>`;
  $("recent-windows").innerHTML = `<div class="phead"><span class="ptitle">Recent windows · peak %</span><span class="pnote mono">${w.length} windows · ${nCap} hit the cap</span></div>
    <div class="hbars"><div class="cap0"></div>${bars}</div>
    <div class="hbars-x"><span>${w.length ? "oldest" : ""}</span><span style="color:var(--short)">■ hit cap</span><span>this window</span></div>`;
}
function renderByDevice() {
  const items = (D.byDevice && D.byDevice.items || []).filter((i) => i.cost_usd > 0);
  const total = items.reduce((a, i) => a + i.cost_usd, 0) || 1;
  const max = Math.max(0.001, ...items.map((i) => i.cost_usd));
  const rows = items.map((i) => {
    const t = (i.tok_input || 0) + (i.tok_output || 0) + (i.tok_cache_read || 0) + (i.tok_cache_write || 0);
    return `<div class="dbar"><div class="t"><span>${esc(i.key)}</span><span class="v">${money(i.cost_usd)} · ${(i.cost_usd / total * 100).toFixed(0)}%</span></div>
      <div class="track"><i style="width:${(i.cost_usd / max * 100).toFixed(0)}%"></i></div>
      <span class="sub">${tok(t)} tok</span></div>`;
  }).join("") || `<span class="pnote">no per-device telemetry yet</span>`;
  $("by-device").innerHTML = `<div class="phead"><span class="ptitle">By device · 7d spend</span><span class="pnote mono">${money(total)}</span></div><div class="dbars">${rows}</div>`;
}
function renderSpendMap(s) {
  const sess = (s.sessions || []).filter((x) => x.cost_usd > 0).sort((a, b) => b.cost_usd - a.cost_usd);
  const unattr = s.unattributed_pct || 0;
  const total = sess.reduce((a, x) => a + x.cost_usd, 0);
  let inner;
  if (!sess.length) inner = `<span class="pnote">no local cost this window</span>`;
  else {
    // Simple 2-column treemap: fill left column until ~58% of total, rest right.
    const left = [], right = []; let acc = 0;
    for (const x of sess) { (acc < total * 0.55 ? left : right).push(x); acc += x.cost_usd; }
    const colSum = (a) => a.reduce((s2, x) => s2 + x.cost_usd, 0) || 1;
    const cell = (x, colTotal) => `<div class="tcell" data-sid="${esc(x.session_id)}" style="flex:${x.cost_usd};background:${color(x.color_idx)};color:#fff;opacity:${dimSid(x.session_id, x.device) ? 0.22 : 1}"><span class="n">${esc(x.label)}</span><span class="c">${money(x.cost_usd)}</span></div>`;
    const lw = colSum(left) / total * 100;
    inner = `<div class="tmap"><div style="width:${lw.toFixed(0)}%;display:flex;flex-direction:column;gap:2px">${left.map((x) => cell(x)).join("")}</div><div style="flex:1;display:flex;flex-direction:column;gap:2px">${right.map((x) => cell(x)).join("")}</div></div>`;
  }
  $("spend-map").innerHTML = `<div class="phead"><span class="ptitle">Spend map · this window</span><span class="pnote mono">${money(total)}</span></div>${inner}
    <span class="pnote" style="display:block;margin-top:8px">local cost only — the unattributed ${unattr.toFixed(1)} pts has no cost data</span>`;
}
function renderByModel() {
  const items = (D.byModel && D.byModel.items || []).filter((i) => i.cost_usd > 0).sort((a, b) => b.cost_usd - a.cost_usd);
  const total = items.reduce((a, i) => a + i.cost_usd, 0) || 1;
  const bar = items.map((i, idx) => `<i style="width:${(i.cost_usd / total * 100).toFixed(1)}%;background:var(--accent);opacity:${(1 - idx * 0.28).toFixed(2)}"></i>`).join("");
  const rows = items.map((i, idx) => `<div class="mrow"><span class="sw" style="background:var(--accent);opacity:${(1 - idx * 0.28).toFixed(2)}"></span><span class="mono">${esc(i.key.replace(/^claude-/, ""))}</span><b class="mono">${(i.cost_usd / total * 100).toFixed(0)}%</b><span class="c">${money(i.cost_usd)}</span></div>`).join("");
  $("by-model").innerHTML = `<div class="phead"><span class="ptitle">By model · ${esc(SCOPE_WINDOW[UI.scope])}</span></div>
    ${items.length ? `<div class="split">${bar}</div><div style="display:flex;flex-direction:column;gap:9px">${rows}</div>` : `<span class="pnote">no model spend in window</span>`}`;
}
function renderCalibration() {
  const health = D.health;
  if (!health) { $("calibration").innerHTML = `<div class="phead"><span class="ptitle">Calibration &amp; feeds</span></div><span class="pnote">…</span>`; return; }
  const cal = health.calibration || {}, src = health.sources || {};
  const models = (cal.models || []).map((m) => `<div class="kv"><span>${esc(m.model.replace(/^claude-/, ""))}</span><span>${m.pct_per_usd.toFixed(2)} %/$</span></div>`).join("");
  const bestR2 = Math.max(0, ...(cal.models || []).map((m) => m.r2 || 0));
  const nMax = Math.max(0, ...(cal.models || []).map((m) => m.n_samples || 0));
  $("calibration").innerHTML = `<div class="phead"><span class="ptitle">Calibration &amp; feeds</span><span class="feed ${cal.calibrated ? "on" : "off"}" style="border:0;padding:0"><i></i>${cal.calibrated ? "calibrated" : "uncalibrated"}</span></div>
    <div class="kvlist">${models}<div class="div"></div>
      <div class="kv"><span>fit</span><span>r² ${bestR2.toFixed(2)} · n=${nMax}</span></div>
      <div class="kv"><span>status samples</span><span>${(src.statusline || {}).samples ?? "—"}</span></div>
      <div class="kv"><span>otel buckets</span><span>${(src.otel_metrics || {}).buckets ?? "—"}</span></div>
      <div class="kv"><span>uptime</span><span>${dur(health.uptime_s)}</span></div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------
let _live = false;
function render() {
  const s = D.summary;
  if (!s) return;
  // merge live per-session extras (share/burn) into summary sessions for convenience
  renderHeader(s);
  renderSlicer(s);
  renderKpi(s);
  renderProjection(s);
  renderComposition(s);
  renderAttribution(s);
  renderExplore(s);
  $("foot-l").textContent = `updated ${hm(s.now)} · streaming /events`;
  $("foot-r").textContent = location.host;
  wireHover();
}

// hover / click isolation over anything with data-sid
function wireHover() {
  // handled by delegation below (added once)
}
document.addEventListener("mouseover", (e) => {
  const t = e.target.closest("[data-sid]"); if (!t) return;
  const sid = t.dataset.sid; if (sid === "__u__") return;
  const dev = (D.summary.sessions.find((x) => x.session_id === sid) || {}).device;
  UI.hover = { type: "session", key: sid, dev }; applyDim();
});
document.addEventListener("mouseout", (e) => {
  if (e.target.closest("[data-sid]")) { UI.hover = null; applyDim(); }
});
document.addEventListener("click", (e) => {
  const t = e.target.closest(".arow.body[data-sid], .tcell[data-sid], .legend .lrow[data-sid]");
  if (!t) return;
  const sid = t.dataset.sid; if (sid === "__u__") return;
  const sObj = D.summary.sessions.find((x) => x.session_id === sid) || {};
  UI.isolate = (UI.isolate && UI.isolate.type === "session" && UI.isolate.key === sid)
    ? null : { type: "session", key: sid, label: sObj.label };
  render();
});
function applyDim() {
  document.querySelectorAll("[data-sid]").forEach((n) => {
    const sid = n.dataset.sid; if (sid === "__u__") { n.style.opacity = 1; return; }
    const dev = (D.summary.sessions.find((x) => x.session_id === sid) || {}).device;
    n.style.opacity = dimSid(sid, dev) ? 0.22 : 1;
  });
}

async function pollPanels() {
  const scopeW = SCOPE_WINDOW[UI.scope];
  try {
    const [m, dev, hist] = await Promise.all([
      fetch(`/api/breakdown?by=model&window=${scopeW}`).then((r) => r.json()),
      fetch(`/api/breakdown?by=device&window=7d`).then((r) => r.json()),
      fetch(`/api/history?days=14`).then((r) => r.json()),
    ]);
    D.byModel = m; D.byDevice = dev; D.history = hist;
    if (UI.groupBy !== "session") {
      D.byGroup = await fetch(`/api/breakdown?by=${UI.groupBy}&window=${scopeW}`).then((r) => r.json());
    }
    fetch("/api/health").then((r) => r.json()).then((h) => { D.health = h; if (D.summary) renderCalibration(); renderHeader(D.summary); }).catch(() => {});
    if (D.summary) render();
  } catch (_) { /* daemon momentarily unreachable */ }
}

function connect() {
  const es = new EventSource("/events");
  es.onopen = () => { _live = true; };
  es.onmessage = (ev) => {
    _live = true;
    try {
      const f = JSON.parse(ev.data);
      D.summary = f.summary; D.sessions = f.sessions;
      render();
    } catch (_) { /* ignore bad frame */ }
  };
  es.onerror = () => { _live = false; if (D.summary) renderHeader(D.summary); };
}

connect();
pollPanels();
setInterval(pollPanels, 10000);
