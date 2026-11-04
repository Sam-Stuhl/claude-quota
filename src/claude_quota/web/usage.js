// "Where it went": /api/usage as a drill-down. Starts at categories, then
// groups, sessions, turns and single requests; any level can be pivoted by
// model, MCP server, skill, subagent, device or effort. State lives in the URL
// hash so a view can be linked and survives a reload.
(() => {
  const $u = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => "$" + (n || 0).toFixed(n >= 100 ? 0 : 2);
  const pct = (x) => (x * 100).toFixed(x >= 0.1 ? 0 : 1) + "%";
  const tok = (n) => (n >= 1e9 ? (n / 1e9).toFixed(1) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(0) + "k" : String(n || 0));
  const color = (i) => `var(--s${i % 12})`;
  const when = (ts) => {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  };

  const WINDOWS = ["5h", "today", "24h", "7d", "30d"];
  const PIVOTS = [["", "next level"], ["model", "model"], ["mcp", "MCP server"], ["skill", "skill"], ["subagent", "subagent"], ["device", "device"], ["effort", "effort"]];
  const S = { window: "7d", path: [], by: "", turnsByTime: false, showAll: false };
  const SHOW = 25;

  function readHash() {
    const m = location.hash.match(/usage=([^&]*)/);
    if (!m) return;
    try {
      const v = JSON.parse(decodeURIComponent(m[1]));
      Object.assign(S, { window: v.w || "7d", path: v.p || [], by: v.b || "" });
    } catch (_) { /* ignore a malformed hash */ }
  }
  function writeHash() {
    const v = encodeURIComponent(JSON.stringify({ w: S.window, p: S.path, b: S.by }));
    history.replaceState(null, "", "#usage=" + v);
  }

  async function load() {
    const q = new URLSearchParams({ window: S.window });
    S.path.forEach((p) => q.append("p", p));
    if (S.by) q.set("by", S.by);
    const r = await fetch("/api/usage?" + q.toString(), { cache: "no-store" });
    if (!r.ok) throw new Error("usage " + r.status);
    return r.json();
  }

  function go(path, by) {
    S.path = path;
    S.showAll = false;
    if (by !== undefined) S.by = by;
    writeHash();
    refresh();
  }

  function changeCell(c) {
    if (c.kind === "item" || c.kind === "turn") return `<span class="acell dim rt"></span>`;
    if (c.change == null) return `<span class="acell dim rt">new</span>`;
    const up = c.change > 0;
    const v = Math.abs(c.change) >= 10 ? "10x+" : (up ? "+" : "") + Math.round(c.change * 100) + "%";
    return `<span class="acell rt" style="color:${up ? "var(--short)" : "var(--clear)"}">${v}</span>`;
  }
  function quotaCell(c, d) {
    if (c.est_5h_points == null) return `<span class="acell dim rt">n/a</span>`;
    const wk = d.week_per_5h_point ? ` · ${(c.est_5h_points * d.week_per_5h_point).toFixed(1)}% wk` : "";
    return `<span class="acell rt">${c.est_5h_points.toFixed(1)} pts${wk}</span>`;
  }

  const COLS = "minmax(220px,2.2fr) minmax(120px,1.4fr) 70px 104px 60px 70px 64px";
  function head(first) {
    return `<div class="arow head" style="grid-template-columns:${COLS}"><span>${esc(first)}</span><span>share</span><span class="rt">cost</span><span class="rt">quota (est)</span><span class="rt">reqs</span><span class="rt">tokens</span><span class="rt">vs prev</span></div>`;
  }
  function row(c, i, d, max, opts) {
    const total = c.tok_input + c.tok_output + c.tok_cache_read + c.tok_cache_write;
    const sub = opts.sub ? `<span class="usub">${opts.sub}</span>` : "";
    const est = c.estimated ? `<span class="utag" title="placed by a heuristic, not ATLAS's own record">est</span>` : "";
    return `<div class="arow body urow${opts.click ? " click" : ""}" style="grid-template-columns:${COLS}" data-i="${i}">
      <span class="aname"><i class="sw" style="background:${opts.grey ? "var(--grey)" : color(i)}"></i><span class="ulabel" title="${esc(c.label)}">${esc(c.label)}${sub}</span>${est}</span>
      <span class="ashare"><span class="bar"><i style="width:${(c.cost_usd / max * 100).toFixed(1)}%;background:${opts.grey ? "var(--grey)" : color(i)}"></i></span><b>${pct(c.share)}</b></span>
      <span class="acell rt">${money(c.cost_usd)}</span>
      ${quotaCell(c, d)}
      <span class="acell rt ink2">${c.requests}</span>
      <span class="acell rt ink2">${tok(total)}</span>
      ${changeCell(c)}
    </div>`;
  }
  function table(first, items, d, opts = {}) {
    if (!items || !items.length) return `<span class="pnote">nothing in this window</span>`;
    const max = Math.max(0.0001, ...items.map((c) => c.cost_usd));
    const shown = S.showAll || opts.all ? items : items.slice(0, SHOW);
    const more = items.length > shown.length
      ? `<button class="chip umore" data-more="1">show all ${items.length}</button>` : "";
    return head(first) + shown.map((c, i) => row(c, i, d, max, { click: opts.click, sub: opts.sub && opts.sub(c), grey: opts.grey && opts.grey(c) })).join("") + more;
  }
  function wire(panel, sel, items, onPick) {
    panel.querySelectorAll(sel + " .urow.click").forEach((n) => {
      n.onclick = () => onPick(items[+n.dataset.i]);
    });
  }

  function compBar(items) {
    const tot = items.reduce((a, c) => a + c.cost_usd, 0) || 1;
    return `<div class="comp-bar">${items.map((c, i) => `<i title="${esc(c.label)} ${money(c.cost_usd)}" style="width:${(c.cost_usd / tot * 100).toFixed(2)}%;background:${color(i)}"></i>`).join("")}</div>`;
  }

  function residualNote(res, d) {
    if (!res) return "";
    if (res.est_5h_points == null) {
      return `<div class="residual"><span><b>Claude app (chat, voice, mobile)</b> can only be seen as the quota the status line measured that no shipped request explains. Not shown for this window: ${esc(res.note || "not enough data")}.</span></div>`;
    }
    const wk = d.week_per_5h_point ? ` (about ${(res.est_5h_points * d.week_per_5h_point).toFixed(1)}% of the week)` : "";
    return `<div class="residual"><span><b>Claude app (chat, voice, mobile): about ${res.est_5h_points.toFixed(1)} pts${wk}</b>. That is the quota measured in this window (${res.measured_5h_points.toFixed(1)} pts of 5-hour limit) minus what every shipped session explains. It also holds any machine that is not shipping. It cannot be broken down further: claude.ai keeps no local record.</span></div>`;
  }

  function itemMeta(it) {
    const bits = [];
    if (it.atlas_number) bits.push(`ATLAS #${it.atlas_number}`);
    if (it.started_by) bits.push(`started by ${esc(it.started_by)}`);
    if (it.device) bits.push(esc(it.device));
    if (it.entrypoint) bits.push(esc(it.entrypoint));
    if (it.folder) bits.push(esc(it.folder));
    else if (it.cwd) bits.push(`<span class="mono">${esc(it.cwd.replace(/^\/Users\/[^/]+/, "~"))}</span>`);
    if (it.session_first_ts) bits.push(`${when(it.session_first_ts)} to ${when(it.session_last_ts)}`);
    if (it.pr_url) bits.push(`<a href="${esc(it.pr_url)}" target="_blank" rel="noopener">PR</a>`);
    bits.push(`<span class="mono dim">${esc(it.session_id.slice(0, 8))}</span>`);
    return `<div class="umeta">${bits.join(" · ")}</div>`;
  }

  function render(d) {
    const panel = $u("usage-panel");
    const crumbs = d.breadcrumbs.map((c, i) => i === d.breadcrumbs.length - 1
      ? `<b>${esc(c.label)}</b>`
      : `<a href="#" data-c="${i}">${esc(c.label)}</a>`).join(`<span class="dim"> / </span>`);
    const n = d.node;
    const win = WINDOWS.map((w) => `<button class="${S.window === w ? "on" : ""}" data-w="${w}">${w}</button>`).join("");
    const piv = PIVOTS.map(([k, l]) => `<button class="${S.by === k ? "on" : ""}" data-b="${k}">${l}</button>`).join("");
    const chg = n.change == null ? "" : ` · ${n.change > 0 ? "+" : ""}${Math.round(n.change * 100)}% vs previous ${esc(S.window)}`;
    const cal = d.calibrated ? "" : ` · quota points appear once calibration is trusted`;
    let body = "";

    if (d.requests) {
      body = `<div class="usec"><span class="eyebrow">requests in this turn</span>${requestsTable(d.requests)}</div>`;
    } else if (d.sections) {
      const s = d.sections;
      const turns = S.turnsByTime ? s.turns.slice().sort((a, b) => (a.ts || 0) - (b.ts || 0)) : s.turns;
      body = itemMeta(d.item)
        + `<div class="usec"><div class="usec-h"><span class="eyebrow">turns · ${s.turns.length}</span><div class="toggle" id="u-tsort"><button class="${S.turnsByTime ? "" : "on"}" data-t="0">by cost</button><button class="${S.turnsByTime ? "on" : ""}" data-t="1">in order</button></div></div><div id="u-turns">${table("what you asked", turns, d, { click: true, sub: (c) => [when(c.ts), c.origin].filter(Boolean).join(" · ") })}</div></div>`
        + (s.subagents.length ? `<div class="usec"><span class="eyebrow">subagents</span><div id="u-subs">${table("subagent", s.subagents, d)}</div></div>` : "")
        + `<div class="ugrid">`
        + `<div class="usec"><span class="eyebrow">MCP servers</span>${table("server", s.mcp, d)}</div>`
        + `<div class="usec"><span class="eyebrow">skills</span>${table("skill", s.skills, d)}</div>`
        + `<div class="usec"><span class="eyebrow">models</span>${table("model", s.models, d)}</div>`
        + `</div>`;
      d._turns = turns;
    } else {
      const level = S.by ? PIVOTS.find((p) => p[0] === S.by)[1] : ["category", "group", "session"][S.path.length] || "item";
      const click = !S.by;
      body = compBar(d.children) + `<div id="u-kids">${table(level, d.children, d, {
        click,
        sub: (c) => c.session_id ? [c.started_by && `by ${c.started_by}`, c.device, when(c.last_ts)].filter(Boolean).join(" · ") : "",
      })}</div>` + (S.path.length === 0 && !S.by ? residualNote(d.residual, d) : "");
    }

    panel.innerHTML = `<div class="phead"><div class="phead-l"><span class="ptitle">Where it went</span><div class="toggle" id="u-win">${win}</div></div>
        <span class="pnote mono">${money(n.cost_usd)} list price · ${n.requests} requests${chg}${cal}</span></div>
      <div class="ubar"><div class="ucrumbs">${crumbs}</div>${d.requests || d.sections ? "" : `<div class="ubreak"><span class="pnote">break down by</span><div class="toggle" id="u-by">${piv}</div></div>`}</div>
      ${body}`;

    panel.querySelectorAll(".ucrumbs a").forEach((a) => (a.onclick = (e) => { e.preventDefault(); go(d.breadcrumbs[+a.dataset.c].path, ""); }));
    panel.querySelectorAll("#u-win button").forEach((b) => (b.onclick = () => { S.window = b.dataset.w; writeHash(); refresh(); }));
    panel.querySelectorAll("#u-by button").forEach((b) => (b.onclick = () => go(S.path, b.dataset.b)));
    panel.querySelectorAll("#u-tsort button").forEach((b) => (b.onclick = () => { S.turnsByTime = b.dataset.t === "1"; render(d); }));
    panel.querySelectorAll(".umore").forEach((b) => (b.onclick = () => { S.showAll = true; render(d); }));
    if (d.children && !S.by) wire(panel, "#u-kids", d.children, (c) => go([...S.path, c.key], ""));
    if (d.sections) wire(panel, "#u-turns", d._turns, (c) => go([...S.path, c.key], ""));
  }

  function requestsTable(reqs) {
    if (!reqs.length) return `<span class="pnote">no requests</span>`;
    const cols = "120px 1fr 74px 74px 74px 74px 70px";
    const h = `<div class="arow head" style="grid-template-columns:${cols}"><span>time</span><span>model · source · tool</span><span class="rt">input</span><span class="rt">output</span><span class="rt">cache read</span><span class="rt">cache write</span><span class="rt">cost</span></div>`;
    return h + reqs.map((r) => `<div class="arow body" style="grid-template-columns:${cols}">
      <span class="acell ink2">${when(r.ts)}</span>
      <span class="acell ulabel">${esc((r.model || "").replace(/^claude-/, ""))} · ${esc(r.agent_type || "main")}${r.mcp_server ? " · " + esc(r.mcp_server) + (r.mcp_tool ? "." + esc(r.mcp_tool) : "") : ""}${r.skill ? " · /" + esc(r.skill) : ""}${r.effort ? " · " + esc(r.effort) : ""}</span>
      <span class="acell rt ink2">${tok(r.tok_input)}</span><span class="acell rt ink2">${tok(r.tok_output)}</span>
      <span class="acell rt ink2">${tok(r.tok_cache_r)}</span><span class="acell rt ink2">${tok(r.tok_cache_w)}</span>
      <span class="acell rt">${money(r.cost_usd)}</span></div>`).join("");
  }

  async function refresh() {
    try {
      render(await load());
    } catch (e) {
      $u("usage-panel").innerHTML = `<div class="phead"><span class="ptitle">Where it went</span></div><span class="pnote">could not load usage (${esc(e.message)}). Is a shipper running? See <span class="mono">claude-quota doctor</span>.</span>`;
    }
  }

  readHash();
  refresh();
  setInterval(refresh, 60000);
})();
