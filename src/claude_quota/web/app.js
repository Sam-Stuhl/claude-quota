"use strict";

// Live quota dashboard. Reads /events (SSE) and paints the rail, the time
// axis, and the session leaderboard. Session colours are keyed on color_idx so
// a session keeps its colour across reloads.

const PALETTE = 12;
const $ = (id) => document.getElementById(id);

function color(idx) {
  return `var(--s${((idx % PALETTE) + PALETTE) % PALETTE})`;
}

function fmtTime(ts) {
  if (!ts) return "?";
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function fmtDayTime(ts) {
  if (!ts) return "?";
  return new Date(ts * 1000).toLocaleString([], {
    weekday: "short", hour: "numeric", minute: "2-digit",
  });
}
function fmtDur(sec) {
  if (sec == null) return "?";
  sec = Math.abs(Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h${String(m).padStart(2, "0")}m` : `${m}m`;
}
function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }

function setConn(state) {
  const dot = $("dot"), text = $("conn-text");
  dot.className = "dot " + (state === "live" ? "live" : state === "down" ? "down" : "");
  text.textContent = state === "live" ? "live" : state === "down" ? "reconnecting…" : "connecting…";
}

function renderRail(summary) {
  const five = summary.five_hour;
  const used = five.used_percentage || 0;
  const sessions = summary.sessions || [];
  const unattributed = summary.unattributed_pct || 0;

  // Segment widths are absolute percentage points; when capped past 100 we
  // normalise so the rail stays full but keep the true number in the label.
  const rawTotal = sessions.reduce((a, s) => a + s.est_pct, 0) + unattributed;
  const scale = used > 100 && rawTotal > 0 ? 100 / rawTotal : 1;

  const track = $("rail-track");
  track.innerHTML = "";
  for (const s of sessions) {
    if (s.est_pct <= 0) continue;
    const seg = document.createElement("div");
    seg.className = "seg";
    seg.style.width = `${clamp(s.est_pct * scale, 0, 100)}%`;
    seg.style.background = color(s.color_idx);
    seg.dataset.sid = s.session_id;
    seg.title = `${s.label}: ${s.est_pct.toFixed(1)}%`;
    track.appendChild(seg);
  }
  if (unattributed > 0) {
    const seg = document.createElement("div");
    seg.className = "seg unattributed";
    seg.style.width = `${clamp(unattributed * scale, 0, 100)}%`;
    seg.dataset.sid = "__unattributed__";
    seg.title = "Unattributed: claude.ai chat and Cowork draw from the same pool";
    track.appendChild(seg);
  }

  $("rail").classList.toggle("degraded", !!summary.degraded);

  // Big number, with the over-cap note.
  $("five-pct").textContent = Math.round(used);
  $("five-est").hidden = !(summary.degraded || five.estimated);
  if (five.capped && five.capped_since) {
    $("five-est").hidden = false;
    $("five-est").textContent = `${used.toFixed(0)}% — capped ${fmtDur(summary.now - five.capped_since)} ago`;
  } else if (summary.degraded || five.estimated) {
    $("five-est").textContent = "estimated from local telemetry";
  }
}

function renderTimeAxis(summary) {
  const five = summary.five_hour;
  const proj = summary.projection || {};
  const start = summary.window_start;
  const reset = five.resets_at;
  const now = summary.now;
  const axis = $("timeaxis");
  const resetMark = $("reset-mark");

  const verdictEl = $("verdict");
  const projline = $("projline");

  if (!reset || !start || reset <= start) {
    axis.style.visibility = "hidden";
    resetMark.hidden = true;
    verdictEl.className = "verdict";
    verdictEl.textContent = "";
    projline.textContent = summary.degraded ? "quota estimated from local telemetry (rate_limits absent)" : "";
    return;
  }
  axis.style.visibility = "visible";
  const span = reset - start;
  const fracNow = clamp((now - start) / span, 0, 1);

  $("t-start").textContent = fmtTime(start);
  $("t-reset").textContent = fmtTime(reset);
  const tnow = $("t-now");
  tnow.style.left = `${fracNow * 100}%`;
  tnow.textContent = "now";

  // Cutoff is a marker only (its time is stated in the projection line below),
  // so it never collides with the now/reset labels when they cluster.
  const tcut = $("t-cutoff");
  if (proj.available && proj.cutoff_at) {
    const fracCut = clamp((proj.cutoff_at - start) / span, 0, 1);
    tcut.style.left = `${fracCut * 100}%`;
    tcut.style.visibility = "visible";
    tcut.textContent = "";
    tcut.className = "t-cutoff " + (proj.verdict === "SHORT" ? "short" : "clear");
  } else {
    tcut.style.visibility = "hidden";
  }

  // Where the fill will be when the window resets (headroom preview).
  if (proj.available && proj.burn_rate_pct_per_hour > 0) {
    const hrs = (reset - now) / 3600;
    const projPct = (five.used_percentage || 0) + proj.burn_rate_pct_per_hour * hrs;
    resetMark.hidden = false;
    resetMark.style.left = `${clamp(projPct, 0, 100)}%`;
    resetMark.title = `Projected ${projPct.toFixed(0)}% by reset`;
  } else {
    resetMark.hidden = true;
  }

  // Verdict chip + projection line.
  if (proj.available && proj.verdict) {
    verdictEl.className = "verdict " + proj.verdict.toLowerCase();
    verdictEl.textContent = `${proj.verdict} · ${fmtDur(proj.margin_seconds)} margin`;
    const err = proj.burn_rate_stderr ? ` ± ${proj.burn_rate_stderr.toFixed(1)}` : "";
    projline.textContent =
      `burn ${proj.burn_rate_pct_per_hour.toFixed(1)}${err} %/hr · ` +
      `cutoff ${fmtTime(proj.cutoff_at)} vs reset ${fmtTime(reset)}`;
  } else {
    verdictEl.className = "verdict paused";
    verdictEl.textContent = proj.suppressed_reason || "no projection";
    projline.textContent = proj.suppressed_reason
      ? `projection paused: ${proj.suppressed_reason}` : "";
  }
}

function renderSeven(summary) {
  const s = summary.seven_day;
  const track = $("seven-track");
  track.innerHTML = "";
  if (s.used_percentage == null) {
    $("seven-pct").textContent = "—";
    $("seven-reset").textContent = "no 7-day data";
    return;
  }
  $("seven-pct").textContent = Math.round(s.used_percentage);
  const seg = document.createElement("div");
  seg.className = "seg";
  seg.style.width = `${clamp(s.used_percentage, 0, 100)}%`;
  seg.style.background = "var(--accent)";
  track.appendChild(seg);
  $("seven-reset").textContent = s.resets_at ? `resets ${fmtDayTime(s.resets_at)}` : "";
}

function renderSessions(sessionsData, summary) {
  const box = $("sessions");
  box.innerHTML = "";
  const rows = (sessionsData.sessions || []);
  for (const s of rows) {
    const row = document.createElement("div");
    row.className = "srow";
    row.dataset.sid = s.session_id;
    row.innerHTML = `
      <span class="swatch" style="background:${color(s.color_idx)}"></span>
      <span class="slabel">${escapeHtml(s.label)}${s.idle ? '<span class="idle">idle</span>' : ""}</span>
      <span class="sshare">${s.share_of_sessions_pct.toFixed(0)}%</span>
      <span class="scost">$${s.cost_usd.toFixed(2)}</span>
      <span class="sburn">$${s.burn_usd_per_hour.toFixed(2)}/hr</span>`;
    box.appendChild(row);
  }
  if (sessionsData.unattributed_pct > 0) {
    const row = document.createElement("div");
    row.className = "srow unattributed";
    row.dataset.sid = "__unattributed__";
    row.innerHTML = `
      <span class="swatch" style="background:var(--grey-slice)"></span>
      <span class="slabel">unattributed (claude.ai / Cowork)</span>
      <span class="sshare">${sessionsData.unattributed_pct.toFixed(1)}%</span>
      <span class="scost"></span><span class="sburn"></span>`;
    box.appendChild(row);
  }
  $("calib").textContent = summary.calibrated ? "calibrated" : "uncalibrated";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Hover-to-isolate: dim everything whose sid doesn't match the hovered element.
function wireHover() {
  const root = document.body;
  root.addEventListener("mouseover", (e) => {
    const el = e.target.closest("[data-sid]");
    if (!el) return;
    const sid = el.dataset.sid;
    document.querySelectorAll("[data-sid]").forEach((n) => {
      n.classList.toggle("is-dimmed", n.dataset.sid !== sid);
    });
  });
  root.addEventListener("mouseout", (e) => {
    if (e.target.closest("[data-sid]")) {
      document.querySelectorAll(".is-dimmed").forEach((n) => n.classList.remove("is-dimmed"));
    }
  });
}

function render(data) {
  const { summary, sessions } = data;
  renderRail(summary);
  renderTimeAxis(summary);
  renderSeven(summary);
  renderSessions(sessions, summary);
  $("foot-updated").textContent = "updated " + fmtTime(summary.now);
  $("foot-window").textContent = summary.window_start
    ? `window ${fmtTime(summary.window_start)} → ${fmtTime(summary.five_hour.resets_at)}` : "";
}

function connect() {
  const es = new EventSource("/events");
  es.onopen = () => setConn("live");
  es.onmessage = (ev) => {
    setConn("live");
    try { render(JSON.parse(ev.data)); } catch (_) { /* ignore a bad frame */ }
  };
  // EventSource reconnects on its own with backoff; reflect the state as a dot.
  es.onerror = () => setConn("down");
}

wireHover();
setConn("connecting");
connect();
