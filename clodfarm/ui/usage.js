/* The USAGE page: for every Claude account (seat), its 5-hour and weekly utilization over time from /api/usage, the
 * limits its agents stop at, and (for the farm manager or the person of a Claude on that seat) a form to set the
 * seat's own limits. Refreshes every 60 seconds while you look; a form being edited is left alone. */
const BASE = document.body.dataset.base || "";
const $ = s => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";
let hours = 24, editing = false;

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(4)) if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}
function s(tag, attrs = {}) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}
const pct = x => x == null ? "–" : `${Math.round(x * 100)}%`;
function until(ts) {
  if (!ts) return "";
  const m = Math.max(0, Math.round((ts * 1000 - Date.now()) / 60000));
  return m >= 1440 ? `${Math.floor(m / 1440)}d ${Math.floor(m % 1440 / 60)}h` : m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

async function api(path, opts = {}) {
  if (opts.body) opts = { ...opts, method: "POST", headers: { "Content-Type": "application/json", "X-Clodfarm": "1" } };
  const r = await fetch(`${BASE}/api/${path}`, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function chart(seat, since) {
  const W = 900, H = 190, L = 40, R = 10, T = 10, B = 24;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", preserveAspectRatio: "none", role: "img",
    "aria-label": `${seat.seat} usage over the last ${hours} hours` });
  const t0 = since * 1000, t1 = Date.now();
  const x = t => L + (W - L - R) * Math.min(1, Math.max(0, (t * 1000 - t0) / (t1 - t0)));
  const y = v => T + (H - T - B) * (1 - Math.min(1, Math.max(0, v)));
  for (const v of [0, .25, .5, .75, 1]) {
    svg.append(s("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid-line" }));
    const lab = s("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", class: "axis" });
    lab.textContent = pct(v);
    svg.append(lab);
  }
  for (let i = 0; i <= 4; i++) {
    const t = (t0 + (t1 - t0) * i / 4) / 1000, d = new Date(t * 1000);
    const lab = s("text", { x: x(t), y: H - 6, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle", class: "axis" });
    lab.textContent = hours <= 24 ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString([], { month: "short", day: "numeric" });
    svg.append(lab);
  }
  svg.append(s("line", { x1: L, x2: W - R, y1: y(seat.limits.five_hour_ceiling), y2: y(seat.limits.five_hour_ceiling), class: "cap5" }));
  svg.append(s("line", { x1: L, x2: W - R, y1: y(seat.limits.weekly_target), y2: y(seat.limits.weekly_target), class: "cap7" }));
  for (const [key, cls] of [["five_hour", "l5"], ["seven_day", "l7"]]) {
    const pts = seat.points.filter(p => p[key] != null);
    if (pts.length) svg.append(s("polyline", { class: cls, points: pts.map(p => `${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ") }));
  }
  return svg;
}

function limitsForm(seat) {
  const own = seat.limits.own || {};
  const field = (key, label, val, isPct) => h("label", {},
    h("span", { class: key in own ? "own" : "" }, label + (key in own ? " *" : "")),
    h("input", { type: "number", name: key, min: isPct ? 1 : 0, max: isPct ? 100 : 64, step: 1,
      value: isPct ? Math.round(val * 100) : val, onfocus: () => { editing = true; } }));
  const msg = h("span", { class: "msg" });
  const form = h("form", { class: "limits", onsubmit: async e => {
    e.preventDefault();
    const f = new FormData(form), body = { seat: seat.seat };
    body.five_hour_ceiling = Number(f.get("five_hour_ceiling")) / 100;
    body.weekly_target = Number(f.get("weekly_target")) / 100;
    body.max_workers = Number(f.get("max_workers"));
    try { await api("limits", { body: JSON.stringify(body) }); msg.textContent = "saved"; editing = false; load(); }
    catch (err) { $("#error").textContent = err.message; }
  } },
    field("five_hour_ceiling", "5-HOUR STOP %", seat.limits.five_hour_ceiling, true),
    field("weekly_target", "WEEKLY STOP %", seat.limits.weekly_target, true),
    field("max_workers", "MAX AGENTS", seat.limits.max_workers, false),
    h("button", { type: "submit", class: "btn primary" }, "SAVE"),
    Object.keys(own).length ? h("button", { type: "button", class: "btn", onclick: async () => {
      try { await api("limits", { body: JSON.stringify({ seat: seat.seat, clear: Object.keys(own) }) }); editing = false; load(); }
      catch (err) { $("#error").textContent = err.message; }
    } }, "USE DEFAULTS") : null,
    msg);
  return form;
}

function seatCard(seat, since) {
  const n = seat.now || {}, sm = seat.summary || {};
  const stat = (label, v, extra) => h("div", { class: "stat" }, h("b", {}, label), h("span", {}, pct(v)), extra ? h("small", {}, extra) : null);
  return h("section", { class: "card panel seat" },
    h("div", { class: "panel-head" }, h("h2", {}, seat.seat),
      h("span", { class: "who" }, seat.claudes.length ? seat.claudes.join(", ") : "no Claude up on it right now")),
    h("div", { class: "now-row" },
      stat("5-HOUR NOW", n.five_hour, n.five_hour_resets ? `resets in ${until(n.five_hour_resets)}` : ""),
      stat("5-HOUR PEAK", sm.five_hour && sm.five_hour.peak, `stop at ${pct(seat.limits.five_hour_ceiling)}`),
      stat("WEEKLY NOW", n.seven_day, n.seven_day_resets ? `resets in ${until(n.seven_day_resets)}` : ""),
      stat("WEEKLY STOP", seat.limits.weekly_target),
      h("div", { class: "stat" }, h("b", {}, "AGENTS"), h("span", {}, `${seat.decision.running}/${seat.decision.workers}`),
        h("small", {}, `max ${seat.limits.max_workers}`))),
    seat.points.length ? chart(seat, since) : h("p", { class: "empty" }, "No history yet: it fills in as this seat's agents run."),
    h("p", { class: "legend" }, h("span", {}, h("i", { class: "k5" }), "5-hour"), h("span", {}, h("i", { class: "k7" }), "weekly"),
      h("span", { class: "muted" }, "dashed: where agents stop")),
    h("p", { class: "reason" }, `Governor: ${seat.decision.reason}`),
    seat.can_edit ? limitsForm(seat) : null);
}

async function load() {
  if (editing) return;
  try {
    const d = await api(`usage?hours=${hours}`);
    $("#error").textContent = "";
    const box = $("#seats");
    box.replaceChildren(...(d.seats.length ? d.seats.map(x => seatCard(x, d.since))
      : [h("section", { class: "card panel" }, h("p", { class: "empty" }, "No Claude account has reported usage yet."))]));
  } catch (e) { $("#error").textContent = e.message; }
}

document.querySelectorAll("#range button").forEach(b => b.addEventListener("click", () => {
  hours = Number(b.dataset.hours);
  document.querySelectorAll("#range button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  editing = false;
  load();
}));
load();
setInterval(load, 60000);
