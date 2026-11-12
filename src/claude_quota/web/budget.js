"use strict";

// The weekly budget: today's share of the 7-day limit, what is left of it, the
// week's days (planned share, used, background), and the plan editor.
// One request, /api/budget, polled every 20 seconds.

(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const p1 = (x) => (x == null ? "–" : (Math.round(x * 10) / 10).toFixed(1) + "%");
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const TYPES = [["work", "Work"], ["school", "School"], ["light", "Light"]];
  const DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const S = { data: null, editing: false, msg: "", msgBad: false };

  function todayCard(d) {
    const t = d.today, w = d.week;
    const left = t.left_pct;
    const big = left >= 0 ? `${p1(left)} <small>left today</small>` : `${p1(-left)} <small>over today</small>`;
    const bar = t.budget_pct > 0 ? Math.min(100, (t.used_pct / t.budget_pct) * 100) : 100;
    const cls = t.status === "over" ? "hot" : t.status === "near" ? "warm" : "";
    const pace = w.ahead_by > 0.5 ? `<b>${w.ahead_by.toFixed(1)} points ahead</b> of plan`
      : w.ahead_by < -0.5 ? `${(-w.ahead_by).toFixed(1)} points under plan` : "on plan";
    const bg = `background ${p1(t.background_pct)} of its ${p1(t.background_allow_pct)} share`;
    const warns = d.warnings.map((x) => `<div class="${/Over|reserve|used up/.test(x) ? "bad" : ""}">${esc(x)}</div>`).join("");
    const gate = d.background.level === "hold"
      ? `<div class="gnote bad">ATLAS is holding background work: ${esc(d.background.hold.join("; "))}.</div>`
      : d.background.level === "warn" ? `<div class="gnote">ATLAS warns before background work: ${esc(d.background.warn.join("; "))}.</div>` : "";
    return `<div class="bud-today ${t.status}">
      <div class="eyebrow">Today · ${esc(t.label)} · ${esc(t.dow)} until ${clock(t.ends_at)}</div>
      <div class="big">${big}</div>
      <div class="gauge ${cls}"><i style="width:${bar.toFixed(1)}%"></i></div>
      <div class="gnote"><b>${p1(t.used_pct)}</b> of today's <b>${p1(t.budget_pct)}</b> used · ${bg}</div>
      <div class="gnote">Week <b>${p1(w.used_pct)}</b> used, ${pace} (${p1(w.plan_through_today_pct)} planned by tonight, ${w.reserve_pct}% held in reserve)${d.estimated ? " · estimated, no live reading this week" : ""}</div>
      ${gate}<div class="bud-warn">${warns}</div></div>`;
  }

  function dayCols(d) {
    const days = d.days;
    const top = 1.15 * Math.max(13, ...days.map((x) => Math.max(x.plan_pct || 0, x.budget_pct || 0, x.used_pct || 0)));
    const h = (v) => `${Math.min(100, ((v || 0) / top) * 100).toFixed(1)}%`;
    return `<div class="bud-week"><div class="bud-days">${days.map((x) => {
      const target = x.state === "past" ? x.plan_pct : x.budget_pct;
      const over = x.used_pct != null && target != null && x.used_pct > target + 0.05;
      const val = x.state === "future" ? `${p1(x.budget_pct)}`
        : x.state === "today" ? `${p1(x.used_pct)} / ${p1(x.budget_pct)}` : `${p1(x.used_pct)} / ${p1(x.plan_pct)}`;
      const src = x.type_source === "cedar" ? " · Cedar" : x.type_source === "override" ? " · set" : "";
      return `<div class="bday ${x.state}" title="${esc(x.label + src)}: planned ${p1(x.plan_pct)}${x.budget_pct != null ? ", budget now " + p1(x.budget_pct) : ""}">
        <div class="dh"><b>${esc(x.dow)}</b><span>${x.date.slice(5)}</span></div>
        <div class="dt">${esc(x.label)}${esc(src)}</div>
        <div class="col">
          ${x.used_pct != null ? `<i class="u ${over ? "over" : ""}" style="height:${h(x.used_pct)}"></i>` : ""}
          ${x.background_pct ? `<i class="bg" style="height:${h(x.background_pct)}"></i>` : ""}
          ${target != null ? `<i class="ln" style="bottom:${h(target)}"></i>` : ""}
        </div>
        <div class="dv ${over ? "bad" : ""}">${val}</div></div>`;
    }).join("")}</div>
    <div class="bud-legend"><span><i style="background:var(--accent)"></i>used</span>
      <span><i style="background:var(--s2)"></i>of it background (ATLAS routines, roles, agents)</span>
      <span><i style="border-top:2px dashed var(--ink3);height:0;width:12px"></i>budget (past days: the plan)</span>
      <span>Future days show their share of what is left; today rebalances as the week goes.</span></div></div>`;
  }

  function editor(d) {
    const p = d.plan;
    const sel = (name, v, extra = "") => `<select data-k="${name}">${extra}${TYPES.map(([k, l]) =>
      `<option value="${k}" ${v === k ? "selected" : ""}>${l}</option>`).join("")}</select>`;
    const weights = TYPES.map(([k, l]) => `<label>${l} day <input type="number" step="0.05" min="0" max="5" data-w="${k}" value="${p.weights[k]}"></label>`).join("");
    const week = DOW.map((k) => `<label>${k[0].toUpperCase() + k.slice(1)} ${sel("week." + k, p.week[k])}</label>`).join("");
    const days = d.days.filter((x) => x.state !== "past").map((x) => {
      const ov = p.overrides[x.date] || "auto";
      return `<label>${esc(x.dow)} ${x.date.slice(5)} <select data-k="ov.${x.date}">
        <option value="auto" ${ov === "auto" ? "selected" : ""}>Auto (${esc(x.type_source === "override" ? "pattern" : x.type)})</option>
        ${TYPES.map(([k, l]) => `<option value="${k}" ${ov === k ? "selected" : ""}>${l}</option>`).join("")}</select></label>`;
    }).join("");
    return `<div class="bud-edit ${S.editing ? "open" : ""}" id="bud-edit">
      <div class="row"><span class="eyebrow">Weights</span>${weights}</div>
      <div class="row"><span class="eyebrow">Usual week</span>${week}</div>
      <div class="row"><span class="eyebrow">This week</span>${days}</div>
      <div class="row"><span class="eyebrow">Limits</span>
        <label>Reserve % <input type="number" step="1" min="0" max="50" data-n="reserve_pct" value="${p.reserve_pct}"></label>
        <label>Background share of a day % <input type="number" step="5" min="0" max="100" data-n="background_share" value="${Math.round(p.background_share * 100)}"></label>
        <label>Hold background when 5-hour is past % <input type="number" step="5" min="0" max="100" data-n="five_h_guard" value="${p.five_h_guard}"></label>
        <label><input type="checkbox" data-b="use_cedar" ${p.use_cedar ? "checked" : ""}> Work days from Cedar${d.cedar.configured ? "" : " (not configured)"}</label></div>
      <div class="row"><button class="bud-btn primary" id="bud-save">Save plan</button>
        <button class="bud-btn" id="bud-reset">Reset to defaults</button>
        <span class="bud-msg ${S.msgBad ? "bad" : ""}">${esc(S.msg)}</span></div></div>`;
  }

  function render() {
    const d = S.data;
    if (!d) return;
    if (!d.known) {
      $("budget").innerHTML = `<div class="phead"><span class="ptitle">Weekly budget</span></div><span class="pnote">${esc(d.reason)}</span>`;
      return;
    }
    // Leave an open editor alone, so a refresh never eats what Sam is typing.
    if (S.editing && $("bud-edit") && $("bud-edit").classList.contains("open")) {
      $("bud-top").innerHTML = todayCard(d) + dayCols(d);
      return;
    }
    $("budget").innerHTML = `<div class="phead"><span class="ptitle">Weekly budget</span>
        <span><span class="pnote mono">resets ${new Date(d.week.reset * 1000).toLocaleDateString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}</span>
        <button class="bud-btn" id="bud-toggle">${S.editing ? "Close" : "Adjust plan"}</button></span></div>
      <div class="bud-top" id="bud-top">${todayCard(d)}${dayCols(d)}</div>${editor(d)}`;
    $("bud-toggle").onclick = () => { S.editing = !S.editing; S.msg = ""; $("budget").innerHTML = ""; render(); };
    const save = $("bud-save");
    if (save) save.onclick = () => submit(collect());
    const reset = $("bud-reset");
    if (reset) reset.onclick = () => submit({
      weights: { work: 1.55, school: 1.0, light: 0.45 },
      week: { mon: "work", tue: "school", wed: "school", thu: "work", fri: "school", sat: "light", sun: "light" },
      reserve_pct: 10, background_share: 0.25, five_h_guard: 70, use_cedar: true,
      overrides: Object.fromEntries(Object.keys(d.plan.overrides).map((k) => [k, "auto"])),
    });
  }

  function collect() {
    const root = $("bud-edit"), out = { weights: {}, week: {}, overrides: {} };
    root.querySelectorAll("[data-w]").forEach((i) => (out.weights[i.dataset.w] = parseFloat(i.value)));
    root.querySelectorAll("[data-n]").forEach((i) => (out[i.dataset.n] = parseFloat(i.value)));
    out.background_share = out.background_share / 100;
    root.querySelectorAll("[data-b]").forEach((i) => (out[i.dataset.b] = i.checked));
    root.querySelectorAll("select[data-k]").forEach((s) => {
      const [kind, key] = s.dataset.k.split(/\.(.+)/);
      if (kind === "week") out.week[key] = s.value;
      else out.overrides[key] = s.value;
    });
    return out;
  }

  async function submit(change) {
    try {
      const r = await fetch("/api/budget/plan", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(change) });
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
      S.data = body;
      S.editing = false;
      S.msg = "";
      S.msgBad = false;
    } catch (e) {
      S.msg = e.message;
      S.msgBad = true;
      S.editing = true;
    }
    $("budget").innerHTML = "";
    render();
  }

  async function load() {
    try {
      const r = await fetch("/api/budget", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      S.data = await r.json();
      render();
    } catch (e) {
      if (!S.data) $("budget").innerHTML = `<span class="pnote">budget unavailable (${esc(e.message)})</span>`;
    }
  }

  $("budget").innerHTML = `<span class="pnote">loading budget…</span>`;
  load();
  setInterval(load, 20000);
})();
