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
const assetsById = () => Object.fromEntries((state?.assets || []).map((a) => [a.id, a]));
const nodes = () => (state?.assets || []).filter((a) => a.kind !== "conveyor");
const conveyors = () => (state?.assets || []).filter((a) => a.kind === "conveyor");

function portPos(asset, port, dir) {
  const list = dir === "in" ? asset.ports.inputs : asset.ports.outputs;
  const i = Math.max(0, list.indexOf(port));
  const y = asset.position.y + (H * (i + 1)) / (list.length + 1);
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
    if (x >= a.position.x && x <= a.position.x + W && y >= a.position.y && y <= a.position.y + H) return a;
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
    ctx.beginPath(); ctx.roundRect(x, y, W, H, 6); ctx.fill(); ctx.stroke();
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
        ctx.fillText(p, pp.x + (dir === "in" ? -8 : 8), pp.y - 7);
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
  const maxX = Math.max(...ns.map((a) => a.position.x + W)), maxY = Math.max(...ns.map((a) => a.position.y + H));
  const s = Math.min(1.5, (r.width - 2 * pad) / Math.max(1, maxX - minX), (r.height - 2 * pad) / Math.max(1, maxY - minY));
  view = { s, ox: pad - minX * s + (r.width - 2 * pad - (maxX - minX) * s) / 2, oy: pad - minY * s + (r.height - 2 * pad - (maxY - minY) * s) / 2 };
}
canvas.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const p = screenXY(ev), k = ev.deltaY < 0 ? 1.1 : 1 / 1.1, s = Math.min(4, Math.max(0.2, view.s * k));
  view.ox = p.x - ((p.x - view.ox) * s) / view.s; view.oy = p.y - ((p.y - view.oy) * s) / view.s; view.s = s;
}, { passive: false });
canvas.addEventListener("dblclick", (ev) => { if (!hitNode(canvasXY(ev).x, canvasXY(ev).y)) fitView(); });
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
  if (pan) { const sp = screenXY(ev); view.ox = pan.ox + sp.x - pan.x; view.oy = pan.oy + sp.y - pan.y; return; }
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
window.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") pendingPort = null;
  if (ev.key === "Delete" && selectedId && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
    if (confirm(`Delete ${selectedId}?`)) {
      const id = selectedId; select(null);
      act(() => api("DELETE", `/api/assets/${encodeURIComponent(id)}`), "Deleted");
    }
  }
});

// ------------------------------------------------------------------ palette
const DEFAULT_CONFIG = {
  producer: async () => {
    const types = await api("GET", "/api/product-types");
    if (!types.length) throw new Error("create a product type first (Catalog tab)");
    return { product_type_id: types[0].id, batch_quantity: 1, interval: 10, interval_unit: "seconds" };
  },
  transformer: async () => ({ recipe_id: null, speed_pct: 50 }),
  splitter: async () => ({ mode: "percentage", percentage_a: 50 }),
  sink: async () => ({}),
};
document.querySelectorAll("#palette button[data-kind]").forEach((b) => b.addEventListener("click", () => {
  const kind = b.dataset.kind;
  const n = nodes().length;
  act(async () => {
    const a = await api("POST", "/api/assets", { kind, name: `New ${kind}`, config: await DEFAULT_CONFIG[kind](),
      position: { x: Math.round((40 - view.ox) / view.s) + (n % 5) * 30, y: Math.round((40 - view.oy) / view.s) + (n % 4) * 20 } });
    select(a.id);
  }, `${kind} added`);
}));

// ------------------------------------------------------------------ inspector
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
    <h4>Configuration (edit JSON, then Apply – changes are versioned)</h4>
    <textarea id="ins-config" rows="10">${esc(JSON.stringify(a.config, null, 2))}</textarea>
    <div class="row"><button id="ins-apply" class="primary">Apply config</button><button id="ins-revert">Revert</button></div>
    ${a.kind === "sink" ? '<h4>Received products</h4><div class="row"><select id="sink-filter"><option value="">all</option><option value="completed=true">completed</option><option value="completed=false">stored</option><option value="defective=true">defective</option></select><button id="sink-list">List</button></div><div id="sink-products" class="log"></div>' : ""}
    <h4>Statistics</h4><pre>${esc(JSON.stringify(a.stats, null, 1))}</pre>
    <h4>Operational history (latest)</h4>
    <table class="hist"><tr><th>state</th><th>start</th><th>end</th><th>reason</th></tr>
      ${ops.slice().reverse().map((o) => `<tr><td>${esc(o.state)}</td><td>${fmt(o.start)}</td><td>${fmt(o.end)}</td><td>${esc(o.reason || "")}</td></tr>`).join("")}</table>
    <h4>Configuration changes</h4>
    ${changes.length ? changes.slice().reverse().map((c) => `<pre>t=${fmt(c.timestamp)} v${c.version}: ${esc(JSON.stringify(c.changes))}\n  (${esc(c.effective)})</pre>`).join("") : '<p class="hint">none</p>'}
  `;
  $("#ins-config").addEventListener("input", () => (inspectorDirty = true));
  $("#ins-revert").onclick = () => renderInspector(true);
  $("#ins-rename").onclick = () => act(() => api("POST", `/api/assets/${encodeURIComponent(a.id)}/rename`, { name: $("#ins-name").value }), "Renamed");
  el.querySelectorAll("button[data-cmd]").forEach((b) => (b.onclick = () =>
    act(() => api("POST", `/api/assets/${encodeURIComponent(a.id)}/commands`, { command: b.dataset.cmd }), `${b.dataset.cmd} sent`)));
  $("#ins-apply").onclick = async () => {
    let next;
    try { next = JSON.parse($("#ins-config").value); } catch (e) { return toast("Invalid JSON: " + e.message); }
    const patch = {};
    for (const [k, v] of Object.entries(next)) if (JSON.stringify(v) !== JSON.stringify(a.config[k])) patch[k] = v;
    if (!Object.keys(patch).length) return toast("No changes", true);
    const r = await act(() => api("PATCH", `/api/assets/${encodeURIComponent(a.id)}/config`, patch));
    if (r) { toast(`Applied (v${r.version}) – effective: ${r.effective}`, true); renderInspector(true); }
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
    <h4>History (${r.history.length} entries; inherited entries in italics)</h4>
    <table class="hist"><tr><th>activity</th><th>type</th><th>start</th><th>end</th><th>case</th><th>product</th><th>attributes</th></tr>
    ${r.history.map((h) => `<tr class="${h.inherited ? "inherited" : ""}"><td>${esc(h.activity)}</td><td>${esc(h.activity_type)}</td>
      <td>${fmt(h.timestamp_start)}</td><td>${fmt(h.timestamp_end)}</td><td>${esc(h.case_id)}</td>
      <td>${h.inherited ? `<span class="link" data-pid="${esc(h.product_id)}">${esc(h.product_id.slice(0, 12))}…</span>` : "self"}</td>
      <td><pre>${esc(JSON.stringify(h.attributes))}</pre></td></tr>`).join("")}</table>
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
async function renderCatalog() {
  const [types, recipes, orders] = await Promise.all([api("GET", "/api/product-types"), api("GET", "/api/recipes"), api("GET", "/api/orders")]);
  $("#types-list").innerHTML = types.map((t) => `<pre>${esc(t.id)} – ${esc(t.name)}</pre>`).join("");
  $("#recipes-list").innerHTML = recipes.map((r) => `<pre>${esc(r.id)}: ${r.inputs.map((i) => `${i.quantity}×${i.product_type_id}`).join(" + ")} → ${r.outputs.map((o) => `${o.quantity}×${o.product_type_id}`).join(" + ")} (${r.processing_time_s}s)</pre>`).join("");
  $("#orders-list").innerHTML = orders.map((o) => `<pre>${esc(o.id)} – ${esc(o.description || "")}</pre>`).join("");
}
function jsonFrom(sel) { return JSON.parse($(sel).value); }
$("#btn-add-type").onclick = () => act(async () => { await api("POST", "/api/product-types", jsonFrom("#type-json")); renderCatalog(); }, "Product type added");
$("#btn-add-recipe").onclick = () => act(async () => { await api("POST", "/api/recipes", jsonFrom("#recipe-json")); renderCatalog(); }, "Recipe added");
$("#btn-add-order").onclick = () => act(async () => { await api("POST", "/api/orders", jsonFrom("#order-json")); renderCatalog(); }, "Order added");

// ------------------------------------------------------------------ integration
async function renderIntegration() { $("#int-info").textContent = JSON.stringify(await api("GET", "/api/integration"), null, 1); }
$("#btn-int-apply").onclick = () => act(async () => {
  await api("POST", "/api/integration", { adapter: $("#int-adapter").value, options: jsonFrom("#int-options") });
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
  await refresh(); requestAnimationFrame(draw);
  setInterval(refresh, 100);
  setInterval(pollEvents, 500);
  setInterval(slowRefresh, 1500);
}
init();
