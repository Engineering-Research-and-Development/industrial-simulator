"use strict";
// Schema-driven form widgets used by the inspector, the asset creation dialog, the catalog and the
// integration tab. Every widget returns {el, get}; buildForm() composes them into an object editor.

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = true;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}
const fire = (el) => el.dispatchEvent(new Event("input", { bubbles: true }));

// "12" -> 12, "true" -> true, '{"a":1}' -> object, otherwise the trimmed string
function smartParse(s) {
  const t = String(s).trim();
  if (t === "true") return true;
  if (t === "false") return false;
  if (t !== "" && /^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(t)) return Number(t);
  if (/^[[{]/.test(t)) { try { return JSON.parse(t); } catch { /* keep string */ } }
  return t;
}
const showValue = (v) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
const slug = (s) => String(s).trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);

function ensureDatalist(id, values) {
  let dl = document.getElementById(id);
  if (!dl) { dl = h("datalist", { id }); document.body.append(dl); }
  dl.replaceChildren(...values.map(([v, label]) => h("option", { value: v }, label || "")));
}

// ------------------------------------------------------------------ widgets
const WIDGETS = {
  number(f, v) {
    const i = h("input", { type: "number", step: f.step ?? (f.int ? 1 : "any"), min: f.min, max: f.max, value: v ?? "" });
    return { el: f.suffix ? h("span", { class: "with-suffix" }, i, h("span", { class: "suffix" }, f.suffix)) : i,
      get: () => (i.value === "" ? null : f.int ? parseInt(i.value, 10) : Number(i.value)) };
  },
  text(f, v) {
    const i = h(f.multiline ? "textarea" : "input", { value: v ?? "", placeholder: f.placeholder, rows: f.multiline ? 2 : undefined,
      readonly: f.readonly, class: f.multiline ? "plain" : undefined });
    return { el: i, input: i, get: () => i.value.trim() };
  },
  select(f, v, ctx) {
    const opts = (typeof f.options === "function" ? f.options(ctx) : f.options).map((o) => (Array.isArray(o) ? o : [o, o]));
    if (f.nullable) opts.unshift([null, f.nullLabel || "— none —"]);
    if (v !== undefined && !opts.some(([ov]) => ov === v)) opts.push([v, `${v} (unknown)`]);
    const s = h("select", {}, opts.map(([ov, label], idx) => h("option", { value: String(idx) }, label)));
    const cur = opts.findIndex(([ov]) => ov === v);
    s.value = String(cur >= 0 ? cur : 0);
    return { el: s, get: () => opts[Number(s.value)]?.[0] ?? null };
  },
  slider(f, v) {
    const r = h("input", { type: "range", min: f.min, max: f.max, step: f.step ?? 1, value: v ?? f.min });
    const n = h("input", { type: "number", min: f.min, max: f.max, step: "any", value: v ?? f.min, class: "slider-num" });
    r.addEventListener("input", () => (n.value = r.value));
    n.addEventListener("input", () => (r.value = n.value));
    return { el: h("span", { class: "slider" }, r, n, f.suffix ? h("span", { class: "suffix" }, f.suffix) : null),
      get: () => (n.value === "" ? null : Number(n.value)) };
  },
  percent(f, v) {   // stored as 0..1, edited as 0..100 %
    const w = WIDGETS.slider({ min: 0, max: 100, step: 0.5, suffix: "%" }, v === undefined || v === null ? 0 : +(v * 100).toFixed(4));
    return { el: w.el, get: () => (w.get() === null ? null : w.get() / 100) };
  },
  bool(f, v) {
    const c = h("input", { type: "checkbox", checked: !!v });
    return { el: h("label", { class: "check" }, c, f.checkLabel || ""), get: () => c.checked };
  },
  nullableInt(f, v) {
    const c = h("input", { type: "checkbox", checked: v === null || v === undefined });
    const n = h("input", { type: "number", min: 1, step: 1, value: v ?? "", class: "num" });
    const sync = () => (n.style.display = c.checked ? "none" : "");
    c.addEventListener("change", () => { sync(); if (!c.checked && n.value === "") n.value = f.defaultValue ?? 100; });
    sync();
    return { el: h("span", { class: "inline" }, h("label", { class: "check" }, c, f.nullLabel || "unlimited"), n),
      get: () => (c.checked ? null : parseInt(n.value, 10)) };
  },
  typeSet(f, v, ctx) {   // null = accept every type, otherwise a list of product type IDs
    const all = h("input", { type: "checkbox", checked: v === null || v === undefined });
    const boxes = ctx.types.map((t) => [t.id, h("input", { type: "checkbox", checked: Array.isArray(v) && v.includes(t.id) })]);
    const sync = () => boxes.forEach(([, b]) => (b.disabled = all.checked));
    all.addEventListener("change", sync); sync();
    return { el: h("div", { class: "checks" }, h("label", { class: "check" }, all, h("b", {}, "All product types")),
      boxes.map(([id, b]) => h("label", { class: "check" }, b, typeLabel(ctx, id)))),
      get: () => (all.checked ? null : boxes.filter(([, b]) => b.checked).map(([id]) => id)) };
  },
  kv(f, v) {
    return rowsWidget(Object.entries(v || {}), ([k, val] = ["", ""]) => {
      const ki = h("input", { value: k, placeholder: "name", class: "k" });
      const vi = h("input", { value: showValue(val), placeholder: "value", class: "v" });
      return { cells: [ki, vi], get: () => [ki.value.trim(), smartParse(vi.value)] };
    }, { addLabel: f.addLabel || "+ Add attribute", collect: (rows) => Object.fromEntries(rows.filter(([k]) => k)) });
  },
  conditions(f, v, ctx) {
    ensureDatalist("dl-attrs", [["is_defective", "defective flag"], ["product_type_id", "product type"],
      ["quality_score", "0..1"], ["lot_number", "lot"], ["attributes.", "product attribute…"], ["measurements.", "measurement…"]]);
    ensureDatalist("dl-types", ctx.types.map((t) => [t.id, t.name]));
    ensureDatalist("dl-bool", [["true"], ["false"]]);
    return rowsWidget(v || [], (c = { attribute: "is_defective", operator: "eq", value: false, branch: "A" }) => {
      const attr = h("input", { value: c.attribute, list: "dl-attrs", placeholder: "attribute", class: "attr" });
      const op = h("select", { class: "op" }, Object.entries(OPERATOR_LABELS).map(([k, l]) => h("option", { value: k }, l)));
      op.value = c.operator || "eq";
      const val = h("input", { value: Array.isArray(c.value) ? c.value.join(", ") : showValue(c.value), placeholder: "value", class: "val" });
      const br = f.branch ? h("select", { class: "br", title: "send to" }, h("option", { value: "A" }, "→ A"), h("option", { value: "B" }, "→ B")) : null;
      if (br) br.value = c.branch || "A";
      const sync = () => {
        const a = attr.value.trim();
        val.setAttribute("list", a === "is_defective" ? "dl-bool" : a === "product_type_id" ? "dl-types" : "");
        val.style.visibility = op.value === "exists" ? "hidden" : "visible";
        val.placeholder = op.value === "in" || op.value === "not_in" ? "a, b, c" : "value";
      };
      attr.addEventListener("input", sync); op.addEventListener("change", sync); sync();
      return { cells: [attr, op, val, br], get: () => {
        const a = attr.value.trim(), o = op.value;
        const keepString = a === "product_type_id" || a === "lot_number";
        const parse = (s) => (keepString ? String(s).trim() : smartParse(s));
        let value;
        if (o === "exists") value = null;
        else if (o === "in" || o === "not_in") value = val.value.split(",").map((s) => s.trim()).filter(Boolean).map(parse);
        else if (["lt", "le", "gt", "ge"].includes(o)) value = val.value.trim() === "" ? null : Number(val.value);
        else value = parse(val.value);
        const out = { attribute: a, operator: o, value };
        if (br) out.branch = br.value;
        return out;
      } };
    }, { addLabel: f.addLabel || "+ Add condition", empty: f.empty });
  },
  recipeItems(f, v, ctx) {
    return rowsWidget(v || [], (it = { product_type_id: ctx.types[0]?.id, quantity: 1, is_byproduct: false }) => {
      const t = WIDGETS.select({ options: (c) => c.types.map((x) => [x.id, x.name === x.id ? x.id : `${x.name} (${x.id})`]) }, it.product_type_id, ctx);
      const q = h("input", { type: "number", min: 1, step: 1, value: it.quantity ?? 1, class: "qty", title: "quantity" });
      const b = f.byproduct ? h("input", { type: "checkbox", checked: !!it.is_byproduct }) : null;
      return { cells: [h("span", { class: "x" }, q, "×"), t.el, b ? h("label", { class: "check", title: "by-product" }, b, "by-product") : null],
        get: () => ({ product_type_id: t.get(), quantity: parseInt(q.value, 10), is_byproduct: b ? b.checked : false }) };
    }, { min: 1, max: f.max, addLabel: f.addLabel, minRows: 1 });
  },
  speedPoints(f, v) {
    return rowsWidget(v || [], (p = [50, 1]) => {
      const s = h("input", { type: "number", min: 1, max: 100, step: "any", value: p[0], class: "num" });
      const m = h("input", { type: "number", min: 0.01, step: "any", value: p[1], class: "num" });
      return { cells: [h("span", { class: "x" }, "at", s, "%"), h("span", { class: "x" }, "time ×", m)], get: () => [Number(s.value), Number(m.value)] };
    }, { min: 2, addLabel: "+ Add point" });
  },
  object(f, v, ctx) {
    const sub = buildForm(f.fields, v || {}, ctx);
    return { el: h("div", { class: "subform" }, sub.el), get: sub.get };
  },
};
const OPERATOR_LABELS = { eq: "=", ne: "≠", lt: "<", le: "≤", gt: ">", ge: "≥", in: "in", not_in: "not in", exists: "exists" };

function rowsWidget(items, makeRow, { max = Infinity, min = 0, addLabel = "+ Add", collect, empty, minRows = 0 } = {}) {
  const list = h("div", { class: "rows" });
  const rows = [];
  const emptyEl = h("div", { class: "hint" }, empty || "none");
  const addBtn = h("button", { type: "button", class: "small", onclick: () => { add(); fire(list); } }, addLabel);
  const refresh = () => {
    addBtn.disabled = rows.length >= max;
    rows.forEach((r) => (r.del.disabled = rows.length <= min));
    emptyEl.style.display = rows.length ? "none" : "";
  };
  function add(item) {
    const r = makeRow(item);
    const line = h("div", { class: "line" }, r.cells);
    const rec = { ...r };
    rec.del = h("button", { type: "button", class: "small del", title: "Remove", onclick: () => {
      rows.splice(rows.indexOf(rec), 1); line.remove(); refresh(); fire(list);
    } }, "✕");
    line.append(rec.del);
    rows.push(rec); list.append(line); refresh();
  }
  items.forEach((it) => add(it));
  while (rows.length < minRows) add();
  refresh();
  return { el: h("div", {}, list, emptyEl, addBtn), get: () => (collect ? collect(rows.map((r) => r.get())) : rows.map((r) => r.get())) };
}

// fields: [{key, label, type, help, showIf(values)} | {section, collapsed, fields} | {row: label, fields}]
const SECTION_OPEN = {};   // remembers expanded sections across inspector re-renders
const STACKED_TYPES = new Set(["object", "conditions", "kv", "recipeItems", "speedPoints", "typeSet"]);
function buildForm(fields, value, ctx, preview) {
  const el = h("div", { class: "form" });
  const getters = [];
  const conditional = [];
  const render = (list, parent) => {
    for (const f of list) {
      if (f.section) {
        const d = h("details", { class: "section", open: SECTION_OPEN[f.section] ?? !f.collapsed }, h("summary", {}, f.section));
        d.addEventListener("toggle", () => (SECTION_OPEN[f.section] = d.open));
        parent.append(d); render(f.fields, d);
        continue;
      }
      const parts = f.row ? f.fields : [f];
      const wide = !f.row && STACKED_TYPES.has(f.type);
      const lblText = f.label ?? f.row;
      const wrap = h("div", { class: `f${wide ? " stack" : ""}${lblText ? "" : " nolabel"}` }, lblText ? h("label", { class: "lbl" }, lblText) : null);
      const body = h("div", { class: "fbody" });
      const inline = h("div", { class: f.row ? "inline" : "" });
      for (const p of parts) {
        const w = WIDGETS[p.type || "text"](p, value?.[p.key], ctx);
        getters.push([p.key, w.get]);
        inline.append(w.el);
      }
      body.append(inline);
      if (f.help) body.append(h("div", { class: "help" }, f.help));
      wrap.append(body);
      if (f.showIf) conditional.push([wrap, f.showIf]);
      parent.append(wrap);
    }
  };
  render(fields, el);
  const get = () => Object.fromEntries(getters.map(([k, g]) => [k, g()]));
  const pv = preview ? h("div", { class: "preview" }) : null;
  if (pv) el.append(pv);
  const update = () => {
    let vals; try { vals = get(); } catch { return; }
    conditional.forEach(([w, cond]) => (w.style.display = cond(vals) ? "" : "none"));
    if (pv) { try { pv.innerHTML = preview(vals, ctx) || ""; } catch { pv.innerHTML = ""; } }
  };
  el.addEventListener("input", update);
  el.addEventListener("change", update);
  update();
  return { el, get };
}

// ------------------------------------------------------------------ catalog context & helpers
async function loadCtx() {
  const [types, recipes, orders] = await Promise.all([api("GET", "/api/product-types"), api("GET", "/api/recipes"), api("GET", "/api/orders")]);
  return { types, recipes, orders };
}
function typeLabel(ctx, id) { const t = ctx.types.find((x) => x.id === id); return t ? (t.name && t.name !== id ? `${t.name} (${id})` : id) : id; }
const typeName = (ctx, id) => ctx.types.find((x) => x.id === id)?.name || id;
const itemsText = (ctx, items) => items.map((i) => `${i.quantity}× ${typeName(ctx, i.product_type_id)}${i.is_byproduct ? " (by-product)" : ""}`).join(" + ");
const n2 = (x, d = 2) => (Number.isFinite(x) ? +x.toFixed(d) : "–");

function speedMultiplier(points, s) {
  const pts = (points || [[1, 2], [50, 1], [100, 0.5]]).filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1])).sort((a, b) => a[0] - b[0]);
  if (!pts.length) return NaN;
  if (s <= pts[0][0]) return pts[0][1];
  for (let i = 0; i + 1 < pts.length; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[i + 1];
    if (s <= x1) return y0 + (y1 - y0) * (s - x0) / (x1 - x0);
  }
  return pts[pts.length - 1][1];
}
function qualityAt(q, speed) {
  const m = { q_at_1: 1, q_at_50: 0.95, q_at_100: 0.75, steepness: 10, ...(q || {}) };
  const s = Math.min(100, Math.max(1, speed));
  if (s <= 50) return m.q_at_1 + (m.q_at_50 - m.q_at_1) * (s - 1) / 49;
  const x = (s - 50) / 50, k = m.steepness, sig = (z) => 1 / (1 + Math.exp(-z));
  const lo = sig(-k / 2), hi = sig(k / 2);
  return m.q_at_50 + (m.q_at_100 - m.q_at_50) * (sig(k * (x - 0.5)) - lo) / (hi - lo);
}

// ------------------------------------------------------------------ asset schemas
const STATE_FIELD = { key: "initial_state", label: "Initial state", type: "select",
  options: [["running", "Running"], ["stopped", "Stopped (start manually)"]] };
const UNIT_S = { seconds: 1, minutes: 60, hours: 3600 };

const ASSET_SCHEMAS = {
  producer: [
    { key: "product_type_id", label: "Produces", type: "select", options: (c) => c.types.map((t) => [t.id, typeLabel(c, t.id)]) },
    { key: "batch_quantity", label: "Batch size", type: "number", int: true, min: 1, help: "Products created per batch" },
    { row: "Every", fields: [{ key: "interval", type: "number", min: 0, step: "any" },
      { key: "interval_unit", type: "select", options: [["seconds", "seconds"], ["minutes", "minutes"], ["hours", "hours"]] }] },
    { key: "defect_probability", label: "Defect rate", type: "percent" },
    { key: "production_order_id", label: "Production order", type: "select", options: (c) => c.orders.map((o) => [o.id, o.name === o.id ? o.id : `${o.name} (${o.id})`]) },
    { key: "attributes", label: "Product attributes", type: "kv", help: "Stamped on every product (usable by splitter rules and sink criteria)" },
    { section: "Advanced", collapsed: true, fields: [
      { key: "max_pending_batches", label: "Output buffer", type: "number", int: true, min: 1, suffix: "batches", help: "Producer blocks when this many batches wait for the output conveyor" },
      { key: "start_delay_s", label: "Start delay", type: "number", min: 0, suffix: "s" },
      STATE_FIELD] },
  ],
  conveyor: [
    { key: "length_m", label: "Length", type: "number", min: 0, suffix: "m" },
    { key: "speed_m_s", label: "Speed", type: "number", min: 0, suffix: "m/s" },
    { key: "capacity_per_m", label: "Density", type: "number", min: 0, suffix: "products/m" },
    { section: "Advanced", collapsed: true, fields: [STATE_FIELD] },
  ],
  transformer: [
    { key: "recipe_id", label: "Recipe", type: "select", nullable: true, nullLabel: "— none (unconfigured) —",
      options: (c) => c.recipes.map((r) => [r.id, r.name === r.id ? r.id : `${r.name} (${r.id})`]) },
    { key: "speed_pct", label: "Speed", type: "slider", min: 1, max: 100, suffix: "%", help: "Faster cycles produce lower quality (more defects)" },
    { section: "Buffers", collapsed: true, fields: [
      { key: "input_buffer_multiplier", label: "Input buffer", type: "number", int: true, min: 1, suffix: "× recipe qty" },
      { key: "output_buffer_multiplier", label: "Output buffer", type: "number", int: true, min: 1, suffix: "× recipe qty" }] },
    { section: "Speed & quality curves", collapsed: true, fields: [
      { key: "speed_model", label: "Cycle time", type: "object", fields: [{ key: "points", label: "", type: "speedPoints" }],
        help: "Processing-time multiplier at each speed (linear in between)" },
      { key: "quality_model", label: "Quality", type: "object", fields: [
        { key: "q_at_1", label: "at 1 %", type: "number", min: 0, max: 1, step: 0.01 },
        { key: "q_at_50", label: "at 50 %", type: "number", min: 0, max: 1, step: 0.01 },
        { key: "q_at_100", label: "at 100 %", type: "number", min: 0, max: 1, step: 0.01 },
        { key: "steepness", label: "Steepness", type: "number", min: 0.1, max: 100, step: "any" }] }] },
    { section: "Advanced", collapsed: true, fields: [STATE_FIELD] },
  ],
  splitter: [
    { key: "mode", label: "Routing", type: "select", options: [["percentage", "Percentage split"], ["conditional", "Rules (by product)"]] },
    { key: "percentage_a", label: "Share to A", type: "slider", min: 0, max: 100, step: 1, suffix: "%", showIf: (v) => v.mode === "percentage",
      help: "The remaining share goes to output B" },
    { key: "rules", label: "Rules", type: "conditions", branch: true, showIf: (v) => v.mode === "conditional", addLabel: "+ Add rule",
      empty: "no rules yet", help: "First matching rule wins. Attribute: is_defective, product_type_id, quality_score, lot_number, attributes.<name>, measurements.<name>" },
    { key: "fallback_branch", label: "Otherwise", type: "select", options: [["A", "→ A"], ["B", "→ B"]], showIf: (v) => v.mode === "conditional" },
    { section: "Advanced", collapsed: true, fields: [
      { key: "buffer_capacity", label: "Buffer", type: "number", int: true, min: 1, suffix: "products" }, STATE_FIELD] },
  ],
  sink: [
    { key: "accepted_product_type_ids", label: "Accepts", type: "typeSet" },
    { key: "capacity", label: "Capacity", type: "nullableInt", nullLabel: "unlimited" },
    { key: "completion_criteria", label: "Completed when", type: "conditions", empty: "always (no criteria)",
      help: "All conditions must match, otherwise the product is stored as not completed" },
    { section: "Advanced", collapsed: true, fields: [STATE_FIELD] },
  ],
};

const ASSET_DEFAULTS = {
  producer: (c) => ({ product_type_id: c.types[0]?.id, batch_quantity: 1, interval: 10, interval_unit: "seconds",
    max_pending_batches: 1, production_order_id: c.orders[0]?.id, defect_probability: 0, start_delay_s: 0, attributes: {}, initial_state: "running" }),
  transformer: (c) => ({ recipe_id: c.recipes[0]?.id ?? null, speed_pct: 50, input_buffer_multiplier: 2, output_buffer_multiplier: 2,
    speed_model: { points: [[1, 2], [50, 1], [100, 0.5]] }, quality_model: { q_at_1: 1, q_at_50: 0.95, q_at_100: 0.75, steepness: 10 }, initial_state: "running" }),
  splitter: () => ({ mode: "percentage", percentage_a: 50, rules: [], fallback_branch: "B", buffer_capacity: 1, initial_state: "running" }),
  sink: () => ({ accepted_product_type_ids: null, capacity: null, completion_criteria: [], initial_state: "running" }),
};

const ASSET_PREVIEWS = {
  producer: (v) => {
    const per = (v.interval || 0) * (UNIT_S[v.interval_unit] || 1);
    if (!per || !v.batch_quantity) return "";
    return `≈ <b>${n2(v.batch_quantity * 60 / per)}</b> products/min · one batch every ${n2(per, 1)} s`;
  },
  conveyor: (v) => {
    const cap = Math.floor((v.length_m || 0) * (v.capacity_per_m || 0) + 1e-9), tt = v.length_m / v.speed_m_s;
    return `Holds <b>${cap}</b> product${cap === 1 ? "" : "s"} · transit <b>${n2(tt, 1)} s</b>${cap < 1 ? ' · <span class="bad">capacity must be ≥ 1</span>' : ""}`;
  },
  transformer: (v, c) => {
    const r = c.recipes.find((x) => x.id === v.recipe_id);
    const mult = speedMultiplier(v.speed_model?.points, v.speed_pct), q = qualityAt(v.quality_model, v.speed_pct);
    const head = r ? `<div>${esc(itemsText(c, r.inputs))} → ${esc(itemsText(c, r.outputs))}</div>` : '<div class="bad">No recipe – the transformer stays unconfigured</div>';
    return head + `<div>Cycle ${r ? `<b>${n2(r.processing_time_s * mult, 1)} s</b> (${r.processing_time_s} s × ${n2(mult)})` : `time × ${n2(mult)}`}
      · quality <b>${n2(q, 3)}</b> · defect chance <b>${n2((1 - q) * 100, 1)} %</b></div>`;
  },
  splitter: (v) => (v.mode === "percentage" ? `A: <b>${n2(v.percentage_a, 1)} %</b> · B: <b>${n2(100 - v.percentage_a, 1)} %</b>` : ""),
};

// ------------------------------------------------------------------ catalog schemas
const CATALOG = {
  type: { title: "product type", path: "/api/product-types", fields: (editing) => [
    { key: "name", label: "Name", type: "text", placeholder: "e.g. Steel plate" },
    { key: "id", label: "ID", type: "text", readonly: editing, placeholder: "auto from name", help: editing ? "IDs are immutable" : "Letters, digits, - and _" },
    { key: "description", label: "Description", type: "text", multiline: true },
    { key: "metadata", label: "Metadata", type: "kv" }] },
  recipe: { title: "recipe", path: "/api/recipes", fields: (editing) => [
    { key: "name", label: "Name", type: "text", placeholder: "e.g. Smelt iron" },
    { key: "id", label: "ID", type: "text", readonly: editing, placeholder: "auto from name", help: editing ? "IDs are immutable" : null },
    { key: "inputs", label: "Consumes", type: "recipeItems", max: 5, addLabel: "+ Add input", help: "Up to 5 inputs (one input port each)" },
    { key: "outputs", label: "Produces", type: "recipeItems", max: 3, byproduct: true, addLabel: "+ Add output", help: "Up to 3 outputs (one output port each)" },
    { key: "processing_time_s", label: "Processing time", type: "number", min: 0, suffix: "s (base, × speed factor)" },
    { key: "defect_policy", label: "Defects", type: "select", options: [["any_defective_input", "Cascade: defective if any input is defective"],
      ["independent", "Machine quality only (ignore input defects)"]] },
    { key: "description", label: "Description", type: "text", multiline: true },
    { key: "parameters", label: "Parameters", type: "kv", addLabel: "+ Add parameter" }] },
  order: { title: "production order", path: "/api/orders", fields: (editing) => [
    { key: "name", label: "Name", type: "text" },
    { key: "id", label: "ID", type: "text", readonly: editing, placeholder: "auto from name", help: editing ? "IDs are immutable" : null },
    { key: "description", label: "Description", type: "text", multiline: true },
    { key: "attributes", label: "Attributes", type: "kv" }] },
};

// ------------------------------------------------------------------ dialogs
function closeModal() { $("#modal").classList.add("hidden"); }
function openFormDialog({ title, subtitle, fields, value, ctx, preview, submitLabel = "Save", onSubmit, onBuilt }) {
  const form = buildForm(fields, value, ctx, preview);
  const err = h("div", { class: "form-error" });
  const submit = h("button", { type: "button", class: "primary" }, submitLabel);
  submit.onclick = async () => {
    err.textContent = ""; submit.disabled = true;
    try { await onSubmit(form.get()); closeModal(); }
    catch (e) { err.textContent = e.message; }
    finally { submit.disabled = false; }
  };
  const box = h("div", { class: "dialog" }, h("h3", {}, title), subtitle ? h("p", { class: "hint" }, subtitle) : null, form.el, err,
    h("div", { class: "row end" }, h("button", { type: "button", onclick: closeModal }, "Cancel"), submit));
  $("#modal-content").replaceChildren(box);
  $("#modal").classList.remove("hidden");
  if (onBuilt) onBuilt(form, box);
  box.querySelector("input:not([readonly]), select")?.focus();
  return form;
}

async function openCatalogDialog(kind, existing) {
  const spec = CATALOG[kind];
  const ctx = await loadCtx();
  if (kind === "recipe" && !ctx.types.length) return toast("Create a product type first");
  const editing = !!existing;
  const value = existing || (kind === "recipe" ? { inputs: [], outputs: [], processing_time_s: 10, defect_policy: "any_defective_input" } : {});
  openFormDialog({
    title: `${editing ? "Edit" : "New"} ${spec.title}`, fields: spec.fields(editing), value, ctx,
    preview: kind === "recipe" ? (v, c) => `${esc(itemsText(c, v.inputs))} → ${esc(itemsText(c, v.outputs))}` : null,
    submitLabel: editing ? "Save" : "Create",
    onBuilt: (form, box) => {
      if (editing) return;
      const [nameIn, idIn] = box.querySelectorAll(".form > .f input");
      let touched = false;
      idIn.addEventListener("input", () => (touched = idIn.value !== ""));
      nameIn.addEventListener("input", () => { if (!touched) idIn.value = slug(nameIn.value); });
    },
    onSubmit: async (v) => {
      if (!v.id) v.id = slug(v.name || "");
      if (editing) await api("PUT", `${spec.path}/${encodeURIComponent(existing.id)}`, v);
      else await api("POST", spec.path, v);
      toast(`${spec.title} ${editing ? "saved" : "created"}`, true);
      renderCatalog();
    },
  });
}

async function openAssetDialog(kind, position) {
  const ctx = await loadCtx();
  if (kind === "producer" && !ctx.types.length) { showTab("catalog"); return toast("Create a product type first (Catalog tab)"); }
  const fields = [{ key: "name", label: "Name", type: "text" }, ...ASSET_SCHEMAS[kind]];
  const value = { name: `${kind[0].toUpperCase()}${kind.slice(1)} ${nodes().filter((a) => a.kind === kind).length + 1}`, ...ASSET_DEFAULTS[kind](ctx) };
  openFormDialog({
    title: `New ${kind}`, fields, value, ctx, preview: ASSET_PREVIEWS[kind], submitLabel: "Add to plant",
    subtitle: kind === "transformer" && !ctx.recipes.length ? "Tip: create a recipe in the Catalog tab first." : null,
    onSubmit: async ({ name, ...config }) => {
      const a = await api("POST", "/api/assets", { kind, name, config, position });
      toast(`${kind} added`, true);
      await refresh(); select(a.id);
    },
  });
}

// ------------------------------------------------------------------ integration options
const INTEGRATION_FIELDS = {
  memory: [{ key: "max_messages", label: "Keep last", type: "number", int: true, min: 1, suffix: "messages" }],
  mqtt: [{ key: "host", label: "Host", type: "text" }, { key: "port", label: "Port", type: "number", int: true, min: 1 },
    { key: "qos", label: "QoS", type: "select", options: [[0, "0 – at most once"], [1, "1 – at least once"], [2, "2 – exactly once"]] }],
  kafka: [{ key: "bootstrap_servers", label: "Bootstrap servers", type: "text" }],
  pulsar: [{ key: "service_url", label: "Service URL", type: "text" }],
  none: [],
};
const INTEGRATION_DEFAULTS = { memory: { max_messages: 10000 }, mqtt: { host: "localhost", port: 1883, qos: 1 },
  kafka: { bootstrap_servers: "localhost:9092" }, pulsar: { service_url: "pulsar://localhost:6650" }, none: {} };
