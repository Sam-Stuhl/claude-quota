"use strict";

// The top of the page: how full the windows are, whether the pace will run
// out, and where the quota went (by kind of work, project, model, session and
// host). One request, /api/overview, polled every 10 seconds.

(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => "$" + (n || 0).toFixed(n >= 100 ? 0 : 2);
  const pctOf = (x) => (x * 100 >= 10 ? Math.round(x * 100) : (x * 100).toFixed(1)) + "%";
  const color = (i) => `var(--s${i % 12})`;
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));

  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const day = (ts) => new Date(ts * 1000).toLocaleDateString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  function dur(sec) {
    sec = Math.max(0, Math.round(sec));
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.round((sec % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    return h ? `${h}h ${m}m` : `${m}m`;
  }
  function ago(sec) {
    if (sec == null) return "never";
    return sec < 90 ? `${Math.max(1, Math.round(sec))}s ago` : `${dur(sec)} ago`;
  }

  const WINDOWS = [["5h", "5-hour"], ["24h", "24h"], ["week", "Week"], ["30d", "30 days"]];
  const TABS = [["kind", "Kind of work"], ["project", "Project"], ["model", "Model"], ["session", "Session"], ["host", "Host"]];
  const S = { window: "5h", tab: "kind", data: null, err: null, lastOk: 0 };
  try {
    const h = JSON.parse(localStorage.getItem("cq-view") || "{}");
    if (WINDOWS.some((w) => w[0] === h.window)) S.window = h.window;
    if (TABS.some((t) => t[0] === h.tab)) S.tab = h.tab;
  } catch (_) { /* first visit */ }

  // ---- header -------------------------------------------------------------
  function renderFeeds(d) {
    const l = d.limits, feeds = [];
    if (!l.known) feeds.push(["down", "no limits reading yet"]);
    else if (l.stale) feeds.push(["down", `limits stale · last ${ago(l.age_s)} (${l.source})`]);
    else feeds.push(["on", `limits live · ${l.source} · ${ago(l.age_s)}`]);
    feeds.push([d.calibrated ? "on" : "off", d.calibrated ? "calibrated" : "points split by cost"]);
    $("feeds").innerHTML = feeds.map(([c, t]) => `<span class="feed ${c}"><i></i>${esc(t)}</span>`).join("");
  }

  // ---- gauges -------------------------------------------------------------
  function gauge(pct, expired, mark) {
    if (pct == null) {
      return `<div class="gauge empty"></div>`;
    }
    const cls = pct >= 90 ? "hot" : pct >= 70 ? "warm" : "";
    const m = mark != null ? `<b class="gmark" style="left:${clamp(mark, 0, 100)}%"></b>` : "";
    return `<div class="gauge ${cls}"><i style="width:${clamp(pct, 0, 100)}%"></i>${m}</div>`;
  }

  function renderFive(d) {
    const f = d.limits.five, p = d.projection, now = d.now;
    let big = "–", note = "";
    if (f.pct != null) {
      big = Math.round(f.pct) + "%";
      note = f.resets_at ? `resets in <b>${dur(f.resets_at - now)}</b> · ${clock(f.resets_at)}` : "reset time unknown";
    } else if (f.expired) {
      note = "the last window has reset; waiting for a new reading";
    } else {
      note = "no reading";
    }
    $("g-five").innerHTML = `<div class="eyebrow">5-hour window</div>
      <div class="big mono">${big}</div>${gauge(f.pct, f.expired)}<div class="gnote">${note}</div>`;
  }

  function renderWeek(d) {
    const w = d.limits.seven, now = d.now;
    let big = "–", note = "no reading", mark = null, pace = "";
    if (w.pct != null) {
      big = Math.round(w.pct) + "%";
      note = w.resets_at ? `resets in <b>${dur(w.resets_at - now)}</b> · ${day(w.resets_at)}` : "reset time unknown";
      if (w.resets_at) {
        const total = 7 * 86400, elapsed = clamp(total - (w.resets_at - now), 0, total);
        mark = (elapsed / total) * 100;
        if (elapsed > 24 * 3600) {
          const end = (w.pct / elapsed) * total;
          pace = `<div class="gnote ${end >= 100 ? "bad" : ""}">at this pace <b>${Math.round(end)}%</b> by reset${end >= 100 ? ", runs out first" : ""}</div>`;
        }
      }
    }
    $("g-week").innerHTML = `<div class="eyebrow">Week</div>
      <div class="big mono">${big}</div>${gauge(w.pct, w.expired, mark)}<div class="gnote">${note}</div>${pace}`;
  }

  // ---- projection ---------------------------------------------------------
  function sparkline(d) {
    const pts = d.series || [], start = d.five_start, reset = d.limits.five.resets_at;
    if (!reset || pts.length < 2) return "";
    const W = 600, H = 84, pad = 4, span = reset - start;
    const x = (t) => pad + ((t - start) / span) * (W - 2 * pad);
    const y = (v) => H - pad - (clamp(v, 0, 100) / 100) * (H - 2 * pad);
    let path = pts.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    let proj = "";
    const p = d.projection;
    if (p.available) {
      const last = pts[pts.length - 1];
      const endT = Math.min(p.cutoff_at, reset), endV = last[1] + (p.burn_rate_pct_per_hour / 3600) * (endT - last[0]);
      proj = `<path d="M${x(last[0]).toFixed(1)},${y(last[1]).toFixed(1)} L${x(endT).toFixed(1)},${y(endV).toFixed(1)}" class="sp-proj ${p.verdict === "SHORT" ? "bad" : ""}"/>`;
    }
    const nowX = x(Math.min(d.now, reset)).toFixed(1);
    return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="5-hour usage so far and projection">
      <line x1="${pad}" x2="${W - pad}" y1="${y(100)}" y2="${y(100)}" class="sp-cap"/>
      <line x1="${nowX}" x2="${nowX}" y1="0" y2="${H}" class="sp-now"/>
      <path d="${path}" class="sp-line"/>${proj}</svg>
      <div class="sp-axis mono"><span>${clock(start)}</span><span>now ${clock(d.now)}</span><span>${clock(reset)}</span></div>`;
  }

  function renderProj(d) {
    const p = d.projection, f = d.limits.five, now = d.now;
    let head, sub = "", cls = "";
    if (f.pct == null) {
      head = "No projection";
      sub = esc(p.suppressed_reason || "no reading");
    } else if (!p.available) {
      head = "Not enough movement yet";
      sub = esc(p.suppressed_reason || "collecting data");
    } else {
      const burn = p.burn_rate_pct_per_hour;
      if (p.verdict === "SHORT") {
        head = `Runs out at <b>${clock(p.cutoff_at)}</b>`;
        sub = `${dur(p.margin_seconds)} before the window resets`;
        cls = "bad";
      } else {
        head = "Lasts until the reset";
        sub = `cap would be reached ${dur(p.margin_seconds)} after it`;
        cls = "ok";
      }
      sub += ` · burning ${burn.toFixed(1)}%/h`;
    }
    $("g-proj").innerHTML = `<div class="eyebrow">Projection</div><div class="phl ${cls}">${head}</div>
      <div class="gnote">${sub}</div>${sparkline(d)}`;
  }

  // ---- where it went ------------------------------------------------------
  function rows(items, d) {
    if (!items.length) return `<div class="pnote">nothing in this window</div>`;
    const max = Math.max(...items.map((i) => i.cost_usd), 0.0001);
    const hasPts = items.some((i) => i.points != null);
    return items.map((it, i) => {
      const sub = [it.kind, it.project, it.host, it.model].filter(Boolean);
      const subline = S.tab === "session" ? `<span class="wsub">${esc(sub.join(" · "))}${it.live ? ' · <em>live</em>' : ""}</span>` : "";
      const grey = it.key === "(smaller)" || it.key === "other";
      return `<div class="wrow"><span class="wname"><i class="sw" style="background:${grey ? "var(--grey)" : color(i)}"></i>
          <span class="wlab"><span class="ulabel" title="${esc(it.label)}">${esc(it.label)}</span>${subline}</span></span>
        <span class="wbar"><i style="width:${(it.cost_usd / max * 100).toFixed(1)}%;background:${grey ? "var(--grey)" : color(i)}"></i></span>
        <b class="wshare mono">${pctOf(it.share)}</b>
        ${hasPts ? `<span class="wpts mono">${it.points != null ? it.points.toFixed(1) + " pts" : ""}</span>` : ""}
        <span class="wcost mono">${money(it.cost_usd)}</span></div>`;
    }).join("");
  }

  function renderWhere(d) {
    const t = d.totals, u = d.unclassified;
    const win = WINDOWS.map(([k, l]) => `<button class="${S.window === k ? "on" : ""}" data-w="${k}">${l}</button>`).join("");
    const tabs = TABS.map(([k, l]) => `<button class="${S.tab === k ? "on" : ""}" data-t="${k}">${l}</button>`).join("");
    const span = `${d.window === "week" || d.window === "5h" ? d.basis + " · " : ""}${clock(d.start)} to now`;
    const total = `<b>${money(t.cost_usd)}</b> list price · ${t.requests.toLocaleString()} requests · ${t.sessions} sessions` +
      (t.points != null ? ` · measured <b>${Math.round(t.points)}%</b> of the limit` : "");
    let leftover;
    if (u.cost_usd <= 0) leftover = "Every request in this window is placed.";
    else {
      const f = u.top_folders.map((x) => esc(x.folder)).join(", ");
      leftover = `Unclassified: <b>${pctOf(u.share)}</b> (${money(u.cost_usd)}), sessions with no ATLAS record and no recognisable folder${f ? `: ${f}` : ""}.`;
    }
    const hasPts = t.points != null;
    $("where").innerHTML = `<div class="phead"><div class="phead-l"><span class="ptitle">Where it went</span>
        <div class="toggle" id="w-win">${win}</div></div><span class="pnote mono">${esc(span)}</span></div>
      <div class="wtabs toggle" id="w-tabs">${tabs}</div>
      <div class="wsum">${total}</div>
      <div class="wlist">${rows(d.by[S.tab] || [], d)}</div>
      <div class="wfoot"><span>${leftover}</span>${hasPts ? `<span>Points split the measured percentage by cost share; the limits also include claude.ai app use, which no local record sees.</span>` : ""}</div>`;
    $("w-win").querySelectorAll("button").forEach((b) => (b.onclick = () => { S.window = b.dataset.w; save(); load(); }));
    $("w-tabs").querySelectorAll("button").forEach((b) => (b.onclick = () => { S.tab = b.dataset.t; save(); renderWhere(S.data); }));
  }

  function save() {
    try { localStorage.setItem("cq-view", JSON.stringify({ window: S.window, tab: S.tab })); } catch (_) { /* private mode */ }
  }

  function render() {
    const d = S.data;
    if (!d) return;
    renderFeeds(d);
    renderFive(d);
    renderWeek(d);
    renderProj(d);
    renderWhere(d);
    const tm = new Date(S.lastOk).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
    $("foot-l").textContent = S.err ? `last update ${tm} · ${S.err}` : `updated ${tm} · every 10s`;
    $("foot-r").textContent = location.host;
  }

  async function load() {
    try {
      const r = await fetch(`/api/overview?window=${S.window}`, { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      S.data = await r.json();
      S.err = null;
      S.lastOk = Date.now();
    } catch (e) {
      S.err = "cannot reach the daemon (" + e.message + ")";
    }
    if (S.data) render();
    else $("where").innerHTML = `<span class="pnote">${esc(S.err || "loading")}</span>`;
  }

  $("where").innerHTML = `<span class="pnote">loading…</span>`;
  load();
  setInterval(load, 10000);
})();
