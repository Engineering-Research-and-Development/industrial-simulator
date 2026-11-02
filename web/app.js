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
    const mx = (e.a.x + e.b.x) / 2, my = (e.a.y + e.b.y) / 2;
    ctx.fillStyle = "#c9d1dc"; ctx.font = "11px system-ui"; ctx.textAlign = "center";
    ctx.fillText(`${c.name} ${c.counts.occupancy}/${c.counts.capacity}`, mx, my - 8);
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
        ctx.fillStyle = "#8b93a1"; ctx.font = "9px system-ui"; ctx.textAlign = dir === "in" ? "right" : "left";
        ctx.fillText(a.ports.labels?.[p] || p, pp.x + (dir === "in" ? -8 : 8), pp.y - 7);
      }
    }
  }
  ctx.restore();
  requestAnimationFrame(draw);
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
  return `<details class="section" id="ins-api" ${SECTION_OPEN.deviceApi ? "open" : ""}><summary>API for this device</summary>
    <p class="hint">Full interactive reference: <a href="/docs" target="_blank" rel="noopener">${esc(base)}/docs</a> (OpenAPI JSON at <code>/openapi.json</code>).</p>
    <table class="hist api">${rows.map((r) => `<tr><td><span class="method ${r[0].toLowerCase()}">${r[0]}</span></td>
      <td>${r[0] === "GET" ? `<a href="${esc(r[1])}" target="_blank" rel="noopener"><code>${esc(r[1])}</code></a>` : `<code>${esc(r[1])}</code>`}
        <div class="hint">${esc(r[2])}${r[3] ? ` · body <code>${esc(r[3])}</code>` : ""}</div></td>
      <td><button class="small" data-copy="${esc(curl(r))}" title="Copy curl command">curl</button></td></tr>`).join("")}</table>
    <p class="hint">Broker messages ${active ? `(${esc(integ.adapter)} adapter active)` : "(enable an adapter in the Integration tab)"}:
      topics <code>${esc(prefix)}.machine</code>, <code>${esc(prefix)}.product</code>, <code>${esc(prefix)}.config</code>
      with message key <code>${esc(a.id)}</code>.</p></details>`;
}
function select(id) { selectedId = id; inspectorDirty = false; showTab("inspector"); renderInspector(true); }
async function renderInspector(force = false) {
  const el = $("#tab-inspector");
  if (!selectedId) { el.innerHTML = '<p class="hint">Select an asset on the canvas.</p>'; return; }
  if (!force && (inspectorDirty || el.contains(document.activeElement))) return;
  let a;
  try { a = await api("GET", `/api/assets/${encodeURIComponent(selectedId)}`); }
  catch (e) { el.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  if (!force && (inspectorDirty || el.contains(document.activeElement))) return;
  const ops = await api("GET", `/api/assets/${encodeURIComponent(a.id)}/operational-history?limit=15`).catch(() => []);
  const changes = await api("GET", `/api/config-changes?asset_id=${encodeURIComponent(a.id)}&limit=10`).catch(() => []);
  const ctx = await loadCtx().catch(() => ({ types: [], recipes: [], orders: [] }));
  const integ = await api("GET", "/api/integration").catch(() => null);
  if (!force && (inspectorDirty || el.contains(document.activeElement))) return;
  const scroll = el.dataset.assetId === a.id ? el.scrollTop : 0;
  el.dataset.assetId = a.id;
  el.innerHTML = `
    <div class="kv">
      <div>ID</div><div>${esc(a.id)}</div>
      <div>Kind</div><div>${esc(a.kind)}</div>
      <div>State</div><div><span class="badge ${esc(a.state)}">${esc(a.state)}</span> ${esc(a.state_reason || "")}</div>
      <div>Config version</div><div>${a.config_version}</div>
    </div>
    <div class="row"><input id="ins-name" value="${esc(a.name)}"><button id="ins-rename">Rename</button></div>
    <div class="row">
      <button data-cmd="start">Start</button><button data-cmd="stop">Stop</button>
      <button data-cmd="fault">Inject fault</button><button data-cmd="restart">Restart</button>
    </div>
    ${a.kind !== "conveyor" ? `<div class="row"><button id="ins-copy" title="Ctrl+C">Copy</button><button id="ins-dup" title="Ctrl+D">Duplicate</button></div>` : ""}
    ${a.kind === "conveyor" ? `<div class="kv"><div>From</div><div>${esc(a.ports?.source?.asset_id)} · ${esc(a.ports?.source?.port)}</div>
      <div>To</div><div>${esc(a.ports?.target?.asset_id)} · ${esc(a.ports?.target?.port)}</div></div>` : ""}
    <h4>Configuration <span class="hint">· applied changes are versioned</span></h4>
    <div id="ins-form"></div>
    <div class="row"><button id="ins-apply" class="primary">Apply changes</button><button id="ins-revert">Revert</button></div>
    <details class="section" id="ins-raw" ${SECTION_OPEN.rawJson ? "open" : ""}><summary>Raw JSON (advanced)</summary>
      <textarea id="ins-config" rows="10">${esc(JSON.stringify(a.config, null, 2))}</textarea>
      <div class="row"><button id="ins-apply-json">Apply JSON</button></div></details>
    ${a.kind === "sink" ? '<h4>Received products</h4><div class="row"><select id="sink-filter"><option value="">all</option><option value="completed=true">completed</option><option value="completed=false">stored</option><option value="defective=true">defective</option></select><button id="sink-list">List</button></div><div id="sink-products" class="log"></div>' : ""}
    ${deviceApiHtml(a, integ)}
    <div id="ins-industrial" class="industrial"></div>
    <h4>Statistics</h4><div class="kv stats">${Object.entries(a.stats || {}).map(([k, v]) =>
      `<div>${esc(k.replace(/_/g, " "))}</div><div>${esc(typeof v === "number" ? +v.toFixed(3) : showValue(v) || "–")}</div>`).join("")}</div>
    <h4>Operational history (latest)</h4>
    <table class="hist"><tr><th>state</th><th>start</th><th>end</th><th>reason</th></tr>
      ${ops.slice().reverse().map((o) => `<tr><td>${esc(o.state)}</td><td>${fmt(o.start)}</td><td>${fmt(o.end)}</td><td>${esc(o.reason || "")}</td></tr>`).join("")}</table>
    <h4>Configuration changes</h4>
    ${changes.length ? changes.slice().reverse().map((c) => `<pre>t=${fmt(c.timestamp)} v${c.version}: ${esc(JSON.stringify(c.changes))}\n  (${esc(c.effective)})</pre>`).join("") : '<p class="hint">none</p>'}
  `;
  const form = buildForm(ASSET_SCHEMAS[a.kind] || [], a.config, ctx, ASSET_PREVIEWS[a.kind]);
  $("#ins-form").append(form.el);
  $("#ins-raw").addEventListener("toggle", () => (SECTION_OPEN.rawJson = $("#ins-raw").open));
  $("#ins-api").addEventListener("toggle", () => (SECTION_OPEN.deviceApi = $("#ins-api").open));
  $("#ins-api").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-copy]"); if (!b) return;
    navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copied", true), () => toast("Copy failed"));
  });
  if (typeof renderIndustrialPanel === "function") {
    await renderIndustrialPanel(a, $("#ins-industrial")).catch((e) => ($("#ins-industrial").innerHTML = `<p class="hint">${esc(e.message)}</p>`));
  }
  el.scrollTop = scroll;
  for (const ev of ["input", "change"]) {
    $("#ins-form").addEventListener(ev, () => (inspectorDirty = true));
    $("#ins-config").addEventListener(ev, () => (inspectorDirty = true));
  }
  if ($("#ins-copy")) {
    $("#ins-copy").onclick = () => copyAsset(a.id);
    $("#ins-dup").onclick = () => { copyAsset(a.id); pasteAsset(); };
  }
  $("#ins-revert").onclick = () => { inspectorDirty = false; renderInspector(true); };
  $("#ins-rename").onclick = () => act(() => api("POST", `/api/assets/${encodeURIComponent(a.id)}/rename`, { name: $("#ins-name").value }), "Renamed");
  el.querySelectorAll("button[data-cmd]").forEach((b) => (b.onclick = () =>
    act(() => api("POST", `/api/assets/${encodeURIComponent(a.id)}/commands`, { command: b.dataset.cmd }), `${b.dataset.cmd} sent`)));
  const applyConfig = async (next) => {
    const patch = {};
    for (const [k, v] of Object.entries(next)) if (JSON.stringify(v) !== JSON.stringify(a.config[k])) patch[k] = v;
    if (!Object.keys(patch).length) return toast("No changes", true);
    const r = await act(() => api("PATCH", `/api/assets/${encodeURIComponent(a.id)}/config`, patch));
    if (r) { inspectorDirty = false; toast(`Applied (v${r.version}) – effective: ${r.effective}`, true); renderInspector(true); }
  };
  $("#ins-apply").onclick = () => applyConfig(form.get());
  $("#ins-apply-json").onclick = () => {
    let next;
    try { next = JSON.parse($("#ins-config").value); } catch (e) { return toast("Invalid JSON: " + e.message); }
    applyConfig(next);
  };
  if (a.kind === "sink") {
    $("#sink-list").onclick = async () => {
      const q = $("#sink-filter").value;
      const ps = await api("GET", `/api/assets/${encodeURIComponent(a.id)}/products?limit=200${q ? "&" + q : ""}`).catch((e) => toast(e.message));
      $("#sink-products").innerHTML = (ps || []).map(productRow).join("") || '<p class="hint">none</p>';
    };
  }
}

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
$("#btn-load-scenario").onclick = () => act(async () => {
  await api("POST", `/api/scenarios/${encodeURIComponent($("#scenario-select").value)}/load`);
  eventBuffer = []; lastEventSeq = 0; select(null); needsFit = true;
}, "Scenario loaded");
$("#btn-new").onclick = () => {
  if (!confirm("Start an empty plant? The current network is discarded (catalog is kept). Export first if you need it.")) return;
  act(async () => {
    const data = await api("GET", "/api/scenario");
    await api("POST", "/api/scenario", { ...data, name: "Untitled plant", assets: [] });
    eventBuffer = []; lastEventSeq = 0; select(null); needsFit = true; userView = false; view = { ox: 0, oy: 0, s: 1 };
  }, "Empty plant created: add assets from the palette");
};
$("#btn-export").onclick = async () => {
  const data = await api("GET", "/api/scenario");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${data.name || "scenario"}.json`; a.click();
};
$("#file-import").onchange = async (ev) => {
  const f = ev.target.files[0]; if (!f) return;
  let data; try { data = JSON.parse(await f.text()); } catch (e) { return toast("Invalid JSON file"); }
  act(async () => { await api("POST", "/api/scenario", data); eventBuffer = []; lastEventSeq = 0; select(null); needsFit = true; }, "Scenario imported");
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
  const scs = await api("GET", "/api/scenarios").catch(() => []);
  $("#scenario-select").innerHTML = scs.map((s) => `<option value="${esc(s.id)}">${esc(s.name || s.id)}</option>`).join("");
  resize(); window.addEventListener("resize", resize);
  // Layout can settle after the first fit (fonts, side panels): refit unless the user moved the view.
  new ResizeObserver(() => { resize(); if (!userView) needsFit = true; }).observe(canvas);
  await refresh(); requestAnimationFrame(draw);
  setInterval(refresh, 100);
  setInterval(pollEvents, 500);
  setInterval(slowRefresh, 1500);
}
init();
