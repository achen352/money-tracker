"use strict";

// ---------------------------------------------------------------------------------------
// State & helpers
// ---------------------------------------------------------------------------------------

const $ = (sel, el = document) => el.querySelector(sel);
const state = { range: "6", version: null, data: null, knownCustom: null, modes: {}, cards: new Map() };
try { state.range = localStorage.getItem("money.range") || "6"; } catch (_) { /* storage unavailable */ }

// Embedded in VS Code (see vscode/extension.js): theme + background come from the editor.
const QS = new URLSearchParams(location.search);
const EMBED = QS.has("embed");
function applyLook(theme, bg) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.dataset.theme = theme;
  if (bg && /^(#[0-9a-f]{3,8}|rgba?\([\d.,\s%]+\))$/i.test(bg)) root.style.setProperty("--page", bg);
}
applyLook(QS.get("theme"), QS.get("bg"));
if (EMBED) document.documentElement.classList.add("embed");

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const palette = () => Array.from({ length: 8 }, (_, i) => css(`--series-${i + 1}`));

const usdFull = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const usdCompact = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
const num = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const pct = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function fmt(v, f, mode) {
  if (v === null || v === undefined || v === "") return "";
  if (typeof v !== "number") return String(v);
  if (f === "number") return num.format(v);
  if (f === "percent") return pct.format(v);
  if (mode === "stat") return Math.abs(v) >= 100000 ? usdCompact.format(v) : usd0.format(v);
  return usdFull.format(v);
}
const colFormat = (spec, col) => (spec.formats && spec.formats[col]) || spec.format || "usd";
const isMonth = (v) => typeof v === "string" && /^\d{4}-\d{2}$/.test(v);
const monthLabel = (v) => (isMonth(v) ? `${MONTHS[+v.slice(5) - 1]} '${v.slice(2, 4)}` : v);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c.nodeType ? c : String(c));
  return node;
}

function ago(iso) {
  if (!iso) return "never";
  const mins = (Date.now() - new Date(iso).getTime()) / 60000;
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.round(mins)}m ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

// ---------------------------------------------------------------------------------------
// Data shaping
// ---------------------------------------------------------------------------------------

function seriesFrom(spec) {
  const cols = spec.columns, rows = spec.rows;
  const xCol = spec.x || cols[0];
  const xi = cols.indexOf(xCol);
  if (xi < 0) throw new Error(`x column "${xCol}" not in result columns [${cols.join(", ")}]`);
  if (spec.series) {
    const si = cols.indexOf(spec.series);
    if (si < 0) throw new Error(`series column "${spec.series}" not in result`);
    const yCol = Array.isArray(spec.y) ? spec.y[0] : (spec.y || cols.find((_, i) => i !== xi && i !== si));
    const yi = cols.indexOf(yCol);
    if (yi < 0) throw new Error(`y column "${yCol}" not in result`);
    const xs = [...new Set(rows.map((r) => r[xi]))];
    const names = [...new Set(rows.map((r) => r[si]))];
    const series = names.map((n) => {
      const m = new Map();
      for (const r of rows) if (r[si] === n) m.set(r[xi], (m.get(r[xi]) || 0) + (r[yi] || 0));
      return { name: String(n), y: xs.map((x) => (m.has(x) ? m.get(x) : 0)), col: yCol };
    });
    // Larger series first, "Other" last.
    series.sort((a, b) => (a.name === "Other") - (b.name === "Other") || sum(b.y) - sum(a.y));
    return { xs, series, xCol };
  }
  const ys = spec.y ? (Array.isArray(spec.y) ? spec.y : [spec.y])
    : cols.filter((_, i) => i !== xi && rows.some((r) => typeof r[i] === "number"));
  for (const c of ys) if (!cols.includes(c)) throw new Error(`y column "${c}" not in result`);
  return {
    xs: rows.map((r) => r[xi]),
    xCol,
    series: ys.map((c) => { const i = cols.indexOf(c); return { name: c, y: rows.map((r) => r[i]), col: c }; }),
  };
}
const sum = (a) => a.reduce((s, v) => s + (+v || 0), 0);

// Color follows the entity: spending categories keep the same hue in every chart.
function assignColors(series, spec) {
  const pal = palette(), other = css("--series-other");
  const order = spec.series === "category" ? (state.data.category_order || []) : [];
  const taken = new Set(series.map((s) => order.indexOf(s.name)).filter((k) => k >= 0 && k < 8));
  const free = [...Array(8).keys()].filter((k) => !taken.has(k));
  for (const s of series) {
    const k = order.indexOf(s.name);
    if (s.name === "Other") s.color = other;
    else if (k >= 0 && k < 8) s.color = pal[k];
    else s.color = free.length ? pal[free.shift()] : other; // never generate a 9th hue
  }
}

// ---------------------------------------------------------------------------------------
// Charts (Plotly)
// ---------------------------------------------------------------------------------------

function axisFormat(values, f) {
  const max = Math.max(0, ...values.map((v) => Math.abs(+v || 0)));
  if (f === "percent") return { tickformat: ".0%" };
  if (f === "number") return { tickformat: max >= 10000 ? "~s" : ",~f" };
  return { tickprefix: "$", tickformat: max >= 10000 ? "~s" : ",.0f" };
}

function baseLayout(height) {
  return {
    height,
    margin: { l: 8, r: 12, t: 8, b: 8 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: 'system-ui, -apple-system, "Segoe UI", sans-serif', size: 12, color: css("--ink-2") },
    xaxis: { showgrid: false, linecolor: css("--axis"), ticks: "", automargin: true, fixedrange: true, zeroline: false },
    yaxis: { gridcolor: css("--grid"), gridwidth: 1, zerolinecolor: css("--axis"), linecolor: css("--axis"),
             showline: false, automargin: true, fixedrange: true, ticks: "" },
    legend: { orientation: "h", x: 0, y: 1.02, yanchor: "bottom", font: { color: css("--ink-2") }, traceorder: "normal" },
    hoverlabel: { bgcolor: css("--surface"), bordercolor: css("--axis"), font: { color: css("--ink") } },
    barcornerradius: 4,
    showlegend: false,
  };
}

// Bars are capped at ~24px thick; the leftover band is air.
function barGap(widthPx, slots, barsPerSlot) {
  const slot = Math.max(1, widthPx) / Math.max(1, slots);
  const want = Math.min(24 * barsPerSlot, slot * 0.75);
  return Math.max(0.1, Math.min(0.95, 1 - want / slot));
}

// HTML legend above the plot: wraps at any width (Plotly's own legend turns into a
// scrolling column in a narrow sidebar). Present for 2+ series; text stays in ink colors.
function renderLegend(el_, series, type) {
  if (!el_) return;
  if (series.length < 2 || type === "hbar") { el_.replaceChildren(); return; }
  const lineKey = type === "line" || type === "area";
  el_.replaceChildren(...series.map((s) => el("span", { class: "key" },
    el("i", { class: lineKey ? "line" : "", style: `background:${s.color}` }), s.name)));
}

function plot(div, spec, legendEl) {
  const { xs, series } = seriesFrom(spec);
  assignColors(series, spec);
  const type = spec.chart;
  renderLegend(legendEl, series, type);
  const f = colFormat(spec, series[0] && series[0].col);
  const surface = css("--surface");
  const width = div.clientWidth || 400;
  // Month categories: unique full labels as values; when space is tight, ticks drop the year
  // except on the first month and on January.
  const xLabels = xs.map((v) => monthLabel(v));
  const compact = (width - 60) / Math.max(1, xs.length) < 56;
  const tickText = xs.map((v, i) => (isMonth(v) && compact && i > 0 && !v.endsWith("-01") ? MONTHS[+v.slice(5) - 1] : monthLabel(v)));
  let traces, layout;

  if (type === "hbar") {
    const s = series[0];
    const n = xs.length;
    layout = baseLayout(Math.max(120, n * 30 + 24));
    traces = [{
      type: "bar", orientation: "h", y: xLabels, x: s.y, name: s.name,
      marker: { color: s.color, line: { color: surface, width: 0 } },
      text: s.y.map((v) => fmt(v, f, "stat")), textposition: "outside", cliponaxis: false,
      textfont: { color: css("--ink-2"), size: 12 },
      customdata: s.y.map((v) => fmt(v, f)),
      hovertemplate: "%{y}: %{customdata}<extra></extra>",
    }];
    layout.yaxis = { ...layout.yaxis, autorange: "reversed", gridcolor: "rgba(0,0,0,0)", type: "category" };
    layout.xaxis = { ...layout.xaxis, ...axisFormat(s.y, f), showgrid: true, gridcolor: css("--grid"),
                     showticklabels: false, range: [0, Math.max(...s.y.map(Number), 1) * 1.22] };
    layout.bargap = 1 - 20 / 30;
    layout.hovermode = "closest";
  } else if (type === "line" || type === "area") {
    layout = baseLayout(260);
    const allY = series.flatMap((s) => s.y);
    traces = series.map((s) => ({
      type: "scatter", mode: "lines", x: xs.map((v) => (isMonth(v) ? v + "-01" : v)), y: s.y, name: s.name,
      line: { color: s.color, width: 2, shape: "linear" },
      fill: type === "area" ? "tozeroy" : "none",
      fillcolor: type === "area" ? hexAlpha(s.color, 0.1) : undefined,
      customdata: s.y.map((v) => fmt(v, f)),
      hovertemplate: `${series.length > 1 ? s.name + ": " : ""}%{customdata}<extra></extra>`,
    }));
    // End-dot + direct label on the last point of each series (selective labeling).
    for (const s of series) {
      const lastX = xs[xs.length - 1];
      traces.push({
        type: "scatter", mode: "markers", x: [isMonth(lastX) ? lastX + "-01" : lastX], y: [s.y[s.y.length - 1]],
        marker: { color: s.color, size: 9, line: { color: surface, width: 2 } },
        hoverinfo: "skip", showlegend: false,
      });
    }
    layout.yaxis = { ...layout.yaxis, ...axisFormat(allY, f) };
    if (xs.length && (isMonth(xs[0]) || /^\d{4}-\d{2}-\d{2}/.test(xs[0]))) layout.xaxis.type = "date";
    layout.hovermode = "x unified";
  } else { // bar | stacked
    layout = baseLayout(280);
    const stacked = type === "stacked";
    const perSlot = stacked ? 1 : series.length;
    layout.barmode = stacked ? "relative" : "group";
    layout.bargap = barGap(width - 70, xs.length, perSlot);
    layout.bargroupgap = stacked ? 0 : 0.12;
    traces = series.map((s) => ({
      type: "bar", x: xLabels, y: s.y, name: s.name,
      marker: { color: s.color, line: { color: surface, width: stacked ? 1 : 0 } },
      customdata: s.y.map((v) => fmt(v, f)),
      hovertemplate: `${s.name}: %{customdata}<extra></extra>`,
    }));
    layout.xaxis = { ...layout.xaxis, type: "category", tickangle: 0, tickvals: xLabels, ticktext: tickText };
    const totals = stacked ? xs.map((_, i) => sum(series.map((s) => s.y[i]))) : series.flatMap((s) => s.y);
    layout.yaxis = { ...layout.yaxis, ...axisFormat(totals.concat(series.flatMap((s) => s.y)), f) };
    layout.hovermode = "x unified";
  }
  Plotly.react(div, traces, layout, { displayModeBar: false, responsive: true }).then(() => {
    // Re-fit the bar gap to the real plot width so columns stay <= ~24px thick.
    if (type !== "bar" && type !== "stacked") return;
    const plotW = div._fullLayout && div._fullLayout._size && div._fullLayout._size.w;
    if (!plotW) return;
    const gap = barGap(plotW, xs.length, type === "stacked" ? 1 : series.length);
    if (Math.abs(gap - layout.bargap) > 0.01) Plotly.relayout(div, { bargap: gap });
  });
}

function hexAlpha(hex, a) {
  const h = hex.replace("#", "");
  if (h.length !== 6) return hex;
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return `rgba(${r},${g},${b},${a})`;
}

// ---------------------------------------------------------------------------------------
// Tables & stat tiles
// ---------------------------------------------------------------------------------------

function renderTable(spec, limit = 60) {
  const { columns, rows } = spec;
  const numeric = columns.map((_, i) => rows.length > 0 && rows.every((r) => r[i] === null || typeof r[i] === "number"));
  const signed = columns.map((c) => /^(amount|net)$/i.test(c));
  const shown = rows.slice(0, limit);
  const table = el("table", {},
    el("thead", {}, el("tr", {}, columns.map((c, i) => el("th", { class: numeric[i] ? "num" : "" }, c)))),
    el("tbody", {}, shown.map((r) => el("tr", {}, r.map((v, i) => {
      let cls = numeric[i] ? "num" : (typeof v === "string" && v.length > 28 ? "wrap" : "");
      if (signed[i] && typeof v === "number" && v > 0) cls += " pos";
      const text = numeric[i] ? (signed[i] && v > 0 ? "+" : "") + fmt(v, colFormat(spec, columns[i])) : fmt(v);
      return el("td", { class: cls }, isMonth(v) ? monthLabel(v) : text);
    })))),
  );
  const wrap = el("div", { class: "table-wrap" }, table);
  if (rows.length > limit) {
    wrap.append(el("button", { type: "button", onclick: () => wrap.replaceWith(renderTable(spec, rows.length)) },
      `Show all ${rows.length} rows`));
  }
  return wrap;
}

function renderStats(spec, heroFirst) {
  const { columns, rows } = spec;
  const row = rows[0] || [];
  const tiles = [];
  columns.forEach((c, i) => {
    if (c.endsWith("__compare")) return;
    const f = colFormat(spec, c);
    const tile = el("div", { class: "stat" + (heroFirst && tiles.length === 0 ? " hero" : "") },
      el("div", { class: "label" }, c),
      el("div", { class: "value" }, row[i] === null || row[i] === undefined ? "–" : fmt(row[i], f, "stat")));
    const ci = columns.indexOf(c + "__compare");
    if (ci >= 0 && typeof row[ci] === "number") {
      tile.append(el("div", { class: "delta" }, `vs ${fmt(row[ci], f, "stat")} at this point last month`));
    }
    tiles.push(tile);
  });
  return el("div", { class: "stats" }, tiles);
}

// ---------------------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------------------

function card(spec, opts = {}) {
  const key = (opts.custom ? "c:" : "b:") + spec.id;
  let node = state.cards.get(key);
  if (!node) {
    node = el("article", { class: "card", "data-key": key });
    state.cards.set(key, node);
  }
  const canChart = ["bar", "hbar", "stacked", "line", "area"].includes(spec.chart);
  const mode = state.modes[key] || (canChart ? "chart" : "table");

  const tools = el("div", { class: "tools" });
  if (canChart && spec.rows && spec.rows.length) {
    for (const m of ["chart", "table"]) {
      tools.append(el("button", { type: "button", "aria-pressed": String(mode === m),
        onclick: () => { state.modes[key] = m; render(); } }, m === "chart" ? "Chart" : "Table"));
    }
  }
  if (opts.custom) {
    tools.append(el("button", { type: "button", title: "Remove this view (deletes data/views/" + spec.id + ".json)",
      onclick: async () => {
        await fetch(`/api/views/${encodeURIComponent(spec.id)}`, { method: "DELETE" });
        refresh(true);
      } }, "Remove"));
  }

  const head = el("div", { class: "card-head" },
    el("div", { class: "card-title" }, el("h3", {}, spec.title || spec.id),
      spec.subtitle ? el("p", { class: "sub" }, spec.subtitle) : null),
    tools);

  const body = el("div");
  if (spec.error) {
    body.append(el("div", { class: "error" }, spec.error));
  } else if (!spec.rows || !spec.rows.length) {
    body.append(el("div", { class: "empty" }, "No data for this range."));
  } else if (spec.chart === "stat") {
    body.append(renderStats(spec, false));
  } else if (mode === "table" || !canChart) {
    body.append(renderTable(spec));
  } else {
    const legend = el("div", { class: "legend" });
    const div = el("div", { class: "chart" });
    body.append(legend, div);
    requestAnimationFrame(() => {
      try { plot(div, spec, legend); } catch (e) { div.replaceWith(el("div", { class: "error" }, String(e.message || e))); }
    });
  }
  node.querySelectorAll(".js-plotly-plot").forEach((p) => Plotly.purge(p));
  node.replaceChildren(head, body, spec.note ? el("p", { class: "note" }, spec.note) : "");
  return node;
}

// ---------------------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------------------

function renderHeader(d) {
  const parts = [];
  if (d.demo) parts.push("Demo data");
  parts.push(d.syncing ? "Syncing…" : `Synced ${ago(d.last_sync)}`);
  const status = $("#status");
  status.replaceChildren(parts.join(" · "));
  if (d.sync_error) status.append(" · ", el("span", { class: "err" }, d.sync_error));
  $("#sync").disabled = d.syncing || d.demo;
  $("#sync").textContent = d.syncing ? "Syncing…" : "Sync";

  const banners = [];
  if (d.demo) {
    banners.push(el("div", { class: "banner" }, "You're looking at fake demo data. To use your real accounts, stop this server and run ",
      el("code", {}, "python -m money serve"), "."));
  } else if (!d.has_data) {
    banners.push(el("div", { class: "banner" },
      d.plaid_configured
        ? ["No accounts yet. ", el("a", { href: "/link", target: "_blank", rel: "noopener" }, "Link your first account"),
           " (opens in a browser window, since bank logins use pop-ups)."]
        : ["No accounts yet. Add your Plaid keys to ", el("code", {}, ".env"), " (see README.md), restart, then use Link account."]));
  }
  for (const it of d.items || []) {
    if (it.status === "login_required") {
      banners.push(el("div", { class: "banner" }, `${it.institution_name} needs you to log in again. `,
        el("a", { href: `/link?update=${encodeURIComponent(it.item_id)}`, target: "_blank", rel: "noopener" }, "Reconnect")));
    } else if (it.status === "error") {
      banners.push(el("div", { class: "banner" }, `${it.institution_name}: ${it.error || "sync error"}`));
    }
  }
  $("#banners").replaceChildren(...banners);
}

function render() {
  const d = state.data;
  if (!d) return;
  renderHeader(d);

  const kpi = d.builtin.find((v) => v.id === "kpis");
  $("#kpis").replaceChildren(kpi && d.has_data && !kpi.error ? renderStats(kpi, true) : "");

  // Claude's views, newest first; highlight ones that appeared since the last refresh.
  const section = $("#claude-section");
  const grid = $("#claude");
  section.hidden = !d.has_data && !d.custom.length;
  const ids = new Set(d.custom.map((v) => v.id));
  const fresh = state.knownCustom ? d.custom.filter((v) => !state.knownCustom.has(v.id) || v.updated > (state.knownCustom.get?.(v.id) || 0)) : [];
  if (d.custom.length) {
    grid.replaceChildren(...d.custom.map((v) => card(v, { custom: true })));
  } else {
    grid.replaceChildren(el("div", { class: "card placeholder" },
      "Ask Claude for a chart (e.g. “graph my dining spending by month”) and it shows up here."));
  }
  if (fresh.length) {
    const node = state.cards.get("c:" + fresh[0].id);
    if (node) {
      node.classList.add("fresh");
      node.scrollIntoView({ behavior: "smooth", block: "nearest" });
      setTimeout(() => node.classList.remove("fresh"), 4000);
    }
  }
  state.knownCustom = new Map(d.custom.map((v) => [v.id, v.updated]));
  for (const key of [...state.cards.keys()]) if (key.startsWith("c:") && !ids.has(key.slice(2))) state.cards.delete(key);

  const builtin = d.builtin.filter((v) => v.id !== "kpis" && !(v.hide_empty && (!v.rows || !v.rows.length) && !v.error));
  $("#builtin").replaceChildren(...(d.has_data ? builtin.map((v) => card(v)) : []));
}

async function refresh(force = false) {
  const grids = document.querySelectorAll(".card");
  grids.forEach((c) => c.classList.add("stale")); // keep the old render visible while loading
  try {
    const res = await fetch(`/api/dashboard?range=${encodeURIComponent(state.range)}`);
    const data = await res.json();
    state.data = data;
    state.version = data.version;
    render();
  } catch (e) {
    $("#status").textContent = "Can't reach the local server - is `python -m money serve` running?";
  } finally {
    document.querySelectorAll(".card.stale").forEach((c) => c.classList.remove("stale"));
  }
}

async function poll() {
  try {
    const res = await fetch("/api/version");
    const { v } = await res.json();
    if (v !== state.version) await refresh();
  } catch (_) { /* server restarting */ }
  setTimeout(poll, 2000);
}

$("#range").value = state.range;
$("#range").addEventListener("change", (e) => {
  state.range = e.target.value;
  try { localStorage.setItem("money.range", state.range); } catch (_) { /* ignore */ }
  refresh(true);
});
$("#sync").addEventListener("click", async () => {
  $("#sync").disabled = true;
  const res = await fetch("/api/sync", { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (body.error) $("#status").textContent = body.error;
  refresh(true);
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => render());
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 200); });

if (EMBED) {
  window.addEventListener("message", (e) => {
    if (e.source !== window.parent) return;
    const m = e.data || {};
    if (m.type === "money:theme") { applyLook(m.theme, m.bg); render(); }
  });
  // Webviews can't open new windows: hand "open in browser" links (Plaid Link, Reconnect) to VS Code.
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest('a[target="_blank"]');
    if (!a) return;
    e.preventDefault();
    window.parent.postMessage({ type: "money:open-external", url: a.href }, "*");
  });
}

refresh().then(poll);
