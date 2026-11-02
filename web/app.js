"use strict";
// 2D editor and monitoring UI. All state lives in the backend; the UI polls /api/state.

const $ = (s) => document.querySelector(s);
const canvas = $("#canvas");
const ctx = canvas.getContext("2d");
const W = 150, H = 64, PORT_R = 6;

let state = null;           // last /api/state payload
let selectedId = null;
let drag = null;            // {id, dx, dy, moved}
let pendingPort = null;     // {asset_id, port} – output port chosen for a new conveyor
let lastEventSeq = 0;
let eventBuffer = [];
let mouse = { x: 0, y: 0 };
let inspectorDirty = false;
let view = { ox: 0, oy: 0, s: 1 };   // pan/zoom transform (screen = world * s + o)
let pan = null;
let needsFit = true;
let userView = false;   // set once the user pans/zooms; disables automatic refits on resize

// ------------------------------------------------------------------ API helpers
async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const msg = data && (data.detail !== undefined ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : JSON.stringify(data));
    throw new Error(`${res.status}: ${msg}`);
  }
  return data;
}
function toast(msg, ok = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = ok ? "ok" : ""; t.style.display = "block";
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.style.display = "none"), ok ? 2000 : 6000);
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, true); await refresh(); return r; }
  catch (e) { toast(e.message); }
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (t) => (t === null || t === undefined ? "–" : Number(t).toFixed(1));

// ------------------------------------------------------------------ geometry
const nodeH = (a) => Math.max(H, 18 * (Math.max(a.ports?.inputs?.length || 0, a.ports?.outputs?.length || 0) + 1));
const assetsById = () => Object.fromEntries((state?.assets || []).map((a) => [a.id, a]));
const nodes = () => (state?.assets || []).filter((a) => a.kind !== "conveyor");
const conveyors = () => (state?.assets || []).filter((a) => a.kind === "conveyor");

function portPos(asset, port, dir) {
  const list = dir === "in" ? asset.ports.inputs : asset.ports.outputs;
  const i = Math.max(0, list.indexOf(port));
  const y = asset.position.y + (nodeH(asset) * (i + 1)) / (list.length + 1);
  return { x: asset.position.x + (dir === "in" ? 0 : W), y };
}
function hitPort(x, y) {
  for (const a of nodes()) {
    for (const [dir, list] of [["in", a.ports.inputs], ["out", a.ports.outputs]]) {
      for (const p of list) {
        const pp = portPos(a, p, dir);
        if ((pp.x - x) ** 2 + (pp.y - y) ** 2 <= (PORT_R + 3) ** 2) return { asset: a, port: p, dir };
      }
    }
  }
  return null;
}
function hitNode(x, y) {
  const ns = nodes();
  for (let i = ns.length - 1; i >= 0; i--) {
    const a = ns[i];
    if (x >= a.position.x && x <= a.position.x + W && y >= a.position.y && y <= a.position.y + nodeH(a)) return a;
  }
  return null;
}
function conveyorEnds(c, byId) {
  const s = byId[c.ports.source.asset_id], t = byId[c.ports.target.asset_id];
  if (!s || !t) return null;
  return { a: portPos(s, c.ports.source.port, "out"), b: portPos(t, c.ports.target.port, "in") };
}
function hitConveyor(x, y) {
  const byId = assetsById();
  for (const c of conveyors()) {
    const e = conveyorEnds(c, byId); if (!e) continue;
    const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y, L2 = dx * dx + dy * dy || 1;
    const t = Math.max(0, Math.min(1, ((x - e.a.x) * dx + (y - e.a.y) * dy) / L2));
    const px = e.a.x + t * dx, py = e.a.y + t * dy;
    if ((px - x) ** 2 + (py - y) ** 2 < 36) return c;
  }
  return null;
}

// ------------------------------------------------------------------ rendering
const STATE_COLORS = { running: "#3d8a4f", processing: "#3d8a4f", idle: "#5a6270", blocked: "#c8962b",
  stopped: "#7a5a9a", failed: "#c0392b", unconfigured: "#444" };
const KIND_COLORS = { producer: "#2c4f6e", transformer: "#5b3f1f", splitter: "#3f2f5c", sink: "#2f5236" };

function resize() {
  const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  canvas.width = r.width * dpr; canvas.height = r.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
function draw() {
  const r = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, r.width, r.height);
  ctx.strokeStyle = "#2a2f37"; ctx.lineWidth = 1;
  for (let x = 0; x < r.width; x += 40) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, r.height); ctx.stroke(); }
  for (let y = 0; y < r.height; y += 40) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(r.width, y); ctx.stroke(); }
  if (!state) return requestAnimationFrame(draw);
  if (needsFit && nodes().length) { fitView(); needsFit = false; }
  const byId = assetsById();
  ctx.save(); ctx.translate(view.ox, view.oy); ctx.scale(view.s, view.s);
  const convLabels = [], portLabels = [];

  for (const c of conveyors()) {
    const e = conveyorEnds(c, byId); if (!e) continue;
    ctx.strokeStyle = c.id === selectedId ? "#f0a640" : STATE_COLORS[c.state] || "#777";
    ctx.lineWidth = c.id === selectedId ? 6 : 4;
    ctx.beginPath(); ctx.moveTo(e.a.x, e.a.y); ctx.lineTo(e.b.x, e.b.y); ctx.stroke();
    for (const it of c.items || []) {
      const x = e.a.x + (e.b.x - e.a.x) * it.progress, y = e.a.y + (e.b.y - e.a.y) * it.progress;
      ctx.fillStyle = it.defective ? "#ff5a5a" : "#ffd36b";
      ctx.fillRect(x - 3, y - 3, 6, 6);
    }
    convLabels.push({ e, text: `${c.name} ${c.counts.occupancy}/${c.counts.capacity}`, sel: c.id === selectedId });
  }
  if (pendingPort) {
    const a = byId[pendingPort.asset_id];
    if (a) {
      const p = portPos(a, pendingPort.port, "out");
      ctx.setLineDash([5, 4]); ctx.strokeStyle = "#f0a640"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(mouse.x, mouse.y); ctx.stroke(); ctx.setLineDash([]);
    }
  }
  for (const a of nodes()) {
    const { x, y } = a.position;
    ctx.fillStyle = KIND_COLORS[a.kind] || "#333";
    ctx.strokeStyle = a.id === selectedId ? "#f0a640" : STATE_COLORS[a.state] || "#777";
    ctx.lineWidth = a.id === selectedId ? 3 : 2;
    ctx.beginPath(); ctx.roundRect(x, y, W, nodeH(a), 6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#fff"; ctx.textAlign = "left"; ctx.font = "bold 12px system-ui";
    ctx.fillText(a.name.slice(0, 22), x + 8, y + 16);
    ctx.font = "11px system-ui"; ctx.fillStyle = "#cfd6df";
    ctx.fillText(`${a.kind} · ${a.state}`, x + 8, y + 31);
    ctx.fillText(summaryLine(a).slice(0, 26), x + 8, y + 46);
    if (a.kind === "transformer" && a.counts && a.counts.active_cycle_id) {
      ctx.fillStyle = "#3d8a4f"; ctx.fillRect(x + 8, y + 52, (W - 16) * (a.counts.cycle_progress || 0), 5);
      ctx.strokeStyle = "#555"; ctx.lineWidth = 1; ctx.strokeRect(x + 8, y + 52, W - 16, 5);
    }
    for (const [dir, list] of [["in", a.ports.inputs], ["out", a.ports.outputs]]) {
      for (const p of list) {
        const pp = portPos(a, p, dir);
        const connected = dir === "in" ? a.inputs[p] : a.outputs[p];
        ctx.fillStyle = connected ? "#9fd3ff" : "#1d2026";
        ctx.strokeStyle = pendingPort && pendingPort.asset_id === a.id && pendingPort.port === p ? "#f0a640" : "#9fd3ff";
        ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(pp.x, pp.y, PORT_R, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        portLabels.push({ pp, dir, text: a.ports.labels?.[p] || p });
      }
    }
  }
  drawLabels(portLabels, convLabels);
  ctx.restore();
  requestAnimationFrame(draw);
}
// Labels are placed after all shapes, on a dark pill, avoiding nodes and each other (greedy, first fit).
function drawLabels(portLabels, convLabels) {
  const placed = nodes().map((a) => ({ x: a.position.x - 1, y: a.position.y - 1, w: W + 2, h: nodeH(a) + 2 }));
  const hit = (r) => placed.some((q) => r.x < q.x + q.w && q.x < r.x + r.w && r.y < q.y + q.h && q.y < r.y + r.h);
  const pill = (r, text, color, bg) => {
    ctx.fillStyle = bg; ctx.beginPath(); ctx.roundRect(r.x, r.y, r.w, r.h, 3); ctx.fill();
    ctx.fillStyle = color; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillText(text, r.x + 3, r.y + r.h / 2 + 0.5);
    ctx.textBaseline = "alphabetic";
  };
  ctx.font = "9px system-ui";
  for (const l of portLabels) {
    const text = l.text.length > 18 ? l.text.slice(0, 17) + "\u2026" : l.text;
    const w = ctx.measureText(text).width + 6, hgt = 12;
    const x = l.dir === "in" ? l.pp.x - 8 - w : l.pp.x + 8;
    const cands = [l.pp.y - 7 - hgt / 2 - 1, l.pp.y + 7 + hgt / 2 + 1, l.pp.y - 19, l.pp.y + 19].map((cy) => ({ x, y: cy - hgt / 2, w, h: hgt }));
    const r = cands.find((c) => !hit(c)) || cands[0];
    placed.push(r);
    pill(r, text, "#9aa4b2", "rgba(29,32,38,.85)");
  }
  ctx.font = "11px system-ui";
  for (const l of convLabels) {
    const { a, b } = l.e;
    const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    const nx = -(b.y - a.y) / len, ny = (b.x - a.x) / len;
    const w = ctx.measureText(l.text).width + 8, hgt = 15;
    const cands = [];
    for (const t of [0.5, 0.38, 0.62, 0.28, 0.72, 0.2, 0.8]) {
      for (const side of [-1, 1]) {
        const off = side * (hgt / 2 + 4 + Math.abs(nx) * (w / 2 - hgt / 2));
        const cx = a.x + (b.x - a.x) * t + nx * off, cy = a.y + (b.y - a.y) * t + ny * off;
        cands.push({ x: cx - w / 2, y: cy - hgt / 2, w, h: hgt });
      }
    }
    const r = cands.find((c) => !hit(c)) || cands[0];
    placed.push(r);
    pill(r, l.text, l.sel ? "#f0a640" : "#c9d1dc", "rgba(24,27,32,.9)");
  }
}
function summaryLine(a) {
  const c = a.counts || {};
  if (a.kind === "producer") return `pending ${c.pending ?? 0} · ${a.config.batch_quantity}/${a.config.interval}${a.config.interval_unit[0]}`;
  if (a.kind === "transformer") return `${a.config.recipe_id || "no recipe"} @${a.config.speed_pct}%`;
  if (a.kind === "splitter") return a.config.mode === "percentage" ? `${a.config.percentage_a}% → A` : `conditional (${a.config.rules.length})`;
  if (a.kind === "sink") return `received ${c.received ?? 0}`;
  return "";
}

// ------------------------------------------------------------------ canvas interaction
function screenXY(ev) { const r = canvas.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; }
function canvasXY(ev) { const p = screenXY(ev); return { x: (p.x - view.ox) / view.s, y: (p.y - view.oy) / view.s }; }
function fitView() {
  const ns = nodes(); if (!ns.length) return;
  const r = canvas.getBoundingClientRect(), pad = 50;
  const minX = Math.min(...ns.map((a) => a.position.x)), minY = Math.min(...ns.map((a) => a.position.y));
  const maxX = Math.max(...ns.map((a) => a.position.x + W)), maxY = Math.max(...ns.map((a) => a.position.y + nodeH(a)));
  const s = Math.min(1.5, (r.width - 2 * pad) / Math.max(1, maxX - minX), (r.height - 2 * pad) / Math.max(1, maxY - minY));
  view = { s, ox: pad - minX * s + (r.width - 2 * pad - (maxX - minX) * s) / 2, oy: pad - minY * s + (r.height - 2 * pad - (maxY - minY) * s) / 2 };
}
canvas.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const p = screenXY(ev), k = ev.deltaY < 0 ? 1.1 : 1 / 1.1, s = Math.min(4, Math.max(0.2, view.s * k));
  userView = true; view.ox = p.x - ((p.x - view.ox) * s) / view.s; view.oy = p.y - ((p.y - view.oy) * s) / view.s; view.s = s;
}, { passive: false });
canvas.addEventListener("dblclick", (ev) => { if (!hitNode(canvasXY(ev).x, canvasXY(ev).y)) { userView = false; fitView(); } });
canvas.addEventListener("mousedown", (ev) => {
  const { x, y } = canvasXY(ev);
  const port = hitPort(x, y);
  if (port) {
    if (port.dir === "out") { pendingPort = { asset_id: port.asset.id, port: port.port }; return; }
    if (pendingPort) {
      const src = pendingPort; pendingPort = null;
      act(() => api("POST", "/api/connections", { name: "Conveyor", source: src, target: { asset_id: port.asset.id, port: port.port },
        config: { length_m: 10, capacity_per_m: 1, speed_m_s: 1 } }), "Conveyor created");
    }
    return;
  }
  pendingPort = null;
  const n = hitNode(x, y);
  if (n) { select(n.id); drag = { id: n.id, dx: x - n.position.x, dy: y - n.position.y, moved: false }; return; }
  const c = hitConveyor(x, y);
  select(c ? c.id : null);
  if (!c) { const sp = screenXY(ev); pan = { x: sp.x, y: sp.y, ox: view.ox, oy: view.oy }; }
});
canvas.addEventListener("mousemove", (ev) => {
  mouse = canvasXY(ev);
  if (pan) { const sp = screenXY(ev); userView = true; view.ox = pan.ox + sp.x - pan.x; view.oy = pan.oy + sp.y - pan.y; return; }
  if (!drag || !state) return;
  const a = assetsById()[drag.id]; if (!a) return;
  a.position = { x: Math.round(mouse.x - drag.dx), y: Math.round(mouse.y - drag.dy) };
  drag.moved = true;
});
window.addEventListener("mouseup", () => {
  if (drag && drag.moved) {
    const a = assetsById()[drag.id];
    if (a) api("PUT", `/api/assets/${encodeURIComponent(a.id)}/position`, a.position).catch((e) => toast(e.message));
  }
  drag = null; pan = null;
});
// ------------------------------------------------------------------ copy / paste
let clipboard = null, pasteCount = 0;
function copyAsset(id) {
  const a = assetsById()[id];
  if (!a || a.kind === "conveyor") return toast("Select a machine (not a conveyor) to copy");
  clipboard = { kind: a.kind, name: a.name, config: structuredClone(a.config), position: { ...a.position }, height: nodeH(a) };
  pasteCount = 0;
  toast(`Copied ${a.name}`, true);
}
async function pasteAsset() {
  if (!clipboard) return toast("Nothing to copy-paste yet: select an asset and press Ctrl+C");
  pasteCount++;
  const { kind, name, config, position } = clipboard;
  const pos = { x: position.x + 30 * pasteCount, y: position.y + (clipboard.height + 24) * pasteCount };
  const r = await act(() => api("POST", "/api/assets", { kind, name: `${name} (copy)`, config, position: pos }), "Pasted");
  if (r && r.id) select(r.id);
}
const typing = () => ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
window.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") pendingPort = null;
  if ((ev.ctrlKey || ev.metaKey) && !typing() && !String(window.getSelection())) {
    const k = ev.key.toLowerCase();
    if (k === "c" && selectedId) { ev.preventDefault(); copyAsset(selectedId); }
    else if (k === "v") { ev.preventDefault(); pasteAsset(); }
    else if (k === "d" && selectedId) { ev.preventDefault(); copyAsset(selectedId); pasteAsset(); }
  }
  if (ev.key === "Delete" && selectedId && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
    if (confirm(`Delete ${selectedId}?`)) {
      const id = selectedId; select(null);
      act(() => api("DELETE", `/api/assets/${encodeURIComponent(id)}`), "Deleted");
    }
  }
});

// ------------------------------------------------------------------ palette
document.querySelectorAll("#palette button[data-kind]").forEach((b) => b.addEventListener("click", () => {
  const n = nodes().length;
  openAssetDialog(b.dataset.kind, { x: Math.round((40 - view.ox) / view.s) + (n % 5) * 30, y: Math.round((40 - view.oy) / view.s) + (n % 4) * 20 })
    .catch((e) => toast(e.message));
}));

// ------------------------------------------------------------------ inspector
// Per-device REST endpoints and broker topics, shown in the inspector.
const EXAMPLE_PATCH = { producer: ["interval", 30], transformer: ["speed_pct", 70], splitter: ["percentage_a", 60],
  sink: ["capacity", 100], conveyor: ["speed_m_s", 1] };
function deviceApiHtml(a, integ) {
  const base = location.origin, id = encodeURIComponent(a.id), q = (s) => `'${s.replace(/'/g, "'\\''")}'`;
  const [k, dflt] = EXAMPLE_PATCH[a.kind] || ["name", a.name];
  const patch = JSON.stringify({ [k]: a.config?.[k] ?? dflt });
  const rows = [
    ["GET", `/api/assets/${id}`, "State, config, counters, statistics"],
    ["GET", `/api/assets/${id}/stats`, "Statistics only"],
    ["GET", `/api/assets/${id}/operational-history?limit=50`, "State intervals (running, idle, blocked...)"],
    ["GET", `/api/events?asset_id=${id}&limit=50`, "Machine events"],
    ["GET", `/api/config-changes?asset_id=${id}`, "Versioned configuration changes"],
    ...(a.kind === "transformer" || a.kind === "producer" ? [["GET", `/api/cycles?asset_id=${id}&limit=20`, "Production cycles"]] : []),
    ...(a.kind === "sink" ? [["GET", `/api/assets/${id}/products?limit=50`, "Received products"]] : []),
    ["PATCH", `/api/assets/${id}/config`, "Change configuration (only the keys you send)", patch],
    ["POST", `/api/assets/${id}/commands`, "Command: start | stop | fault | restart", '{"command":"stop"}'],
  ];
  const curl = ([m, path, , body]) => body
    ? `curl -X ${m} ${base}${path} -H "Content-Type: application/json" -d ${q(body)}`
    : `curl ${q(base + path)}`;
  const active = integ && integ.adapter && integ.adapter !== "none";
  const prefix = (integ && integ.topic_prefix) || "industrial-sim";
  return `
    <p class="hint">Full interactive reference: <a href="/docs" target="_blank" rel="noopener">${esc(base)}/docs</a> (OpenAPI JSON at <code>/openapi.json</code>).</p>
    <table class="hist api">${rows.map((r) => `<tr><td><span class="method ${r[0].toLowerCase()}">${r[0]}</span></td>
      <td>${r[0] === "GET" ? `<a href="${esc(r[1])}" target="_blank" rel="noopener"><code>${esc(r[1])}</code></a>` : `<code>${esc(r[1])}</code>`}
        <div class="hint">${esc(r[2])}${r[3] ? ` · body <code>${esc(r[3])}</code>` : ""}</div></td>
      <td><button class="small" data-copy="${esc(curl(r))}" title="Copy curl command">curl</button></td></tr>`).join("")}</table>
    <p class="hint">Broker messages ${active ? `(${esc(integ.adapter)} adapter active)` : "(enable an adapter in the Integration tab)"}:
      topics <code>${esc(prefix)}.machine</code>, <code>${esc(prefix)}.product</code>, <code>${esc(prefix)}.config</code>
      with message key <code>${esc(a.id)}</code>.</p>`;
}
// ------------------------------------------------------------------ inspector (extensible section registry)
// The inspector is a list of sections. Each section is registered with registerInspectorSection(spec):
//   id, order, title (string | (a) => string), kinds (optional list of asset kinds),
//   collapsible / collapsed (default state), summary(a, shared) → html shown in the header,
//   actions(a, shared) → [{label, title, primary, run(a, shared)}] header buttons (e.g. "Edit…" opening a modal),
//   mount(a, body, shared, sec) → fills the body (may be async) – called on selection / explicit re-render,
//   refresh(a, body, shared, prev, sec) → optional in-place live update, called every slow refresh.
// A full render is built off-DOM and swapped in at once; live refreshes never rebuild the panel, so the
// layout stays stable while a simulation runs and editing is not interrupted.
const INSPECTOR_SECTIONS = [];
function registerInspectorSection(spec) {
  const i = INSPECTOR_SECTIONS.findIndex((s) => s.id === spec.id);
  if (i >= 0) INSPECTOR_SECTIONS.splice(i, 1);
  INSPECTOR_SECTIONS.push(spec);
  INSPECTOR_SECTIONS.sort((x, y) => (x.order ?? 50) - (y.order ?? 50));
}
let insSeq = 0, insBusy = false, insView = null, insPending = null;   // insView: {id, a, secs, shared}
const encId = (id) => encodeURIComponent(id);

function select(id) { selectedId = id; inspectorDirty = false; showTab("inspector"); renderInspector(true); }
async function renderInspector(force = false) {
  const el = $("#tab-inspector");
  if (!selectedId) { insView = null; insSeq++; el.dataset.assetId = ""; el.innerHTML = '<p class="hint">Select an asset on the canvas.</p>'; return; }
  if (!force && insPending === selectedId) return;
  if (force || !insView || insView.id !== selectedId) return fullInspectorRender(el);
  return liveInspectorRefresh();
}
function sectionShell(spec, a, shared) {
  const key = `ins:${spec.id}`;
  const collapsed = spec.collapsible ? !(SECTION_OPEN[key] ?? !spec.collapsed) : false;
  const title = h("h4", {}, typeof spec.title === "function" ? spec.title(a) : spec.title || "");
  const sum = h("span", { class: "ins-sum" });
  const acts = h("span", { class: "ins-actions" });
  const body = h("div", { class: `ins-body ${spec.bodyClass || ""}` });
  const el = h("section", { class: `ins-sec${collapsed ? " collapsed" : ""}${spec.collapsible ? " collapsible" : ""}`, "data-sec": spec.id },
    h("header", {}, title, sum, acts), body);
  for (const act of spec.actions?.(a, shared) || []) {
    acts.append(h("button", { type: "button", class: `small${act.primary ? " primary" : ""}`, title: act.title || "",
      onclick: (ev) => { ev.stopPropagation(); Promise.resolve(act.run(insView?.a || a, shared)).catch((e) => toast(e.message)); } }, act.label));
  }
  if (spec.collapsible) {
    el.querySelector("header").addEventListener("click", (ev) => {
      if (ev.target.closest("button, input, select, a")) return;
      el.classList.toggle("collapsed");
      SECTION_OPEN[key] = !el.classList.contains("collapsed");
    });
  }
  const sec = { spec, el, body, sum, title };
  sec.setSummary = (x) => { const html = spec.summary ? spec.summary(x, shared) || "" : ""; if (sum.innerHTML !== html) sum.innerHTML = html; };
  sec.setSummary(a);
  return sec;
}
async function fullInspectorRender(el) {
  const tok = ++insSeq;
  const id = selectedId;
  insPending = id;
  try { await buildInspector(el, tok, id); } finally { if (tok === insSeq) insPending = null; }
}
async function buildInspector(el, tok, id) {
  let a;
  try { a = await api("GET", `/api/assets/${encId(id)}`); }
  catch (e) { if (tok === insSeq) el.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  const [ctx, integ] = await Promise.all([loadCtx().catch(() => ({ types: [], recipes: [], orders: [] })),
    api("GET", "/api/integration").catch(() => null)]);
  if (tok !== insSeq) return;
  const shared = { ctx, integ };
  const root = h("div", { class: "inspector" });
  const secs = INSPECTOR_SECTIONS.filter((s) => !s.kinds || s.kinds.includes(a.kind)).map((spec) => sectionShell(spec, a, shared));
  secs.forEach((s) => root.append(s.el));
  await Promise.all(secs.map((s) => Promise.resolve().then(() => s.spec.mount?.(a, s.body, shared, s))
    .catch((e) => (s.body.innerHTML = `<p class="hint bad">${esc(e.message)}</p>`))));
  if (tok !== insSeq) return;
  const scroll = el.dataset.assetId === a.id ? el.scrollTop : 0;
  el.replaceChildren(root);
  el.dataset.assetId = a.id;
  el.scrollTop = scroll;
  insView = { id: a.id, a, secs, shared };
}
async function liveInspectorRefresh() {
  if (insBusy || !insView) return;
  insBusy = true;
  const tok = insSeq, view = insView;
  try {
    const a = await api("GET", `/api/assets/${encId(view.id)}`);
    if (tok !== insSeq) return;
    const prev = view.a;
    view.a = a;
    await Promise.all(view.secs.map(async (s) => {
      s.setSummary(a);
      const t = typeof s.spec.title === "function" ? s.spec.title(a) : null;
      if (t !== null && s.title.textContent !== t) s.title.textContent = t;
      if (s.spec.refresh) await Promise.resolve(s.spec.refresh(a, s.body, view.shared, prev, s)).catch(() => {});
    }));
  } catch (e) { /* asset gone: refresh() deselects it */ }
  finally { insBusy = false; }
}
const setHtml = (el, html) => { if (el && el.innerHTML !== html) el.innerHTML = html; };

// ---- value formatting (compact, never widens the panel)
function compactValue(v) {
  if (v === null || v === undefined || v === "") return '<span class="muted">–</span>';
  if (typeof v === "number") return esc(Number.isInteger(v) ? v : +v.toFixed(3));
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return `<span class="chip" title="${esc(JSON.stringify(v))}">${v.length} item${v.length === 1 ? "" : "s"}</span>`;
  if (typeof v === "object") {
    const n = Object.keys(v).length;
    return `<span class="chip" title="${esc(JSON.stringify(v, null, 1))}">${n} field${n === 1 ? "" : "s"}</span>`;
  }
  return `<span class="trunc" title="${esc(v)}">${esc(v)}</span>`;
}
function cycleCard(c, now) {
  if (!c) return '<div class="cycle-card idle"><span class="muted">No active cycle</span></div>';
  const span = (c.scheduled_end ?? now) - c.started_at;
  const p = span > 0 ? Math.max(0, Math.min(1, (now - c.started_at) / span)) : 0;
  return `<div class="cycle-card"><div class="cycle-head"><span class="trunc" title="${esc(c.id)}"><b>${esc(c.id)}</b></span>
      <span class="badge running">${esc(c.status)}</span></div>
    <div class="bar"><div style="width:${(p * 100).toFixed(1)}%"></div></div>
    <div class="cycle-meta"><span>lot <b class="trunc" title="${esc(c.lot_number)}">${esc(c.lot_number)}</b></span>
      <span>t ${fmt(c.started_at)} → ${fmt(c.scheduled_end)}</span><span>${(c.input_ids || []).length} in</span>
      ${c.recipe_id ? `<span>${esc(c.recipe_id)}</span>` : ""}</div></div>`;
}
function statsHtml(stats) {
  const s = stats || {};
  const hasCycle = "active_cycle" in s;
  const rows = Object.entries(s).filter(([k]) => k !== "active_cycle")
    .map(([k, v]) => `<div>${esc(k.replace(/_/g, " "))}</div><div>${compactValue(v)}</div>`).join("");
  return `${hasCycle ? `<div class="sub">Active cycle</div>${cycleCard(s.active_cycle, state?.sim?.sim_time ?? 0)}` : ""}
    <div class="kv stats">${rows || '<div class="muted">no statistics</div>'}</div>`;
}

// ---- configuration helpers
function configPatch(a, next) {
  const patch = {};
  for (const [k, v] of Object.entries(next)) if (JSON.stringify(v) !== JSON.stringify(a.config[k])) patch[k] = v;
  return patch;
}
async function applyConfigPatch(a, next) {
  const patch = configPatch(a, next);
  if (!Object.keys(patch).length) { toast("No changes", true); return null; }
  const r = await api("PATCH", `/api/assets/${encId(a.id)}/config`, patch);
  inspectorDirty = false;
  toast(`Applied (v${r.version}) – effective: ${r.effective}`, true);
  await refresh(); renderInspector(true);
  return r;
}
const flatFields = (fields) => fields.flatMap((f) => (f.row ? f.fields : f.section ? flatFields(f.fields) : [f]));
function sectionSummary(fields, cfg, ctx) {
  return flatFields(fields).map((f) => {
    const v = cfg[f.key];
    let txt;
    if (Array.isArray(v)) txt = `${v.length}`;
    else if (v && typeof v === "object") txt = f.key === "speed_model" ? `${(v.points || []).length} pts` : "set";
    else if (f.type === "select") {
      const opts = typeof f.options === "function" ? f.options(ctx) : f.options || [];
      const o = opts.find((x) => (Array.isArray(x) ? x[0] : x) === v);
      txt = o ? (Array.isArray(o) ? o[1] : o) : showValue(v);
    } else txt = showValue(v);
    return `${f.label || f.key}: ${txt === "" ? "–" : txt}`;
  }).join(" · ");
}
function openConfigSectionDialog(a, sec, ctx) {
  openFormDialog({ title: `${a.name} · ${sec.section}`, subtitle: "Changes are applied to the running asset and versioned.",
    fields: sec.fields, value: a.config, ctx, submitLabel: "Apply",
    onSubmit: async (v) => { await applyConfigPatch(a, v); } });
}
function openRawConfigDialog(a) {
  openJsonDialog({ title: `${a.name} · configuration (JSON)`, value: a.config, submitLabel: "Apply",
    subtitle: "Advanced: edit the complete configuration. Only changed keys are sent (PATCH) and versioned.",
    onSubmit: async (v) => {
      if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error("configuration must be a JSON object");
      await applyConfigPatch(a, v);
    } });
}
function openDeviceApiDialog(a, integ) {
  $("#modal-content").innerHTML = `<div class="dialog wide"><h3>API for ${esc(a.name)}</h3>${deviceApiHtml(a, integ)}
    <div class="row end"><button type="button" onclick="closeModal()">Close</button></div></div>`;
  $("#modal-content").querySelector(".dialog").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-copy]"); if (!b) return;
    navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copied", true), () => toast("Copy failed"));
  });
  $("#modal").classList.remove("hidden");
}

// ---- built-in sections
registerInspectorSection({
  id: "overview", order: 0, title: (a) => a.name,
  summary: (a) => `<span class="badge ${esc(a.state)}">${esc(a.state)}</span>`,
  mount(a, body) {
    body.innerHTML = `<div class="kv">
        <div>ID</div><div><code class="trunc" title="${esc(a.id)}">${esc(a.id)}</code></div>
        <div>Kind</div><div>${esc(a.kind)}</div>
        <div>State</div><div class="ins-state"></div>
        <div>Config version</div><div class="ins-ver"></div>
        ${a.kind === "conveyor" ? `<div>From</div><div class="trunc">${esc(a.ports?.source?.asset_id)} · ${esc(a.ports?.source?.port)}</div>
          <div>To</div><div class="trunc">${esc(a.ports?.target?.asset_id)} · ${esc(a.ports?.target?.port)}</div>` : ""}
      </div>
      <div class="row"><input class="ins-name" value="${esc(a.name)}" aria-label="Name"><button data-x="rename">Rename</button></div>
      <div class="row">
        <button data-cmd="start">Start</button><button data-cmd="stop">Stop</button>
        <button data-cmd="fault">Inject fault</button><button data-cmd="restart">Restart</button>
        ${a.kind !== "conveyor" ? `<button data-x="copy" title="Ctrl+C">Copy</button><button data-x="dup" title="Ctrl+D">Duplicate</button>` : ""}
      </div>`;
    this.refresh(a, body);
    body.onclick = (ev) => {
      const b = ev.target.closest("button"); if (!b) return;
      const cur = insView?.a || a;
      if (b.dataset.cmd) return act(() => api("POST", `/api/assets/${encId(cur.id)}/commands`, { command: b.dataset.cmd }), `${b.dataset.cmd} sent`);
      if (b.dataset.x === "rename") return act(async () => {
        await api("POST", `/api/assets/${encId(cur.id)}/rename`, { name: body.querySelector(".ins-name").value });
        renderInspector(true);
      }, "Renamed");
      if (b.dataset.x === "copy") return copyAsset(cur.id);
      if (b.dataset.x === "dup") { copyAsset(cur.id); pasteAsset(); }
    };
  },
  refresh(a, body) {
    setHtml(body.querySelector(".ins-state"), `<span class="badge ${esc(a.state)}">${esc(a.state)}</span>
      ${a.state_reason ? `<span class="trunc muted" title="${esc(a.state_reason)}">${esc(a.state_reason)}</span>` : ""}`);
    setHtml(body.querySelector(".ins-ver"), String(a.config_version));
  },
});

registerInspectorSection({
  id: "config", order: 10, title: "Configuration", summary: () => '<span class="hint">versioned</span>',
  actions: (a) => [{ label: "{ } JSON…", title: "Advanced: edit the raw configuration as JSON", run: (cur) => openRawConfigDialog(cur) }],
  mount(a, body, shared, sec) {
    const schema = ASSET_SCHEMAS[a.kind] || [];
    const basic = schema.filter((f) => !f.section), advanced = schema.filter((f) => f.section);
    const form = buildForm(basic, a.config, shared.ctx, ASSET_PREVIEWS[a.kind]);
    const notice = h("div", { class: "notice hidden" }, "Configuration changed elsewhere. ", h("button", { type: "button", class: "small",
      onclick: () => { inspectorDirty = false; renderInspector(true); } }, "Reload"));
    const adv = h("div", { class: "adv-list" }, advanced.map((s) => h("div", { class: "adv-row" },
      h("span", { class: "adv-name" }, s.section),
      h("span", { class: "adv-sum", title: sectionSummary(s.fields, a.config, shared.ctx) }, sectionSummary(s.fields, a.config, shared.ctx)),
      h("button", { type: "button", class: "small", onclick: () => openConfigSectionDialog(insView?.a || a, s, shared.ctx) }, "Edit…"))));
    const applyBtn = h("button", { type: "button", class: "primary", onclick: () => applyConfigPatch(insView?.a || a, form.get()).catch((e) => toast(e.message)) }, "Apply changes");
    const revert = h("button", { type: "button", onclick: () => { inspectorDirty = false; renderInspector(true); } }, "Revert");
    body.replaceChildren(notice, form.el, h("div", { class: "row" }, applyBtn, revert), advanced.length ? adv : "");
    for (const ev of ["input", "change"]) form.el.addEventListener(ev, () => (inspectorDirty = true));
    sec.version = a.config_version;
    sec.notice = notice;
  },
  refresh(a, body, shared, prev, sec) {
    if (a.config_version === sec.version) return;
    if (inspectorDirty || body.contains(document.activeElement)) { sec.notice.classList.remove("hidden"); return; }
    this.mount(a, body, shared, sec);
  },
});

registerInspectorSection({
  id: "sink-products", order: 35, kinds: ["sink"], title: "Received products", collapsible: true, collapsed: true,
  summary: (a) => `<span class="muted">${a.counts?.received ?? 0} received</span>`,
  mount(a, body) {
    body.innerHTML = `<div class="row"><select class="sink-filter"><option value="">all</option><option value="completed=true">completed</option>
      <option value="completed=false">stored</option><option value="defective=true">defective</option></select><button class="sink-list">List</button></div>
      <div class="log sink-products"></div>`;
    body.querySelector(".sink-list").onclick = async () => {
      const q = body.querySelector(".sink-filter").value;
      const ps = await api("GET", `/api/assets/${encId(a.id)}/products?limit=200${q ? "&" + q : ""}`).catch((e) => toast(e.message));
      body.querySelector(".sink-products").innerHTML = (ps || []).map(productRow).join("") || '<p class="hint">none</p>';
    };
  },
});

registerInspectorSection({
  id: "stats", order: 40, title: "Statistics", collapsible: true,
  mount(a, body) { body.innerHTML = statsHtml(a.stats); },
  refresh(a, body) { setHtml(body, statsHtml(a.stats)); },
});

async function opsHtml(a) {
  const ops = await api("GET", `/api/assets/${encId(a.id)}/operational-history?limit=15`).catch(() => []);
  return `<table class="hist fixed"><colgroup><col style="width:28%"><col style="width:16%"><col style="width:16%"><col></colgroup>
    <tr><th>state</th><th>start</th><th>end</th><th>reason</th></tr>
    ${ops.slice().reverse().map((o) => `<tr><td>${esc(o.state)}</td><td>${fmt(o.start)}</td><td>${fmt(o.end)}</td>
      <td class="trunc" title="${esc(o.reason || "")}">${esc(o.reason || "")}</td></tr>`).join("")}</table>`;
}
registerInspectorSection({
  id: "ops", order: 50, title: "Operational history", collapsible: true, collapsed: true,
  async mount(a, body) { body.innerHTML = await opsHtml(a); },
  async refresh(a, body, shared, prev, sec) { if (!sec.el.classList.contains("collapsed")) setHtml(body, await opsHtml(a)); },
});

async function changesHtml(a) {
  const changes = await api("GET", `/api/config-changes?asset_id=${encId(a.id)}&limit=10`).catch(() => []);
  return changes.length ? changes.slice().reverse().map((c) => `<div class="change"><b>v${c.version}</b> t=${fmt(c.timestamp)}
      ${Object.keys(c.changes || {}).map((k) => `<span class="chip" title="${esc(JSON.stringify(c.changes[k]))}">${esc(k)}</span>`).join("")}
      <div class="hint">${esc(c.effective || "")}</div></div>`).join("") : '<p class="hint">none</p>';
}
registerInspectorSection({
  id: "changes", order: 60, title: "Configuration changes", collapsible: true, collapsed: true,
  summary: (a) => `<span class="muted">v${a.config_version}</span>`,
  async mount(a, body, shared, sec) { body.innerHTML = await changesHtml(a); sec.version = a.config_version; },
  async refresh(a, body, shared, prev, sec) {
    if (a.config_version !== sec.version) { sec.version = a.config_version; setHtml(body, await changesHtml(a)); }
  },
});

registerInspectorSection({
  id: "api", order: 70, title: "API for this device",
  actions: () => [{ label: "Details…", title: "REST endpoints, curl commands and broker topics", run: (cur, shared) => openDeviceApiDialog(cur, shared.integ) }],
  mount(a, body) {
    body.innerHTML = `<p class="hint">REST <code class="trunc">/api/assets/${esc(encId(a.id))}</code> · interactive reference at
      <a href="/docs" target="_blank" rel="noopener">/docs</a></p>`;
  },
});

// ------------------------------------------------------------------ DPP viewer
function productRow(p) {
  return `<div class="ev"><span class="link" data-pid="${esc(p.id)}">${esc(p.label)}</span> ${esc(p.status)}
    ${p.is_defective ? '<b style="color:#ff6b6b">DEFECT</b>' : ""} q=${p.quality_score ?? "–"} · ${esc(p.lot_number)}
    · @${esc(p.location?.asset_id || "")}</div>`;
}
document.body.addEventListener("click", (ev) => {
  const pid = ev.target?.dataset?.pid;
  if (pid) openProduct(pid);
});
function lineageHtml(n) {
  return `<li><span class="link" data-pid="${esc(n.id)}">${esc(n.label)}</span> (${esc(n.product_type_id)}${n.is_defective ? ", defective" : ""})
    ${n.parents && n.parents.length ? `<ul class="tree">${n.parents.map(lineageHtml).join("")}</ul>` : ""}${n.truncated ? " …" : ""}</li>`;
}
// Inherited entries from the same batch/cycle differ only by product: collapse them into one row.
function groupHistory(history) {
  const groups = [], byKey = new Map();
  for (const h of history) {
    const { is_defective, ...rest } = h.attributes || {};
    const key = h.inherited ? JSON.stringify([h.activity, h.activity_type, h.timestamp_start, h.timestamp_end,
      h.case_id, h.cycle_id, rest]) : h.entry_id;
    let g = byKey.get(key);
    if (!g) { g = { ...h, attributes: rest, products: [], defective: 0 }; byKey.set(key, g); groups.push(g); }
    g.products.push(h.product_id);
    g.defective += is_defective ? 1 : 0;
  }
  return groups;
}
function attrValue(v) {
  if (v && typeof v === "object") {
    const n = Array.isArray(v) ? v.length : Object.keys(v).length;
    const s = JSON.stringify(v);
    return s.length <= 40 ? esc(s) : `<span title="${esc(s)}">{${n} entries}</span>`;
  }
  return esc(typeof v === "number" ? +v.toFixed(4) : v);
}
const HIDDEN_ATTRS = new Set(["activity_name"]);
function attrChips(attrs) {
  return Object.entries(attrs || {}).filter(([k, v]) => !HIDDEN_ATTRS.has(k) && v !== null && v !== undefined)
    .map(([k, v]) => `<span class="chip"><b>${esc(k.replace(/_/g, " "))}</b> ${attrValue(v)}</span>`).join("");
}
function historyHtml(history) {
  const groups = groupHistory(history);
  return `<h4>History (${groups.length} steps from ${history.length} entries; inherited steps in italics)</h4>
    <table class="hist"><tr><th>activity</th><th>type</th><th>start</th><th>end</th><th>case</th><th>product(s)</th><th>attributes</th></tr>
    ${groups.map((g) => {
      const own = !g.inherited;
      const prod = own ? "self" : g.products.length === 1
        ? `<span class="link" data-pid="${esc(g.products[0])}">${esc(g.products[0].slice(0, 12))}…</span>`
        : `<details><summary>${g.products.length} products${g.defective ? ` · ${g.defective} defective` : ""}</summary>
            ${g.products.map((p) => `<span class="link" data-pid="${esc(p)}">${esc(p.slice(0, 12))}…</span>`).join(" ")}</details>`;
      const defect = g.products.length === 1 && g.defective ? '<span class="chip bad">defective</span>' : "";
      return `<tr class="${own ? "" : "inherited"}"><td title="${esc(g.attributes.activity_name || "")}">${esc(g.activity)}</td><td>${esc(g.activity_type)}</td>
        <td>${fmt(g.timestamp_start)}</td><td>${fmt(g.timestamp_end)}</td><td>${esc(g.case_id)}</td><td>${prod}</td>
        <td class="chips">${defect}${attrChips(g.attributes)}</td></tr>`;
    }).join("")}</table>`;
}
async function openProduct(pid) {
  let r;
  try { r = await api("GET", `/api/products/${encodeURIComponent(pid)}`); } catch (e) { return toast(e.message); }
  const id = r.identity, cs = r.current_state, pv = r.provenance;
  const children = pv.child_product_ids || [];
  $("#modal-content").innerHTML = `
    <h3>Digital Product Passport · ${esc(id.label)}</h3>
    <div class="kv">${Object.entries(id).map(([k, v]) => `<div>${esc(k)}</div><div>${esc(v)}</div>`).join("")}
      <div>status</div><div>${esc(cs.status)}</div>
      <div>location</div><div>${esc(JSON.stringify(cs.location))}</div>
      <div>defective</div><div>${cs.is_defective}</div>
      <div>quality score</div><div>${cs.quality_score ?? "–"}</div>
      <div>orders</div><div>${esc((pv.production_order_ids || []).join(", "))}</div>
      <div>origin cycle</div><div>${esc(pv.origin_cycle_id || "")}</div>
      <div>children</div><div>${children.map((c) => `<span class="link" data-pid="${esc(c)}">${esc(c.slice(0, 12))}…</span>`).join(" ") || "–"}</div>
    </div>
    <h4>Lineage</h4><ul class="tree">${lineageHtml(r.lineage)}</ul>
    ${historyHtml(r.history)}
    <h4>Raw record</h4><pre>${esc(JSON.stringify(r, null, 1)).slice(0, 20000)}</pre>`;
  $("#modal").classList.remove("hidden");
}
$("#modal-close").onclick = () => $("#modal").classList.add("hidden");
$("#modal").addEventListener("click", (ev) => { if (ev.target.id === "modal") $("#modal").classList.add("hidden"); });

// ------------------------------------------------------------------ tabs
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "catalog") renderCatalog();
  if (name === "integration") renderIntegration();
}
document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

// ------------------------------------------------------------------ events
async function pollEvents() {
  try {
    const evs = await api("GET", `/api/events?since_seq=${lastEventSeq}&limit=500`);
    if (evs.length) {
      lastEventSeq = evs[evs.length - 1].seq;
      eventBuffer = eventBuffer.concat(evs).slice(-400);
      renderEvents();
    }
  } catch (e) { /* ignore transient errors */ }
}
const WARN = new Set(["fault_detected", "machine_stopped", "conveyor_blocked", "asset_blocked", "product_rejected", "cycle_cancelled", "production_skipped"]);
function renderEvents() {
  const f = $("#ev-filter").value.trim().toLowerCase();
  const list = eventBuffer.filter((e) => !f || e.asset_id.toLowerCase().includes(f) || e.event_type.includes(f));
  $("#event-log").innerHTML = list.slice(-200).reverse().map((e) => {
    const prods = (e.output_product_ids || []).concat(e.input_product_ids || []).slice(0, 3);
    return `<div class="ev ${WARN.has(e.event_type) ? "warn" : ""}"><span class="t">${fmt(e.timestamp)}</span>
      <span class="ty">${esc(e.event_type)}</span> ${esc(e.asset_id)} ${e.error ? esc(e.error) : ""}
      ${e.defect_count ? `defects=${e.defect_count}` : ""}
      ${prods.map((p) => `<span class="link" data-pid="${esc(p)}">${esc(p.slice(4, 12))}</span>`).join(" ")}</div>`;
  }).join("");
}
$("#ev-filter").addEventListener("input", renderEvents);

// ------------------------------------------------------------------ products tab
$("#pf-search").onclick = async () => {
  const q = new URLSearchParams({ limit: "200" });
  if ($("#pf-status").value) q.set("status", $("#pf-status").value);
  if ($("#pf-type").value) q.set("product_type_id", $("#pf-type").value);
  if ($("#pf-defective").value) q.set("defective", $("#pf-defective").value);
  try { $("#product-list").innerHTML = (await api("GET", `/api/products?${q}`)).map(productRow).join("") || '<p class="hint">none</p>'; }
  catch (e) { toast(e.message); }
};
$("#pf-open").onclick = () => $("#pf-id").value && openProduct($("#pf-id").value.trim());

// ------------------------------------------------------------------ catalog
function catalogCard(title, sub, body, kind, item, canDelete = true) {
  return `<div class="card"><div class="card-head"><b>${esc(title)}</b> <span class="muted">${esc(sub)}</span>
    <span class="card-actions"><button class="small" data-edit="${kind}" data-id="${esc(item.id)}">Edit</button>
    ${canDelete ? `<button class="small del" data-del="${kind}" data-id="${esc(item.id)}">Delete</button>` : ""}</span></div>${body}</div>`;
}
const chips = (obj) => Object.entries(obj || {}).map(([k, v]) => `<span class="chip">${esc(k)}: ${esc(showValue(v))}</span>`).join(" ");
let catalogCache = null;
async function renderCatalog() {
  const ctx = catalogCache = await loadCtx();
  $("#types-list").innerHTML = ctx.types.map((t) => catalogCard(t.name, t.id,
    `${t.description ? `<div>${esc(t.description)}</div>` : ""}${chips(t.metadata)}`, "type", t)).join("") || '<p class="hint">No product types yet.</p>';
  $("#recipes-list").innerHTML = ctx.recipes.map((r) => catalogCard(r.name, r.id,
    `<div class="flow"><span>${esc(itemsText(ctx, r.inputs))}</span><span class="arrow">→</span><span>${esc(itemsText(ctx, r.outputs))}</span></div>
     <div class="muted">${r.processing_time_s} s · ${r.defect_policy === "any_defective_input" ? "inherits input defects" : "machine quality"}</div>
     ${r.description ? `<div>${esc(r.description)}</div>` : ""}${chips(r.parameters)}`, "recipe", r)).join("") || '<p class="hint">No recipes yet.</p>';
  $("#orders-list").innerHTML = ctx.orders.map((o) => catalogCard(o.name, o.id,
    `${o.description ? `<div>${esc(o.description)}</div>` : ""}${chips(o.attributes)}`, "order", o, o.id !== "order-default")).join("");
}
const CATALOG_KEYS = { type: "types", recipe: "recipes", order: "orders" };
$("#tab-catalog").addEventListener("click", (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  if (b.dataset.new) return openCatalogDialog(b.dataset.new).catch((e) => toast(e.message));
  const kind = b.dataset.edit || b.dataset.del;
  if (!kind) return;
  const item = catalogCache?.[CATALOG_KEYS[kind]].find((x) => x.id === b.dataset.id);
  if (b.dataset.edit) return openCatalogDialog(kind, item).catch((e) => toast(e.message));
  if (confirm(`Delete ${CATALOG[kind].title} '${b.dataset.id}'?`))
    act(async () => { await api("DELETE", `${CATALOG[kind].path}/${encodeURIComponent(b.dataset.id)}`); renderCatalog(); }, "Deleted");
});

// ------------------------------------------------------------------ integration
let intForm = null;
function buildIntegrationForm(adapter, options) {
  intForm = buildForm(INTEGRATION_FIELDS[adapter] || [], { ...INTEGRATION_DEFAULTS[adapter], ...(options || {}) }, {});
  $("#int-options").replaceChildren(intForm.el);
  $("#int-common").style.display = adapter === "none" ? "none" : "";
}
async function renderIntegration() {
  const info = await api("GET", "/api/integration");
  const cur = info.adapter || "none";
  if (!intForm) { $("#int-adapter").value = INTEGRATION_FIELDS[cur] ? cur : "memory"; buildIntegrationForm($("#int-adapter").value); }
  const st = info.adapter ? [`<b>${esc(info.adapter)}</b> active`, ...Object.entries(info).filter(([k]) => k !== "adapter")
    .map(([k, v]) => `${esc(k)}: ${esc(showValue(v))}`)] : ["<b>disabled</b>"];
  $("#int-info").innerHTML = st.map((s) => `<div>${s}</div>`).join("");
}
$("#int-adapter").onchange = () => buildIntegrationForm($("#int-adapter").value);
$("#btn-int-apply").onclick = () => act(async () => {
  const cats = [...document.querySelectorAll("#int-cats input:checked")].map((c) => c.value);
  if (!cats.length) throw new Error("select at least one event category");
  await api("POST", "/api/integration", { adapter: $("#int-adapter").value, options: intForm ? intForm.get() : {},
    topic_prefix: $("#int-prefix").value.trim() || "industrial-sim", categories: cats });
  renderIntegration();
}, "Integration updated");
$("#btn-int-msgs").onclick = async () => {
  try { $("#int-msgs").textContent = JSON.stringify(await api("GET", "/api/integration/messages?limit=20"), null, 1); }
  catch (e) { toast(e.message); }
};

// ------------------------------------------------------------------ toolbar
const simCmd = (c, body) => act(() => api("POST", `/api/sim/${c}`, body));
$("#btn-start").onclick = () => simCmd("start");
$("#btn-pause").onclick = () => simCmd("pause");
$("#btn-resume").onclick = () => simCmd("resume");
$("#btn-stop").onclick = () => simCmd("stop");
$("#btn-reset").onclick = () => { eventBuffer = []; lastEventSeq = 0; simCmd("reset"); };
$("#btn-step").onclick = () => simCmd("step", { seconds: Number($("#step-seconds").value) });
$("#accel").onchange = () => simCmd("acceleration", { factor: Number($("#accel").value) });
// ---- scenario files: New (empty plant, keeps catalogues), Save, Save as, Rename
let currentScenario = { id: null, name: "", saved: false };
async function refreshScenarioInfo() {
  const [cur, scs] = await Promise.all([api("GET", "/api/scenarios/current").catch(() => null), api("GET", "/api/scenarios").catch(() => [])]);
  if (cur) currentScenario = cur;
  const sel = $("#scenario-select"), keep = sel.value;
  sel.innerHTML = scs.map((s) => `<option value="${esc(s.id)}">${esc(s.name || s.id)} (${esc(s.id)})</option>`).join("");
  sel.value = scs.some((s) => s.id === currentScenario.id) ? currentScenario.id : keep;
  $("#scenario-name").innerHTML = `<b>${esc(currentScenario.name || "Untitled")}</b> <span class="muted">${
    currentScenario.id ? esc(currentScenario.id) + ".json" : "unsaved"}</span>`;
  $("#scenario-name").title = currentScenario.description || "Current scenario";
}
const resetView = () => { eventBuffer = []; lastEventSeq = 0; select(null); needsFit = true; userView = false; view = { ox: 0, oy: 0, s: 1 }; };
function wireSlug(box) {
  const [nameIn, idIn] = box.querySelectorAll(".form > .f input");
  let touched = false;
  idIn.addEventListener("input", () => (touched = idIn.value !== ""));
  nameIn.addEventListener("input", () => { if (!touched) idIn.value = slug(nameIn.value).replace(/-/g, "_"); });
}
$("#btn-new").onclick = () => openFormDialog({
  title: "New scenario", submitLabel: "Create",
  subtitle: "Starts a fresh, empty plant in a new scenario file. The current plant is not saved automatically.",
  fields: [{ key: "name", label: "Name", type: "text", placeholder: "e.g. Rolling mill line 2" },
    { key: "id", label: "File", type: "text", placeholder: "auto from name", help: "scenarios/<file>.json · letters, digits, - and _" },
    { key: "description", label: "Description", type: "text", multiline: true },
    { key: "seed", label: "Random seed", type: "number", int: true, help: "Empty = keep the current seed" },
    { key: "keep_catalog", label: "Catalogues", type: "bool", checkLabel: "keep product types, recipes and production orders" },
    { key: "keep_asset_types", label: "Asset types", type: "bool", checkLabel: "keep asset types (templates)" }],
  value: { name: "", keep_catalog: true, keep_asset_types: true }, ctx: {},
  onBuilt: (f, box) => wireSlug(box),
  onSubmit: async (v) => {
    if (!v.name) throw new Error("name is required");
    const body = { ...v, id: v.id || undefined };
    if (body.seed === null) delete body.seed;
    const r = await api("POST", "/api/scenarios", body);
    resetView(); await refresh(); await refreshScenarioInfo(); if (typeof renderAssetTypes === "function") renderAssetTypes();
    toast(`Scenario '${r.name}' created (${r.id}.json): add assets from the palette`, true);
  },
});
function saveAsDialog() {
  openFormDialog({
    title: "Save scenario as", submitLabel: "Save",
    fields: [{ key: "name", label: "Name", type: "text" },
      { key: "id", label: "File", type: "text", placeholder: "auto from name", help: "scenarios/<file>.json" },
      { key: "overwrite", label: "", type: "bool", checkLabel: "overwrite an existing file" }],
    value: { name: currentScenario.name, id: "", overwrite: false }, ctx: {},
    onBuilt: (f, box) => wireSlug(box),
    onSubmit: async (v) => {
      const id = v.id || slug(v.name || "").replace(/-/g, "_");
      if (!id) throw new Error("name or file is required");
      await api("PUT", `/api/scenarios/${encodeURIComponent(id)}?overwrite=${!!v.overwrite}`, { name: v.name || undefined });
      await refreshScenarioInfo(); toast(`Saved to ${id}.json`, true);
    },
  });
}
$("#btn-save").onclick = () => {
  if (!currentScenario.id) return saveAsDialog();
  act(async () => { await api("PUT", `/api/scenarios/${encodeURIComponent(currentScenario.id)}`); await refreshScenarioInfo(); },
    `Saved ${currentScenario.id}.json`);
};
$("#btn-save-as").onclick = saveAsDialog;
$("#btn-rename").onclick = () => {
  if (!currentScenario.id) return saveAsDialog();
  openFormDialog({
    title: "Rename scenario", submitLabel: "Rename",
    subtitle: "Changes the display name and/or the file name. Unsaved changes to the plant are not written.",
    fields: [{ key: "name", label: "Name", type: "text" }, { key: "new_id", label: "File", type: "text", help: "scenarios/<file>.json" }],
    value: { name: currentScenario.name, new_id: currentScenario.id }, ctx: {},
    onSubmit: async (v) => {
      const r = await api("POST", `/api/scenarios/${encodeURIComponent(currentScenario.id)}/rename`,
        { name: v.name || undefined, new_id: v.new_id || undefined });
      await refreshScenarioInfo(); toast(`Renamed to '${r.name}' (${r.id}.json)`, true);
    },
  });
};
$("#btn-load-scenario").onclick = () => act(async () => {
  await api("POST", `/api/scenarios/${encodeURIComponent($("#scenario-select").value)}/load`);
  eventBuffer = []; lastEventSeq = 0; select(null); needsFit = true;
  await refreshScenarioInfo();
}, "Scenario loaded");
$("#btn-export").onclick = async () => {
  const data = await api("GET", "/api/scenario");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${data.name || "scenario"}.json`; a.click();
};
$("#file-import").onchange = async (ev) => {
  const f = ev.target.files[0]; if (!f) return;
  let data; try { data = JSON.parse(await f.text()); } catch (e) { return toast("Invalid JSON file"); }
  act(async () => { await api("POST", "/api/scenario", data); eventBuffer = []; lastEventSeq = 0; select(null); needsFit = true; await refreshScenarioInfo(); },
    "Scenario imported (unsaved: use Save as to keep it)");
  ev.target.value = "";
};
$("#btn-ckpt-save").onclick = () => act(() => api("POST", `/api/checkpoints/${encodeURIComponent($("#ckpt-name").value)}`), "Checkpoint saved");
$("#btn-ckpt-restore").onclick = () => act(async () => {
  await api("POST", `/api/checkpoints/${encodeURIComponent($("#ckpt-name").value)}/restore`);
  eventBuffer = []; lastEventSeq = 0;
}, "Checkpoint restored");

// ------------------------------------------------------------------ polling
async function refresh() {
  try {
    const s = await api("GET", "/api/state");
    if (drag && state) {  // keep local position of the asset being dragged
      const local = assetsById()[drag.id];
      const remote = s.assets.find((a) => a.id === drag.id);
      if (local && remote) remote.position = local.position;
    }
    state = s;
    if (s.last_event_seq < lastEventSeq) { lastEventSeq = 0; eventBuffer = []; }
    $("#clock").textContent = `${s.sim.status} · t=${s.sim.sim_time.toFixed(1)}s · ×${s.sim.acceleration}`;
    if (Number($("#accel").value) !== s.sim.acceleration && document.activeElement !== $("#accel")) {
      if (![...$("#accel").options].some((o) => Number(o.value) === s.sim.acceleration)) $("#accel").add(new Option(String(s.sim.acceleration)));
      $("#accel").value = String(s.sim.acceleration);
    }
    $("#metrics").textContent = JSON.stringify(s.metrics, null, 1);
    if (selectedId && !s.assets.some((a) => a.id === selectedId)) select(null);
  } catch (e) { $("#clock").textContent = "backend unreachable"; }
}
async function slowRefresh() {
  try { $("#validation").textContent = JSON.stringify(await api("GET", "/api/validation"), null, 1); } catch (e) { }
  if ($("#tab-inspector").classList.contains("active")) renderInspector(false);
}
async function init() {
  await refreshScenarioInfo();
  resize(); window.addEventListener("resize", resize);
  // Layout can settle after the first fit (fonts, side panels): refit unless the user moved the view.
  new ResizeObserver(() => { resize(); if (!userView) needsFit = true; }).observe(canvas);
  await refresh(); requestAnimationFrame(draw);
  setInterval(refresh, 100);
  setInterval(pollEvents, 500);
  setInterval(slowRefresh, 1500);
}
init();
