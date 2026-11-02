"use strict";
// Industrial extensions of the UI: asset types, instance profiles (parameters, sensors, composites, conditions,
// behaviors), synchronisation modes, live measurements, external ingestion (REST / MQTT) and dataset sessions.
// Loaded after app.js and forms.js; uses their helpers (api, act, toast, esc, fmt, h, buildForm, …).

const enc = encodeURIComponent;
const PROFILE_SECTIONS = ["parameters", "sensors", "composites", "conditions", "behaviors"];
const OVERRIDABLE = ["sensors", "composites", "conditions", "behaviors"];
const SECTION_ID = { parameters: "name", sensors: "id", composites: "id", conditions: "id", behaviors: "id" };
const PROFILE_SKELETON = {
  parameters: [{ name: "setpoint", type: "number", unit: "degC", default: 900, min: 0, max: 2000 }],
  sensors: [{ id: "temp", unit: "degC", sample_interval_s: 5, external_key: "TT101",
    model: { type: "first_order", target: "param.setpoint", time_constant_s: 30, initial: 20 }, noise_std: 1 }],
  composites: [{ id: "health", expression: "clamp(sensor.temp / param.setpoint, 0, 1)" }],
  conditions: [{ id: "cold", severity: "warning", activation: "sensor.temp < 500", deactivation: "sensor.temp > 520",
    effects: [{ target: "processing_time_s", op: "multiply", value: 1.5 }] }],
  behaviors: [],
};
const EXPR_HELP = "Variables: param.<name>, sensor.<id>, composite.<id>, condition.<id> (active?), feature.<name> (speed_pct, processing_time_s, …), " +
  "state.<state>|operational, cycle.active|elapsed_s|progress, recipe.<parameter>|processing_time_s, config.<field>, t (sim time), self (behaviors / sensor models). " +
  "Functions: min max abs clamp avg sum sqrt exp log log10 round floor ceil pow sin cos tanh sigmoid if_ coalesce isnum. " +
  "Operators: + - * / // % ** and or not, comparisons and 'a if cond else b'.";

const originBadge = (o) => `<span class="origin ${esc(o)}" title="${esc(o)}">${esc(o)}</span>`;
const num = (v, d = 3) => (typeof v === "number" ? +v.toFixed(d) : showValue(v));
const pretty = (v) => JSON.stringify(v, null, 2);
function openSection(key, html, summary, collapsed = true) {
  return `<details class="section" data-sec="${esc(key)}" ${SECTION_OPEN[key] ?? !collapsed ? "open" : ""}><summary>${summary}</summary>${html}</details>`;
}
function bindSections(root) {
  root.querySelectorAll("details[data-sec]").forEach((d) => d.addEventListener("toggle", () => (SECTION_OPEN[d.dataset.sec] = d.open)));
}

// ------------------------------------------------------------------ generic JSON dialog
function openJsonDialog({ title, subtitle, value, rows = 18, submitLabel = "Save", onSubmit, extra }) {
  const ta = h("textarea", { rows, class: "code" });
  ta.value = typeof value === "string" ? value : pretty(value ?? {});
  const err = h("div", { class: "form-error" });
  const submit = h("button", { type: "button", class: "primary" }, submitLabel);
  submit.onclick = async () => {
    err.textContent = "";
    let v;
    try { v = JSON.parse(ta.value); } catch (e) { err.textContent = "Invalid JSON: " + e.message; return; }
    submit.disabled = true;
    try { await onSubmit(v); closeModal(); } catch (e) { err.textContent = e.message; } finally { submit.disabled = false; }
  };
  const box = h("div", { class: "dialog wide" }, h("h3", {}, title), subtitle ? h("p", { class: "hint" }, subtitle) : null,
    ta, extra || null, err, h("div", { class: "row end" }, h("button", { type: "button", onclick: closeModal }, "Cancel"), submit));
  $("#modal-content").replaceChildren(box);
  $("#modal").classList.remove("hidden");
  ta.focus();
}

// expression tester shared by dialogs and the inspector
function expressionTester(scope) {
  const input = h("input", { placeholder: "expression, e.g. sensor.temp * 1.8 + 32", class: "grow" });
  const out = h("div", { class: "help" });
  const run = async () => {
    try {
      const r = await api("POST", "/api/expressions/validate", { expression: input.value, ...scope() });
      out.innerHTML = r.valid
        ? `<span class="ok">valid</span> · variables: ${esc((r.variables || []).join(", ") || "none")}${"value" in r && r.value !== undefined
          ? ` · current value: <b>${esc(showValue(r.value))}</b>${r.status && r.status !== "ok" ? ` (${esc(r.status)})` : ""}` : ""}`
        : `<span class="bad">${esc(r.error)}</span>`;
    } catch (e) { out.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  return h("div", { class: "expr-tester" }, h("div", { class: "row" }, input, h("button", { type: "button", onclick: run }, "Check")),
    out, h("div", { class: "help" }, EXPR_HELP));
}

// ------------------------------------------------------------------ inspector: industrial panel
function itemSummary(section, it) {
  switch (section) {
    case "sensors": {
      const m = it.model || {};
      return `${esc(it.unit || "")} · ${esc(it.source)}${m.type && m.type !== "none" ? ` · ${esc(m.type)}` : ""}` +
        `${it.external_key ? ` · key <code>${esc(it.external_key)}</code>` : ""} · every ${num(it.sample_interval_s)} s${it.enabled === false ? " · disabled" : ""}`;
    }
    case "composites": return `<code>${esc(it.expression)}</code>`;
    case "conditions": return `<span class="sev ${esc(it.severity)}">${esc(it.severity)}</span> on <code>${esc(it.activation)}</code>` +
      `${it.deactivation ? ` off <code>${esc(it.deactivation)}</code>` : ""}${it.action && it.action !== "none" ? ` · action <b>${esc(it.action)}</b>` : ""}` +
      `${(it.effects || []).length ? ` · ${it.effects.map((e) => `${esc(e.target)} ${esc(e.op)} ${esc(showValue(e.value))}`).join(", ")}` : ""}` +
      `${it.state?.active ? ' · <b class="bad">ACTIVE</b>' : ""}`;
    case "behaviors": return `${esc(it.output)} = <code>${esc(it.expression)}</code>${it.when ? ` when <code>${esc(it.when)}</code>` : ""}`;
    default: return "";
  }
}

async function renderIndustrialPanel(a, el) {
  const id = enc(a.id);
  const [d, types, meas] = await Promise.all([
    api("GET", `/api/assets/${id}/industrial`), api("GET", "/api/asset-types").catch(() => []),
    api("GET", `/api/assets/${id}/measurements?limit=1`).catch(() => ({ latest: {} }))]);
  const bound = d.type;
  const typeHtml = bound
    ? `<div class="row">Type <b>${esc(bound.name || bound.id)}</b> <code>${esc(bound.id)}</code> v${bound.bound_version}
        ${bound.outdated ? `<span class="badge blocked">outdated → v${bound.current_version}</span>` : '<span class="badge running">up to date</span>'}</div>
       <div class="row">${bound.outdated ? '<button data-ind="sync-preview">Preview update</button><button data-ind="sync" class="primary">Update to latest</button>' : ""}
        <button data-ind="unbind" title="Keep the effective definitions as local items">Detach from type</button></div>`
    : `<div class="row"><select id="ind-bind-type">${types.filter((t) => t.kind === a.kind).map((t) => `<option value="${esc(t.id)}">${esc(t.name)} (${esc(t.id)}) v${t.version}</option>`).join("")}</select>
        <button data-ind="bind">Bind to type</button><button data-ind="save-type" title="Create an asset type from this asset">Save as type…</button></div>`;
  const latest = Object.values(meas.latest || {});
  const measHtml = latest.length
    ? `<table class="hist"><tr><th>measurement</th><th>value</th><th>unit</th><th>source</th><th>quality</th><th>t</th></tr>
        ${latest.map((o) => `<tr><td>${esc(o.measurement_id)}${o.kind === "composite" ? ' <span class="muted">(composite)</span>' : ""}</td>
          <td><b>${esc(num(o.value))}</b></td><td>${esc(o.unit || "")}</td><td>${esc(o.source)}</td>
          <td class="${o.quality === "good" ? "" : "bad"}">${esc(o.quality)}</td><td>${fmt(o.sim_time)}</td></tr>`).join("")}</table>`
    : '<p class="hint">no measurements yet</p>';
  const conds = (d.active_conditions || []);
  const params = d.parameters || [];
  const paramHtml = params.length
    ? `<table class="hist"><tr><th>parameter</th><th>value</th><th></th></tr>${params.map((p) => `<tr><td>${esc(p.label || p.name)}
        ${p.unit ? `<span class="muted">[${esc(p.unit)}]</span>` : ""}</td><td>${p.allowed
          ? `<select data-param="${esc(p.name)}">${p.allowed.map((x) => `<option ${x === p.value ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>`
          : p.type === "boolean" ? `<input type="checkbox" data-param="${esc(p.name)}" ${p.value ? "checked" : ""}>`
          : `<input data-param="${esc(p.name)}" data-type="${esc(p.type)}" value="${esc(showValue(p.value))}" ${p.overridable === false ? "readonly" : ""} class="num">`}</td>
        <td>${originBadge(p.overridden ? "override" : p.origin)}${p.min !== null || p.max !== null ? ` <span class="muted">${num(p.min) ?? ""}…${num(p.max) ?? ""}</span>` : ""}</td></tr>`).join("")}</table>
       <div class="row"><button data-ind="params" class="primary">Apply parameters</button></div>`
    : '<p class="hint">no parameters</p>';
  const sectionHtml = (section) => {
    const items = d[section] || [];
    if (!items.length) return `<p class="hint">none</p>`;
    return `<table class="hist">${items.map((it) => {
      const key = it[SECTION_ID[section]];
      const canOverride = bound && OVERRIDABLE.includes(section) && it.origin !== "local";
      return `<tr><td><b>${esc(key)}</b> ${originBadge(it.origin)}</td><td>${itemSummary(section, it)}</td><td class="nowrap">
        ${canOverride ? `<button class="small" data-override="${esc(section)}" data-key="${esc(key)}">Override</button>` : ""}
        ${it.origin === "override" ? `<button class="small" data-reset="${esc(section)}" data-key="${esc(key)}">Reset</button>` : ""}</td></tr>`;
    }).join("")}</table>`;
  };
  const s = d.sync;
  const sel = (name, opts, v) => `<select data-sync="${name}">${opts.map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`;
  const syncHtml = `<div class="kv">
      <div>Mode</div><div>${sel("mode", ["simulation", "external", "hybrid"], s.mode)}</div>
      <div>External id</div><div><input data-sync="external_id" value="${esc(s.external_id || "")}" placeholder="${esc(a.id)}"></div>
      ${Object.entries(s.categories).map(([k, v]) => `<div>${esc(k)}</div><div>${sel(`cat:${k}`, ["simulation", "external"], v)}</div>`).join("")}
      <div>Reconciliation</div><div>${sel("reconciliation", ["lenient", "strict"], s.reconciliation)}</div>
      <div>Late messages</div><div>${sel("late_policy", ["store", "apply", "discard"], s.late_policy)}</div>
      <div>Time base</div><div>${sel("time_mode", ["arrival", "source"], s.time_mode)}</div>
      <div>Max lateness</div><div><input data-sync="max_lateness_s" type="number" min="0" step="any" value="${s.max_lateness_s}" class="num"> s</div>
    </div><p class="help">Hybrid mode: categories set to <i>external</i> are driven by ingested messages (REST <code>POST /api/ingestion/events</code> or MQTT),
      the others keep being simulated. Mode presets the categories; per-category choices refine it.</p>
    <div class="row"><button data-ind="sync-save" class="primary">Apply synchronisation</button></div>`;
  const problems = [...(d.problems || []), ...(d.evaluation_errors || []).map((e) => (typeof e === "string" ? e : `${e.item || ""}: ${e.error || showValue(e)}`))];
  el.innerHTML = `<h4>Industrial profile</h4>${typeHtml}
    <div class="row">Classification <b class="cls ${esc(d.classification)}">${esc(d.classification || "normal")}</b>
      ${conds.map((c) => `<span class="chip sev ${esc(c.severity)}">${esc(c.id)}</span>`).join(" ") || '<span class="muted">no active conditions</span>'}</div>
    ${problems.length ? `<div class="problems">${problems.map((p) => `<div class="bad">⚠ ${esc(showValue(p))}</div>`).join("")}</div>` : ""}
    ${openSection("ind-meas", measHtml, `Live measurements (${latest.length})`, false)}
    ${openSection("ind-params", paramHtml, `Parameters (${params.length})`, !params.length)}
    ${["sensors", "composites", "conditions", "behaviors"].map((sec) =>
      openSection(`ind-${sec}`, sectionHtml(sec), `${sec[0].toUpperCase()}${sec.slice(1)} (${(d[sec] || []).length})`)).join("")}
    ${openSection("ind-local", `<p class="help">Instance-specific items (in addition to the type). Keys must not clash with template items.</p>
      <div class="row"><button data-ind="local">Edit local definitions…</button></div><div id="ind-expr"></div>`, "Local definitions & expression tester")}
    ${openSection("ind-sync", syncHtml, `Synchronisation · <b>${esc(s.mode)}</b>`)}
    ${openSection("ind-features", `<pre>${esc(pretty(d.last_features || {}))}</pre>`, "Last evaluated features")}`;
  bindSections(el);
  $("#ind-expr").append(expressionTester(() => ({ asset_id: a.id })));
  el.addEventListener("input", () => (inspectorDirty = true));
  const modeSel = el.querySelector('[data-sync="mode"]');
  const catSels = [...el.querySelectorAll('[data-sync^="cat:"]')];
  const syncCats = () => catSels.forEach((c) => {
    if (modeSel.value !== "hybrid") c.value = modeSel.value;
    c.disabled = modeSel.value !== "hybrid";
  });
  modeSel.addEventListener("change", syncCats);
  syncCats();
  const reload = () => { inspectorDirty = false; renderInspector(true); };
  const call = (fn, msg) => act(async () => { const r = await fn(); reload(); return r; }, msg);
  el.onclick = (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.override) {
      const sec = b.dataset.override, key = b.dataset.key;
      return openJsonDialog({ title: `Override ${sec.slice(0, -1)} '${key}'`, value: d.overrides?.[sec]?.[key] || {},
        subtitle: "Only the fields given here replace the template values (nested objects are merged). The item id cannot change.",
        onSubmit: async (v) => { await api("PUT", `/api/assets/${id}/overrides/${sec}/${enc(key)}`, v); toast("Override saved", true); reload(); } });
    }
    if (b.dataset.reset) return call(() => api("DELETE", `/api/assets/${id}/overrides/${b.dataset.reset}/${enc(b.dataset.key)}`), "Reset to template");
    switch (b.dataset.ind) {
      case "bind": return call(() => api("POST", `/api/assets/${id}/bind`, { type_id: $("#ind-bind-type").value, apply_config: true }), "Bound to type");
      case "unbind": return confirm("Detach from the asset type? Effective definitions become local items.") && call(() => api("POST", `/api/assets/${id}/unbind`), "Detached");
      case "sync": return call(() => api("POST", `/api/assets/${id}/sync-type`), "Updated to the latest type version");
      case "sync-preview": return api("POST", `/api/assets/${id}/sync-type?dry_run=true`).then((r) =>
        openJsonDialog({ title: "Type update preview", value: r, submitLabel: "Apply update", onSubmit: async () => {
          await api("POST", `/api/assets/${id}/sync-type`); toast("Updated", true); reload(); } })).catch((e) => toast(e.message));
      case "save-type": return saveAsTypeDialog(a, d);
      case "params": {
        const vals = {};
        el.querySelectorAll("[data-param]").forEach((i) => {
          if (i.readOnly) return;
          vals[i.dataset.param] = i.type === "checkbox" ? i.checked : i.tagName === "SELECT" ? i.value
            : ["number", "integer"].includes(i.dataset.type) ? Number(i.value) : i.dataset.type === "json" ? smartParse(i.value) : i.value;
        });
        return call(() => api("PUT", `/api/assets/${id}/parameters`, vals), "Parameters applied");
      }
      case "local": return openJsonDialog({ title: `Local definitions of ${a.id}`, value: d.local,
        subtitle: "Sections: parameters, sensors, composites, conditions, behaviors.", extra: h("div", { class: "help" }, EXPR_HELP),
        onSubmit: async (v) => { await api("PUT", `/api/assets/${id}/local`, v); toast("Local definitions saved", true); reload(); } });
      case "sync-save": {
        const patch = { categories: {} };
        el.querySelectorAll("[data-sync]").forEach((i) => {
          const k = i.dataset.sync;
          if (k.startsWith("cat:")) patch.categories[k.slice(4)] = i.value;
          else if (k === "max_lateness_s") patch[k] = Number(i.value);
          else if (k === "external_id") patch[k] = i.value.trim() || null;
          else patch[k] = i.value;
        });
        return call(() => api("PUT", `/api/assets/${id}/sync`, patch), "Synchronisation updated");
      }
    }
  };
}

function saveAsTypeDialog(a, d) {
  const cfg = { ...a.config };
  delete cfg.initial_state;
  const profile = Object.fromEntries(PROFILE_SECTIONS.map((s) => [s, (d.local?.[s] || [])]));
  openJsonDialog({ title: `Create asset type from ${a.id}`, submitLabel: "Create type & bind",
    value: { id: slug(a.name), name: a.name, kind: a.kind, description: "", config: cfg, profile },
    onSubmit: async (v) => {
      const t = await api("POST", "/api/asset-types", v);
      await api("POST", `/api/assets/${enc(a.id)}/bind`, { type_id: t.id, apply_config: false });
      toast(`Asset type '${t.id}' created`, true); inspectorDirty = false; renderInspector(true); renderAssetTypes();
    } });
}

// ------------------------------------------------------------------ asset types tab
let typesCache = [];
async function renderAssetTypes() {
  const el = $("#types-catalog");
  if (!el) return;
  try { typesCache = await api("GET", "/api/asset-types"); } catch (e) { el.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  el.innerHTML = typesCache.map((t) => {
    const p = t.profile || {};
    const counts = PROFILE_SECTIONS.map((s) => `${(p[s] || []).length} ${s}`).join(" · ");
    return `<div class="card"><div class="card-head"><b>${esc(t.name)}</b> <span class="muted">${esc(t.id)} · ${esc(t.kind)} · v${t.version}</span>
      <span class="card-actions"><button class="small" data-tact="edit" data-id="${esc(t.id)}">Edit</button>
      <button class="small" data-tact="dup" data-id="${esc(t.id)}">Duplicate</button>
      ${t.kind !== "conveyor" ? `<button class="small primary" data-tact="deploy" data-id="${esc(t.id)}">Deploy</button>` : ""}
      <button class="small del" data-tact="del" data-id="${esc(t.id)}">Delete</button></span></div>
      ${t.description ? `<div>${esc(t.description)}</div>` : ""}
      <div class="muted">${counts}</div>
      <div>Instances: ${t.instances.length ? t.instances.map((i) => `<span class="link chip" data-asset="${esc(i)}">${esc(i)}${t.outdated_instances.includes(i) ? " ⟳" : ""}</span>`).join(" ") : '<span class="muted">none</span>'}
      ${t.outdated_instances.length ? `<button class="small" data-tact="sync" data-id="${esc(t.id)}">Update ${t.outdated_instances.length} outdated…</button>` : ""}</div></div>`;
  }).join("") || '<p class="hint">No asset types yet. Create one, or select an asset and use “Save as type…” in its inspector.</p>';
}
function typeDialog(existing) {
  const v = existing ? { name: existing.name, description: existing.description, config: existing.config, profile: existing.profile }
    : { id: "furnace", name: "Furnace", kind: "transformer", description: "", config: { speed_pct: 80 }, profile: PROFILE_SKELETON };
  openJsonDialog({ title: existing ? `Edit asset type ${existing.id} (v${existing.version})` : "New asset type", value: v, rows: 26,
    subtitle: existing ? "Saving creates a new version. Instances keep their bound version until updated; overrides are preserved."
      : "kind: producer | transformer | conveyor | splitter | sink. config: template configuration. profile: parameters, sensors, composites, conditions, behaviors.",
    extra: expressionTester(() => (existing ? { type_id: existing.id } : {})),
    onSubmit: async (body) => {
      if (existing) await api("PUT", `/api/asset-types/${enc(existing.id)}`, body);
      else await api("POST", "/api/asset-types", body);
      toast(existing ? "Asset type saved (new version)" : "Asset type created", true); renderAssetTypes();
    } });
}
async function deployDialog(t) {
  const ctx = await loadCtx();
  const n = (state?.assets || []).filter((x) => x.kind === t.kind).length + 1;
  openFormDialog({ title: `Deploy ${t.name}`, subtitle: "Creates a new asset bound to this type. Instance config overrides the template.",
    fields: [{ key: "name", label: "Name", type: "text" }, { key: "id", label: "ID", type: "text", placeholder: "auto" },
      ...(t.kind === "transformer" ? [{ key: "recipe_id", label: "Recipe", type: "select", nullable: true,
        options: (c) => c.recipes.map((r) => [r.id, r.name === r.id ? r.id : `${r.name} (${r.id})`]) }] : []),
      ...(t.kind === "producer" ? [{ key: "product_type_id", label: "Produces", type: "select",
        options: (c) => c.types.map((x) => [x.id, typeLabel(c, x.id)]) }] : [])],
    value: { name: `${t.name} ${n}`, recipe_id: t.config.recipe_id ?? ctx.recipes[0]?.id ?? null, product_type_id: t.config.product_type_id ?? ctx.types[0]?.id },
    ctx, submitLabel: "Deploy",
    onSubmit: async (v) => {
      const config = {};
      if (t.kind === "transformer") config.recipe_id = v.recipe_id;
      if (t.kind === "producer") config.product_type_id = v.product_type_id;
      const pos = { x: 80 + (n % 6) * 40, y: 80 + (n % 5) * 40 };
      const a = await api("POST", `/api/asset-types/${enc(t.id)}/deploy`, { name: v.name, id: v.id || undefined, config, position: pos });
      toast(`Deployed ${a.id}`, true); await refresh(); renderAssetTypes(); select(a.id);
    } });
}
document.addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-tact], #btn-new-asset-type, [data-asset]");
  if (!b) return;
  if (b.dataset.asset) return select(b.dataset.asset);
  if (b.id === "btn-new-asset-type") return typeDialog(null);
  const t = typesCache.find((x) => x.id === b.dataset.id);
  if (!t) return;
  switch (b.dataset.tact) {
    case "edit": return typeDialog(t);
    case "dup": {
      const nid = prompt("New asset type id", `${t.id}-copy`);
      if (nid) act(async () => { await api("POST", `/api/asset-types/${enc(t.id)}/duplicate`, { id: nid, name: `${t.name} (copy)` }); renderAssetTypes(); }, "Duplicated");
      return;
    }
    case "deploy": return deployDialog(t).catch((e) => toast(e.message));
    case "del": return confirm(`Delete asset type '${t.id}'?`) && act(async () => { await api("DELETE", `/api/asset-types/${enc(t.id)}`); renderAssetTypes(); }, "Deleted");
    case "sync": return api("POST", `/api/asset-types/${enc(t.id)}/sync?dry_run=true`).then((r) => openJsonDialog({
      title: `Update instances of ${t.id} to v${t.version}`, value: r, submitLabel: "Apply to all",
      subtitle: "Preview: config changes, preserved and dropped overrides per instance.",
      onSubmit: async () => { await api("POST", `/api/asset-types/${enc(t.id)}/sync`); toast("Instances updated", true); renderAssetTypes(); } })).catch((e) => toast(e.message));
  }
});

// ------------------------------------------------------------------ external ingestion (integration tab)
const MQTT_FIELDS = [
  { row: "Broker", fields: [{ key: "host", type: "text", placeholder: "host" }, { key: "port", type: "number", int: true, min: 1, max: 65535 }] },
  { key: "client_id", label: "Client id", type: "text" },
  { row: "Credentials", fields: [{ key: "username", type: "text", placeholder: "username" }, { key: "password", type: "text", placeholder: "password" }] },
  { key: "tls", label: "TLS", type: "bool", checkLabel: "use TLS" },
  { key: "qos", label: "QoS", type: "select", options: [[0, "0"], [1, "1"], [2, "2"]] },
  { key: "topics", label: "Topics", type: "object", fields: [
    { key: "telemetry", label: "telemetry", type: "text" }, { key: "events", label: "events", type: "text" },
    { key: "configuration", label: "configuration", type: "text" }] },
];
let mqttForm = null;
async function renderIngestion() {
  const el = $("#ingestion");
  if (!el) return;
  let st;
  try { st = await api("GET", "/api/ingestion/status"); } catch (e) { return; }
  const m = st.mqtt;
  if (!mqttForm) {
    mqttForm = buildForm(MQTT_FIELDS, { ...m.config, password: m.config.password ? "***" : "" }, {});
    $("#mqtt-form").replaceChildren(mqttForm.el);
  }
  $("#mqtt-status").innerHTML = `<span class="badge ${m.status === "connected" ? "running" : m.status === "error" ? "failed" : "idle"}">${esc(m.status)}</span>
    received ${m.received} · rejected ${m.rejected}${m.last_error ? ` · <span class="bad">${esc(m.last_error)}</span>` : ""}
    ${(m.subscriptions || []).length ? `<div class="muted">topics: ${m.subscriptions.map((s) => esc(Array.isArray(s) ? s[0] : s)).join(", ")}</div>` : ""}`;
  const counts = Object.entries(st.counts || {}).map(([k, v]) => `${esc(k)}: <b>${v}</b>`).join(" · ") || "no messages yet";
  $("#ingest-status").innerHTML = `<div>${counts}</div>${Object.keys(st.clock_skew || {}).length ? `<div class="muted">clock skew: ${
    Object.entries(st.clock_skew).map(([a, s]) => `${esc(a)} avg ${num(s.avg, 2)} s`).join(", ")}</div>` : ""}`;
}
async function showIngestMessages(status) {
  try {
    const msgs = await api("GET", `/api/ingestion/messages?limit=50${status ? `&status=${status}` : ""}`);
    $("#ingest-log").innerHTML = msgs.slice().reverse().map((r) => `<div class="ev ${r.status === "accepted" ? "" : "warn"}">
      t=${fmt(r.received_at)} <b>${esc(r.status)}</b> ${esc(r.transport)} ${esc(r.event_type || "")} ${esc(r.asset_id || "")} <span class="muted">${esc(r.event_id || "")}</span>
      ${(r.errors || []).map((e) => `<div class="bad">${esc(e)}</div>`).join("")}${(r.warnings || []).map((e) => `<div class="muted">${esc(e)}</div>`).join("")}</div>`).join("") || '<p class="hint">none</p>';
  } catch (e) { toast(e.message); }
}
function sampleEnvelope() {
  const target = (state?.assets || []).find((a) => a.id === selectedId) || (state?.assets || []).find((a) => a.kind === "transformer");
  return { schema_version: "1.0", event_id: `evt-${Date.now()}`, asset_id: target?.id || "asset-id", event_type: "telemetry",
    timestamp: new Date().toISOString(), measurements: { temp: { value: 850.5, unit: "degC" } } };
}
function bindIngestion() {
  if (!$("#ingestion")) return;
  $("#mqtt-save").onclick = () => act(async () => { await api("PUT", "/api/ingestion/mqtt", mqttForm.get()); mqttForm = null; renderIngestion(); }, "MQTT settings saved");
  $("#mqtt-start").onclick = () => act(async () => {
    await api("PUT", "/api/ingestion/mqtt", mqttForm.get());
    const r = await api("POST", "/api/ingestion/mqtt/start"); mqttForm = null; renderIngestion();
    if (r.status === "error") throw new Error(r.last_error || "connection failed");
  }, "Connecting…");
  $("#mqtt-stop").onclick = () => act(async () => { await api("POST", "/api/ingestion/mqtt/stop"); renderIngestion(); }, "Disconnected");
  $("#ingest-sample").onclick = () => ($("#ingest-body").value = pretty(sampleEnvelope()));
  $("#ingest-send").onclick = async () => {
    let body;
    try { body = JSON.parse($("#ingest-body").value); } catch (e) { return toast("Invalid JSON: " + e.message); }
    try {
      const r = await api("POST", "/api/ingestion/events", body);
      $("#ingest-result").textContent = pretty(r);
      const one = r.results ? r.results.every((x) => x.status === "accepted") : r.status === "accepted";
      toast(one ? "Accepted" : "Not (fully) accepted – see result", one);
      renderIngestion();
    } catch (e) { toast(e.message); }
  };
  $("#ingest-msgs").onclick = () => showIngestMessages("");
  $("#ingest-rejected").onclick = () => showIngestMessages("rejected");
}

// ------------------------------------------------------------------ datasets tab
const DS_FIELDS = (ctx, assets) => [
  { key: "name", label: "Name", type: "text" },
  { key: "experiment", label: "Experiment", type: "text", placeholder: "optional label" },
  { key: "description", label: "Description", type: "text", multiline: true },
  { key: "datasets", label: "Datasets", type: "multi", options: ["sensor", "cycle", "product", "machine"] },
  { key: "assets", label: "Assets", type: "multi", options: assets, help: "none selected = all assets" },
  { key: "measurements", label: "Measurements", type: "csv", placeholder: "temp, t1.pressure (empty = all)" },
  { key: "include_composites", label: "Composites", type: "bool", checkLabel: "include composite values" },
  { key: "product_types", label: "Product types", type: "multi", options: ctx.types.map((t) => [t.id, t.name]), help: "none selected = all" },
  { key: "attributes", label: "Attributes", type: "csv", placeholder: "empty = all" },
  { key: "aggregations", label: "Cycle aggregations", type: "multi", options: ["avg", "min", "max", "initial", "final", "count"] },
  { row: "Sensor sampling", fields: [{ key: "sampling_mode", type: "select", options: [["event", "every observation"], ["periodic", "periodic snapshot"], ["both", "both"]] },
    { key: "sampling_interval", type: "number", min: 0.001, step: "any", suffix: "s" }] },
  { row: "Start", fields: [{ key: "start_type", type: "select", options: [["manual", "manually / now"], ["at_time", "at sim time"]] },
    { key: "start_at", type: "number", min: 0, step: "any", suffix: "s" }] },
  { row: "End", fields: [{ key: "end_type", type: "select", options: [["manual", "manually"], ["at_time", "at sim time"], ["duration", "after duration (s)"],
    ["cycle_count", "after N cycles"], ["product_count", "after N products"]] }, { key: "end_value", type: "number", min: 0, step: "any" }] },
  { row: "Missing values", fields: [{ key: "missing_policy", type: "select", options: [["empty", "empty cell"], ["null", "NULL marker"], ["drop_row", "drop row"], ["fill", "fill value"]] },
    { key: "fill_value", type: "text", placeholder: "fill value" }] },
  { key: "format", label: "Export format", type: "select", options: [["csv", "CSV"], ["jsonl", "JSON lines"]] },
  { key: "autostart", label: "Start", type: "bool", checkLabel: "start collecting immediately" },
];
WIDGETS.multi = (f, v) => {
  const opts = f.options.map((o) => (Array.isArray(o) ? o : [o, o]));
  const boxes = opts.map(([val, label]) => [val, h("input", { type: "checkbox", checked: (v || []).includes(val) })]);
  return { el: h("div", { class: "checks" }, boxes.map(([val, b], i) => h("label", { class: "check" }, b, opts[i][1]))),
    get: () => boxes.filter(([, b]) => b.checked).map(([val]) => val) };
};
WIDGETS.csv = (f, v) => {
  const i = h("input", { value: (v || []).join(", "), placeholder: f.placeholder });
  return { el: i, get: () => i.value.split(",").map((s) => s.trim()).filter(Boolean) };
};
function dsFormValue(s) {
  const c = s?.config || {};
  return { name: s?.name || "Collection session", experiment: s?.experiment || "", description: s?.description || "",
    datasets: c.datasets || ["sensor", "cycle", "product", "machine"], assets: c.assets || [], measurements: c.measurements || [],
    include_composites: c.include_composites ?? true, product_types: c.product_types || [], attributes: c.attributes || [],
    aggregations: c.aggregations || ["avg", "min", "max"], sampling_mode: c.sampling?.mode || "event", sampling_interval: c.sampling?.interval_s ?? 1,
    start_type: c.start?.type || "manual", start_at: c.start?.at_s ?? null, end_type: c.end?.type || "manual", end_value: c.end?.value ?? null,
    missing_policy: c.missing?.policy || "empty", fill_value: showValue(c.missing?.fill_value ?? ""), format: c.format || "csv", autostart: !s };
}
function dsConfigFrom(v) {
  return { datasets: v.datasets, assets: v.assets, measurements: v.measurements, include_composites: v.include_composites,
    product_types: v.product_types, attributes: v.attributes, aggregations: v.aggregations,
    sampling: { mode: v.sampling_mode, interval_s: v.sampling_interval || 1 },
    start: { type: v.start_type, at_s: v.start_type === "at_time" ? v.start_at : null },
    end: { type: v.end_type, value: v.end_type === "manual" ? null : v.end_value },
    missing: { policy: v.missing_policy, fill_value: smartParse(v.fill_value ?? "") }, format: v.format };
}
async function sessionDialog(existing) {
  const ctx = await loadCtx();
  const assets = (state?.assets || []).filter((a) => a.kind !== "conveyor").map((a) => [a.id, a.name === a.id ? a.id : `${a.name} (${a.id})`]);
  const fields = DS_FIELDS(ctx, assets).filter((f) => !(existing && f.key === "autostart"));
  openFormDialog({ title: existing ? `Edit ${existing.id}` : "New dataset collection session", fields, value: dsFormValue(existing), ctx,
    subtitle: "Column selection/renaming and other advanced options can be edited as JSON afterwards.",
    submitLabel: existing ? "Save" : "Create",
    onSubmit: async (v) => {
      const body = { name: v.name, experiment: v.experiment, description: v.description, config: dsConfigFrom(v) };
      if (existing) await api("PUT", `/api/datasets/sessions/${enc(existing.id)}`, body);
      else await api("POST", "/api/datasets/sessions", { ...body, autostart: v.autostart });
      toast(existing ? "Session saved" : "Session created", true); renderDatasets();
    } });
}
const DS_ACTIONS = { created: ["start"], scheduled: ["stop"], running: ["pause", "stop"], paused: ["resume", "stop"], stopped: [], completed: [] };
function sessionCard(s) {
  const cont = s.mode === "continuous";
  const acts = cont ? (s.status === "running" ? ["stop"] : ["start"]) : DS_ACTIONS[s.status] || [];
  const dl = (d) => `/api/datasets/sessions/${enc(s.id)}/export/${d}`;
  return `<div class="card"><div class="card-head"><b>${esc(s.name)}</b> <span class="muted">${esc(s.id)}${s.experiment ? ` · ${esc(s.experiment)}` : ""}</span>
    <span class="badge ${s.status === "running" ? "running" : s.status === "paused" ? "blocked" : "idle"}">${esc(s.status)}</span>
    <span class="card-actions">${acts.map((x) => `<button class="small ${x === "start" || x === "resume" ? "primary" : ""}" data-ds="${x}" data-id="${esc(s.id)}">${x}</button>`).join("")}
      <button class="small" data-ds="clear" data-id="${esc(s.id)}">clear</button>
      ${!cont && !["running", "paused"].includes(s.status) ? `<button class="small" data-ds="edit" data-id="${esc(s.id)}">edit</button>` : ""}
      <button class="small" data-ds="json" data-id="${esc(s.id)}">JSON</button>
      ${!cont ? `<button class="small del" data-ds="delete" data-id="${esc(s.id)}">✕</button>` : ""}</span></div>
    ${s.description ? `<div>${esc(s.description)}</div>` : ""}
    <div class="muted">rows: ${Object.entries(s.counts).map(([k, v]) => `${k} ${v}${s.truncated?.[k] ? " (truncated)" : ""}`).join(" · ")}
      ${s.duplicates_skipped ? ` · ${s.duplicates_skipped} duplicates skipped` : ""}
      · segments ${(s.segments || []).map(([a, b]) => `${fmt(a)}–${b === null ? "now" : fmt(b)}`).join(", ") || "–"}${s.end_reason ? ` · ended: ${esc(s.end_reason)}` : ""}</div>
    <div class="row">${s.config.datasets.map((d) => `<button class="small" data-ds="preview" data-set="${d}" data-id="${esc(s.id)}">preview ${d}</button>
      <a class="small btnlink" href="${dl(d)}" download>⬇ ${d}</a>`).join("")}
      <a class="small btnlink" href="/api/datasets/sessions/${enc(s.id)}/bundle" download>⬇ zip bundle</a>
      <a class="small btnlink" href="/api/datasets/sessions/${enc(s.id)}/manifest" target="_blank" rel="noopener">manifest</a></div></div>`;
}
let dsCache = null;
async function renderDatasets() {
  const el = $("#ds-sessions");
  if (!el) return;
  try { dsCache = await api("GET", "/api/datasets"); } catch (e) { el.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  $("#ds-continuous").innerHTML = sessionCard(dsCache.continuous);
  el.innerHTML = dsCache.sessions.slice().reverse().map(sessionCard).join("") || '<p class="hint">No sessions yet.</p>';
}
async function previewDataset(sid, dataset) {
  try {
    const pv = await api("GET", `/api/datasets/sessions/${enc(sid)}/preview/${dataset}?limit=100`);
    const head = pv.columns.map((c) => `<th title="${esc(pv.units[c] || "")}">${esc(c)}${pv.units[c] ? `<div class="muted">${esc(pv.units[c])}</div>` : ""}</th>`).join("");
    const body = pv.rows.map((r) => `<tr>${pv.columns.map((c) => `<td>${esc(num(r[c], 4))}</td>`).join("")}</tr>`).join("");
    $("#modal-content").innerHTML = `<div class="dialog wide"><h3>${esc(sid)} · ${esc(dataset)} dataset</h3>
      <p class="hint">${pv.total_rows} rows in total; showing the last ${pv.rows.length}.</p>
      <div class="table-scroll"><table class="hist data">${head ? `<tr>${head}</tr>` : ""}${body}</table></div>
      <div class="row end"><a class="btnlink" href="/api/datasets/sessions/${enc(sid)}/export/${dataset}" download>⬇ Export</a>
      <button onclick="closeModal()">Close</button></div></div>`;
    $("#modal").classList.remove("hidden");
  } catch (e) { toast(e.message); }
}
document.addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-ds], #btn-new-session");
  if (!b) return;
  if (b.id === "btn-new-session") return sessionDialog(null).catch((e) => toast(e.message));
  const sid = b.dataset.id, all = dsCache ? [dsCache.continuous, ...dsCache.sessions] : [];
  const s = all.find((x) => x.id === sid);
  switch (b.dataset.ds) {
    case "edit": return sessionDialog(s).catch((e) => toast(e.message));
    case "json": return openJsonDialog({ title: `${sid} configuration`, value: s.config,
      subtitle: "Advanced: columns.<dataset>.select / rename, cycle_filter, time_window, cycle_parameters, production_order_ids …",
      onSubmit: async (cfg) => { await api("PUT", `/api/datasets/sessions/${enc(sid)}`, { config: cfg }); toast("Saved", true); renderDatasets(); } });
    case "delete": return confirm(`Delete session ${sid} and its collected rows?`) && act(async () => { await api("DELETE", `/api/datasets/sessions/${enc(sid)}`); renderDatasets(); }, "Deleted");
    case "preview": return previewDataset(sid, b.dataset.set);
    default: return act(async () => { await api("POST", `/api/datasets/sessions/${enc(sid)}/${b.dataset.ds}`); renderDatasets(); }, `${b.dataset.ds}: ok`);
  }
});

// ------------------------------------------------------------------ tab hooks & polling
const TAB_RENDER = { types: renderAssetTypes, datasets: renderDatasets, integration: renderIngestion };
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => TAB_RENDER[b.dataset.tab]?.()));
bindIngestion();
setInterval(() => {
  const active = document.querySelector(".tabs button.active")?.dataset.tab;
  if (active === "datasets" && !$("#modal").classList.contains("hidden")) return;
  if (active === "datasets") renderDatasets();
  if (active === "integration") renderIngestion();
}, 2000);
