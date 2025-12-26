const MANIFEST_URL = window.__FFT_MANIFEST_PATH__ || "assets/docs_manifest.json";

// function el(tag, attrs = {}, ...children) {
//   const n = document.createElement(tag);
//   for (const [k, v] of Object.entries(attrs || {})) {
//     if (k === "class") n.className = v;
//     else if (k === "html") n.innerHTML = v;
//     else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2).toLowerCase(), v);
//     else n.setAttribute(k, String(v));
//   }
//   for (const c of children) {
//     if (c == null) continue;
//     n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
//   }
//   return n;
// }

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;

    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function")
      n.addEventListener(k.slice(2).toLowerCase(), v);

    // ✅ critical: boolean attributes
    else if (k === "disabled") n.disabled = !!v;
    else if (typeof v === "boolean") {
      if (v) n.setAttribute(k, "");
      // if false: omit attribute
    }

    else n.setAttribute(k, String(v));
  }

  for (const c of children) {
    if (c == null) continue;
    n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return n;
}

function safeGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function safeSet(key, value) {
  try { localStorage.setItem(key, value); } catch {}
}
function safeGetJSON(key, fallback) {
  const raw = safeGet(key);
  if (!raw) return fallback;
  try { return JSON.parse(raw); } catch { return fallback; }
}
function safeSetJSON(key, obj) {
  safeSet(key, JSON.stringify(obj));
}

function stripHtml(html) {
  if (!html) return "";
  const div = document.createElement("div");
  div.innerHTML = html;
  return (div.textContent || div.innerText || "").replace(/\s+/g, " ").trim();
}

// “Fuzzy-ish” scorer: subsequence match + bonuses for contiguity and word boundaries.
// Returns -1 for no match, higher is better.
function fuzzyScore(query, text) {
  query = (query || "").toLowerCase();
  text = (text || "").toLowerCase();
  if (!query) return 0;

  let qi = 0;
  let score = 0;
  let lastMatch = -10;

  for (let ti = 0; ti < text.length && qi < query.length; ti++) {
    if (text[ti] === query[qi]) {
      score += 10;

      // contiguous bonus
      if (ti === lastMatch + 1) score += 8;

      // word boundary bonus
      const prev = ti > 0 ? text[ti - 1] : " ";
      if (prev === " " || prev === "_" || prev === "-" || prev === "." || prev === "/" ) score += 6;

      lastMatch = ti;
      qi++;
    }
  }

  if (qi !== query.length) return -1;

  // shorter texts get a small bonus
  score += Math.max(0, 30 - Math.min(text.length, 30));
  return score;
}

function topN(items, n) {
  items.sort((a, b) => b.score - a.score);
  return items.slice(0, n);
}

function escapeHashPart(s) {
  return encodeURIComponent(String(s || "")).replaceAll("%2F", "/");
}

function parseHashWithQuery() {
  const full = (location.hash || "#/").slice(1); // remove leading '#'
  const [pathPart, queryPart] = full.split("?", 2);
  const parts = pathPart.split("/").filter(Boolean);

  const query = new URLSearchParams(queryPart || "");
  return { parts, query };
}

// -------- Model facet filters (shareable via hash query params) --------
const FACET_Q = {
  kind: "mk",         // "sql" | "python" (omit => both)
  materialized: "mm", // normalized materialization, or "__unknown__"
  path: "mp",         // path prefix
  tags:"mt", 
  group:"mg"
};
const MAT_UNKNOWN = "__unknown__";

function normalizeModelKind(k) {
  return (k || "").toLowerCase() === "python" ? "python" : "sql";
}
function normalizeMaterialized(v) {
  return (v || "").toString().trim().toLowerCase();
}
function normalizePath(p) {
  return (p || "").toString().replace(/\\/g, "/").replace(/^\/+/, "");
}
function stripModelsPrefix(p) {
  const n = normalizePath(p);
  return n.toLowerCase().startsWith("models/") ? n.slice("models/".length) : n;
}
function modelMatchesPathPrefix(modelPath, prefix) {
  if (!prefix) return true;
  const p = normalizePath(modelPath).toLowerCase();
  const pref = normalizePath(prefix).toLowerCase();
  if (p.startsWith(pref)) return true;
  // also allow prefix relative to "models/"
  return stripModelsPrefix(p).startsWith(stripModelsPrefix(pref));
}

function readModelFacetsFromQuery(query) {
  const mk = (query.get(FACET_Q.kind) || "").trim().toLowerCase();
  const mt = (query.get(FACET_Q.tags) || "").trim();
  const tags = mt ? mt.split(",").map(s => s.trim()).filter(Boolean) : [];

  const mg = (query.get(FACET_Q.group) || "").trim().toLowerCase();
  const groupBy = (mg === "owner" || mg === "domain") ? mg : "";

  const kinds = mk
    ? mk.split(",").map(s => s.trim()).filter(Boolean)
    : ["sql", "python"];

  const validKinds = new Set(kinds.filter(k => k === "sql" || k === "python"));
  if (validKinds.size === 0) { validKinds.add("sql"); validKinds.add("python"); }

  return {
    kinds: Array.from(validKinds),
    materialized: normalizeMaterialized(query.get(FACET_Q.materialized) || ""),
    pathPrefix: query.get(FACET_Q.path) || "",
    tags,
    groupBy
  };
}

function writeModelFacetsToQuery(query, facets) {
  const kinds = Array.from(new Set(facets.kinds || []));
  const hasSql = kinds.includes("sql");
  const hasPy = kinds.includes("python");

  // Default => omit
  if (hasSql && hasPy) query.delete(FACET_Q.kind);
  else query.set(FACET_Q.kind, kinds.join(","));

  if (facets.materialized) query.set(FACET_Q.materialized, facets.materialized);
  else query.delete(FACET_Q.materialized);

  if (facets.pathPrefix) query.set(FACET_Q.path, facets.pathPrefix);
  else query.delete(FACET_Q.path);

  if (facets.tags && facets.tags.length) query.set(FACET_Q.tags, facets.tags.join(","));
  else query.delete(FACET_Q.tags);

  if (facets.groupBy) query.set(FACET_Q.group, facets.groupBy);
  else query.delete(FACET_Q.group);
}

function currentModelFacets() {
  return readModelFacetsFromQuery(parseHashWithQuery().query);
}

function facetsActiveCount(f) {
  const kinds = new Set(f.kinds || []);
  const kindActive = !(kinds.has("sql") && kinds.has("python"));
  const tagsN = (f.tags || []).length;
  const groupActive = f.groupBy ? 1 : 0;
  return (kindActive ? 1 : 0) + (f.materialized ? 1 : 0) + (f.pathPrefix ? 1 : 0) + tagsN + groupActive;
}

function filterModelsWithFacets(models, facets) {
  const kinds = new Set((facets.kinds || []).map(k => (k || "").toLowerCase()));
  const mat = normalizeMaterialized(facets.materialized || "");
  const pref = facets.pathPrefix || "";
  const tagSet = new Set((facets.tags || []).map(t => t.toLowerCase()));

  return (models || []).filter(m => {
    const kind = normalizeModelKind(m.kind);
    if (kinds.size && !kinds.has(kind)) return false;

    if (mat) {
      const mm = normalizeMaterialized(m.materialized || "");
      if (mat === MAT_UNKNOWN) {
        if (mm) return false;
      } else {
        if (mm !== mat) return false;
      }
    }

    if (pref && !modelMatchesPathPrefix(m.path || "", pref)) return false;
    
    if (tagSet.size) {
      const mtags = Array.isArray(m.tags) ? m.tags : [];
      const ok = mtags.some(t => tagSet.has(String(t).toLowerCase()));
      if (!ok) return false;
    }

    return true;
  });
}

// Merge current facet params into an arbitrary hash route (e.g. "#/model/x?tab=columns")
function routeWithFacets(route) {
  const facets = currentModelFacets();
  const r = (route || "#/").startsWith("#") ? (route || "#/").slice(1) : (route || "/");
  const [pathPart, queryPart] = r.split("?", 2);
  const q = new URLSearchParams(queryPart || "");

  if (String(pathPart || "").startsWith("/model/")) {
    const curQ = parseHashWithQuery().query;
    const curTab = curQ.get("tab") || "";
    const curCode = curQ.get("code") || "";

    if (!q.has("tab") && curTab) q.set("tab", curTab);

    const effectiveTab = q.get("tab") || curTab;
    if (effectiveTab === "code") {
      if (!q.has("code") && curCode) q.set("code", curCode);
    } else {
      // avoid leaking stale code=... into non-code tabs
      q.delete("code");
    }
  }
  
  writeModelFacetsToQuery(q, facets);

  const next = q.toString() ? `${pathPart}?${q.toString()}` : `${pathPart}`;
  return `#${next.startsWith("/") ? "" : "/"}${next}`;
}

// Replace just the hash query string without triggering hashchange (good UX for filters)
function replaceHashQuery(mutator) {
  const full = (location.hash || "#/").slice(1);
  const [pathPart, queryPart] = full.split("?", 2);
  const q = new URLSearchParams(queryPart || "");
  mutator(q, pathPart);

  const next = q.toString() ? `${pathPart}?${q.toString()}` : `${pathPart}`;
  const nextHash = `#${next.startsWith("/") ? "" : "/"}${next}`;

  history.replaceState(null, "", nextHash);
  if (window.__fftLastHashKey) safeSet(window.__fftLastHashKey, nextHash);
}

// Debounce helper (with cancel)
function debounce(fn, ms = 180) {
  let t = null;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => { clearTimeout(t); t = null; };
  return wrapped;
}


// -------- Source freshness helpers -----------------------------------------
function toNumOrNull(v) {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function formatMinutesCompact(mins) {
  const m = toNumOrNull(mins);
  if (m == null) return "—";
  const total = Math.max(0, Math.round(m));
  const d = Math.floor(total / 1440);
  const h = Math.floor((total % 1440) / 60);
  const mm = total % 60;
  const parts = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  if (mm || parts.length === 0) parts.push(`${mm}m`);
  return parts.join(" ");
}

function sourceFreshnessConfig(s) {
  const loadedAtField = String((s && s.loaded_at_field) || "").trim();
  const warnMinutes = toNumOrNull(s && s.warn_after_minutes);
  const errorMinutes = toNumOrNull(s && s.error_after_minutes);

  const hasLoaded = !!loadedAtField;
  const hasThresh = warnMinutes != null || errorMinutes != null;
  const configured = hasLoaded && hasThresh;

  let reason = "";
  if (configured) reason = "Freshness is configured (loaded_at field + thresholds).";
  else if (!hasLoaded && hasThresh) reason = "Warn/error thresholds are set but loaded_at field is missing.";
  else if (hasLoaded && !hasThresh) reason = "loaded_at field is set but warn/error thresholds are missing.";
  else reason = "No freshness configuration found for this source.";

  return { configured, reason, loadedAtField, warnMinutes, errorMinutes };
}


function setTabInHash(tab) {
  const full = (location.hash || "#/").slice(1);
  const [pathPart, queryPart] = full.split("?", 2);
  const q = new URLSearchParams(queryPart || "");
  if (tab) q.set("tab", tab);
  else q.delete("tab");
  const next = q.toString() ? `${pathPart}?${q.toString()}` : `${pathPart}`;
  location.hash = `#${next.startsWith("/") ? "" : "/"}${next}`;
}

function setModelQuery({ tab, col }) {
  const full = (location.hash || "#/").slice(1);
  const [pathPart, queryPart] = full.split("?", 2);
  const q = new URLSearchParams(queryPart || "");

  if (tab) q.set("tab", tab); else q.delete("tab");
  if (col) q.set("col", col); else q.delete("col");

  const next = q.toString() ? `${pathPart}?${q.toString()}` : `${pathPart}`;
  location.hash = `#${next.startsWith("/") ? "" : "/"}${next}`;
}

function setModelCodeQuery({ code }) {
  const full = (location.hash || "#/").slice(1);
  const [pathPart, queryPart] = full.split("?", 2);
  const q = new URLSearchParams(queryPart || "");

  q.set("tab", "code");
  if (code) q.set("code", code);
  else q.delete("code");

  const next = q.toString() ? `${pathPart}?${q.toString()}` : `${pathPart}`;
  location.hash = `#${next.startsWith("/") ? "" : "/"}${next}`;
}

function parseRoute() {
  const { parts, query } = parseHashWithQuery();
  if (parts.length === 0) {
    return { route: "home", focus: query.get("focus") || "" };
  }

  if (parts[0] === "model" && parts[1]) {
    return {
      route: "model",
      name: decodeURIComponent(parts.slice(1).join("/")),
      tab: query.get("tab") || "",
      col: query.get("col") || "",
      code: query.get("code") || "",  // "rendered" | "raw" | "refs"
    };
  }
  if (parts[0] === "source" && parts[1] && parts[2]) {
    return { route: "source", source: decodeURIComponent(parts[1]), table: decodeURIComponent(parts[2]) };
  }

  if (parts[0] === "macro" && parts[1]) {
    return { route: "macro", name: decodeURIComponent(parts.slice(1).join("/")) };
  }

  if (parts[0] === "macros") return { route: "macros", q: query.get("mq") || "" };

  return { route: "home" };
}

function byName(arr, keyFn) {
  const m = new Map();
  for (const x of arr) m.set(keyFn(x), x);
  return m;
}

function pillForKind(kind) {
  return el("span", { class: `pill ${kind}` }, kind);
}

function graphTransformDirection(graph, dir) {
  dir = (dir || "LR").toUpperCase();

  // manifest graph is already LR; just return it
  if (dir === "LR") return graph;

  // --- TB layout derived from LR graph (no rectangle rotation) ---
  const PAD = 24;
  const NODE_GAP_X = 32; // space between siblings in the same row

  // Copy nodes so we don't mutate manifest
  const nodes = (graph.nodes || []).map(n => ({ ...n }));
  const byId = new Map(nodes.map(n => [n.id, n]));

  // Group ORIGINAL nodes by rank (preserve ordering using original y)
  const byRank = new Map();
  for (const n of (graph.nodes || [])) {
    const r = Number.isFinite(n.rank) ? n.rank : 0;
    if (!byRank.has(r)) byRank.set(r, []);
    byRank.get(r).push(n);
  }

  const ranks = [...byRank.keys()].sort((a, b) => a - b);
  const minRank = ranks.length ? ranks[0] : 0;

  const maxH = nodes.reduce((m, n) => Math.max(m, Number(n.h || 0)), 0);
  const RANK_GAP_Y = Math.max(110, maxH + 70); // reduces the “too large” vertical spacing

  // First pass: compute each row width, so we can optionally center rows
  let maxRowW = 0;
  const rowInfo = new Map();

  for (const r of ranks) {
    const items = byRank.get(r).slice().sort((a, b) => (a.y || 0) - (b.y || 0));
    let rowW = 0;
    for (const orig of items) {
      const nn = byId.get(orig.id);
      rowW += Number(nn?.w || 0);
    }
    if (items.length > 1) rowW += NODE_GAP_X * (items.length - 1);
    maxRowW = Math.max(maxRowW, rowW);
    rowInfo.set(r, { items, rowW });
  }

  // Second pass: assign TB positions
  for (const r of ranks) {
    const { items, rowW } = rowInfo.get(r);

    // center each row inside the widest row (optional, but looks nicer)
    let x = PAD + Math.max(0, (maxRowW - rowW) / 2);
    const y = PAD + (r - minRank) * RANK_GAP_Y;

    for (const orig of items) {
      const n = byId.get(orig.id);
      n.x = x;
      n.y = y;
      x += Number(n.w || 0) + NODE_GAP_X;
    }
  }

  // Recompute bounds from new positions
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const n of nodes) {
    minx = Math.min(minx, n.x);
    miny = Math.min(miny, n.y);
    maxx = Math.max(maxx, n.x + n.w);
    maxy = Math.max(maxy, n.y + n.h);
  }
  const bounds = {
    minx, miny, maxx, maxy,
    width: (maxx - minx + PAD),
    height: (maxy - miny + PAD),
  };

  return { ...graph, direction: "TB", nodes, bounds };
}

function buildAdj(graph) {
  const out = new Map();
  const inn = new Map();
  for (const e of (graph.edges || [])) {
    if (!out.has(e.from)) out.set(e.from, []);
    if (!inn.has(e.to)) inn.set(e.to, []);
    out.get(e.from).push(e.to);
    inn.get(e.to).push(e.from);
  }
  return { out, inn };
}

function bfsCollect(startId, getNeighbors, depth) {
  const dist = new Map();
  const q = [startId];
  dist.set(startId, 0);

  while (q.length) {
    const id = q.shift();
    const d = dist.get(id);
    if (d >= depth) continue;

    const nbrs = getNeighbors(id) || [];
    for (const nb of nbrs) {
      if (!dist.has(nb)) {
        dist.set(nb, d + 1);
        q.push(nb);
      }
    }
  }
  return dist; // Map(id -> distance)
}

function buildNeighborhoodGraph(fullGraph, centerId, opts) {
  const depth = Math.max(1, Math.min(8, Number(opts?.depth || 2)));
  const mode = (opts?.mode || "both").toLowerCase(); // "up" | "down" | "both"

  const nodeById = new Map((fullGraph.nodes || []).map(n => [n.id, n]));
  if (!nodeById.has(centerId)) return { nodes: [], edges: [], bounds: { minx:0,miny:0,maxx:0,maxy:0,width:0,height:0 }, direction:"LR" };

  const { out, inn } = buildAdj(fullGraph);

  const upDist = (mode === "down") ? new Map([[centerId, 0]])
    : bfsCollect(centerId, (id) => inn.get(id), depth);

  const downDist = (mode === "up") ? new Map([[centerId, 0]])
    : bfsCollect(centerId, (id) => out.get(id), depth);

  // Collect included ids
  const ids = new Set([centerId]);
  for (const [id] of upDist) ids.add(id);
  for (const [id] of downDist) ids.add(id);

  // Build nodes (copy w/h from fullGraph)
  const nodes = [...ids].map(id => {
    const n = nodeById.get(id);
    return { ...n, x: 0, y: 0 }; // x/y will be recomputed
  });

  // Keep only edges fully inside
  const idSet = new Set(ids);
  const edges = (fullGraph.edges || []).filter(e => idSet.has(e.from) && idSet.has(e.to));

  // Assign layers: upstream negative, downstream positive
  const layer = new Map();
  layer.set(centerId, 0);
  for (const [id, d] of upDist) {
    if (id === centerId) continue;
    layer.set(id, -d);
  }
  for (const [id, d] of downDist) {
    if (id === centerId) continue;
    // if something is both up and down (cycle), keep the smaller magnitude, prefer downstream for ties
    if (!layer.has(id) || Math.abs(d) < Math.abs(layer.get(id))) layer.set(id, d);
    else if (Math.abs(d) === Math.abs(layer.get(id)) && layer.get(id) < 0) layer.set(id, d);
  }

  // Group by layer
  const byLayer = new Map();
  for (const n of nodes) {
    const L = layer.get(n.id) ?? 0;
    if (!byLayer.has(L)) byLayer.set(L, []);
    byLayer.get(L).push(n);
  }
  const layers = [...byLayer.keys()].sort((a,b)=>a-b);

  // Order within each layer: use original rank/x/y as a stable hint
  for (const L of layers) {
    byLayer.get(L).sort((a,b) => (a.rank ?? 0) - (b.rank ?? 0) || (a.y ?? 0) - (b.y ?? 0) || (a.x ?? 0) - (b.x ?? 0));
  }

  // Layout parameters
  const PAD = 20;
  const GAP_Y = 18;
  const GAP_X = 70;

  // Column widths per layer
  const colW = new Map();
  for (const L of layers) {
    let mw = 0;
    for (const n of byLayer.get(L)) mw = Math.max(mw, Number(n.w || 0));
    colW.set(L, mw);
  }

  // X positions by layer with variable column widths
  const xPos = new Map();
  let x = PAD;
  for (const L of layers) {
    xPos.set(L, x);
    x += colW.get(L) + GAP_X;
  }

  // Y packing per column; then vertically center columns to the tallest column
  const colH = new Map();
  for (const L of layers) {
    const col = byLayer.get(L);
    let h = 0;
    for (const n of col) h += Number(n.h || 0);
    if (col.length > 1) h += GAP_Y * (col.length - 1);
    colH.set(L, h);
  }
  const maxColH = Math.max(...layers.map(L => colH.get(L) || 0), 0);

  for (const L of layers) {
    const col = byLayer.get(L);
    const startY = PAD + Math.max(0, (maxColH - (colH.get(L) || 0)) / 2);
    let y = startY;
    for (const n of col) {
      n.x = xPos.get(L);
      n.y = y;
      y += Number(n.h || 0) + GAP_Y;
    }
  }

  // Bounds
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const n of nodes) {
    minx = Math.min(minx, n.x);
    miny = Math.min(miny, n.y);
    maxx = Math.max(maxx, n.x + n.w);
    maxy = Math.max(maxy, n.y + n.h);
  }
  const bounds = { minx, miny, maxx, maxy, width: (maxx - minx + PAD), height: (maxy - miny + PAD) };

  return { ...fullGraph, nodes, edges, bounds, direction: "LR" };
}


function hasDocs(obj) {
  const txt = (obj?.description_text || "").trim();
  const html = (obj?.description_html || "").trim();
  return !!(txt || html);
}

function isColumnDocumented(c) {
  const txt = (c?.description_text || "").trim();
  const html = (c?.description_html || "").trim();
  return !!(txt || html);
}

function modelDocsStatus(state, m) {
  const described = hasDocs(m);
  const withSchema = !!state.manifest.project?.with_schema;

  if (!withSchema) {
    return { described, withSchema: false, colDoc: 0, colTotal: 0, colMissing: 0 };
  }

  const cols = m.columns || [];
  const colTotal = cols.length;
  const colDoc = cols.filter(isColumnDocumented).length;
  const colMissing = colTotal - colDoc;

  return { described, withSchema: true, colDoc, colTotal, colMissing };
}

function computeDocsCoverage(state, modelsOverride) {
  const models = modelsOverride || state.manifest.models || [];
  const withSchema = !!state.manifest.project?.with_schema;

  let modelsDescribed = 0;
  let colsTotal = 0;
  let colsDoc = 0;

  const perModel = models.map(m => {
    const described = hasDocs(m);
    if (described) modelsDescribed += 1;

    let colTotal = 0, colDoc = 0, colMissing = 0;
    if (withSchema) {
      const cols = m.columns || [];
      colTotal = cols.length;
      colDoc = cols.filter(isColumnDocumented).length;
      colMissing = colTotal - colDoc;
      colsTotal += colTotal;
      colsDoc += colDoc;
    }

    return {
      name: m.name,
      kind: m.kind,
      path: m.path || "",
      described,
      colDoc, colTotal, colMissing,
    };
  });

  const undocumented = perModel.filter(p => !p.described || (withSchema && p.colMissing > 0));
  const missingModelDesc = perModel.filter(p => !p.described);
  const missingColDocs = perModel.filter(p => withSchema && p.colMissing > 0);
  const fullyDocumented = perModel.filter(p => p.described && (!withSchema || p.colMissing === 0));

  return {
    withSchema,
    modelsTotal: perModel.length,
    modelsDescribed,
    colsTotal,
    colsDoc,
    perModel,
    undocumented,
    missingModelDesc,
    missingColDocs,
    fullyDocumented,
  };
}

function renderDocsBadges(state, m, { compact = false } = {}) {
  const st = modelDocsStatus(state, m);

  const modelPill = st.described
    ? el("span", { class: "pillSmall pillGood" }, compact ? "Model docs" : "Model described")
    : el("span", { class: "pillSmall pillBad" }, compact ? "No model docs" : "No model docs");

  let colPill = null;
  if (!st.withSchema) {
    colPill = el("span", { class: "pillSmall" }, compact ? "Schema off" : "Schema disabled");
  } else if (!st.colTotal) {
    colPill = el("span", { class: "pillSmall" }, compact ? "No cols" : "No columns found");
  } else {
    const ok = st.colMissing === 0;
    colPill = el("span", { class: `pillSmall ${ok ? "pillGood" : "pillBad"}` },
      compact ? `${st.colDoc}/${st.colTotal} cols` : `${st.colDoc}/${st.colTotal} columns documented`
    );
  }

  const contracted = hasContract(m);
  const contractPill = contracted
    ? el("span", { class:"pillSmall pillGood", title:"Model contract defined" }, "Contracted")
    : null;

  const cls = compact ? "docPills docPillsCompact" : "docPills docPillsHeader";
  return el("div", { class: cls }, modelPill, colPill, contractPill);
}

function renderDocsCoverageCard(state, cov) {
  const jumpBtn = el("button", {
    class: "btn",
    onclick: () => document.getElementById("undocModels")?.scrollIntoView({ behavior: "smooth", block: "start" })
  }, "Undocumented models ↓");

  const colsNode = cov.withSchema
    ? el("span", {}, `${cov.colsDoc}/${cov.colsTotal}`)
    : el("span", { class: "empty" }, "Schema disabled");

  return el("div", { class: "card", id: "docsCoverage" },
    el("div", { class: "grid2" },
      el("div", {},
        el("h2", {}, "Docs coverage"),
        el("p", { class: "empty" }, "How complete your model + column descriptions are.")
      ),
      el("div", {}, jumpBtn)
    ),
    el("div", { class: "kv" },
      el("div", { class: "k" }, "Models described"),
      el("div", {}, `${cov.modelsDescribed}/${cov.modelsTotal}`),

      el("div", { class: "k" }, "Columns documented"),
      el("div", {}, colsNode),

      el("div", { class: "k" }, "Undocumented models"),
      el("div", {}, `${cov.undocumented.length}/${cov.modelsTotal}`)
    )
  );
}

function renderUndocumentedModelsCard(state, cov) {
  state.coverageUI ||= { tab: "undoc", q: "" };
  const uiState = state.coverageUI;

  const countNode = el("span", { class: "colCount" }, "");

  const qInput = el("input", {
    class: "search",
    type: "search",
    placeholder: "Filter models… (name or path)",
    value: uiState.q || "",
    oninput: (e) => {
      uiState.q = e.target.value || "";
      renderList();
    },
  });

  const tabs = [
    ["undoc", "Undocumented"],
    ["noDesc", "Missing model docs"],
    ["missingCols", "Undocumented columns"],
    ["fully", "Fully documented"],
    ["all", "All models"],
  ];

  const tabBtns = new Map();

  const tabRow = el("div", { class: "tabs" },
    ...tabs.map(([id, label]) => {
      const btn = el("button", {
        class: `tab ${uiState.tab === id ? "active" : ""}`,
        onclick: () => {
          uiState.tab = id;
          syncTabs();
          renderList();
          queueMicrotask(() => qInput.focus());
        },
      }, label);
      tabBtns.set(id, btn);
      return btn;
    })
  );

  function syncTabs() {
    for (const [id, btn] of tabBtns.entries()) {
      btn.classList.toggle("active", uiState.tab === id);
    }
  }

  const list = el("ul", { class: "docList" });

  function baseRows() {
    if (uiState.tab === "undoc") return cov.undocumented.slice();
    if (uiState.tab === "noDesc") return cov.missingModelDesc.slice();
    if (uiState.tab === "missingCols") return cov.missingColDocs.slice();
    if (uiState.tab === "fully") return cov.fullyDocumented.slice();
    return cov.perModel.slice();
  }

  function getRows() {
    let rows = baseRows();

    const q = (uiState.q || "").trim().toLowerCase();
    if (q) {
      rows = rows.filter(r =>
        (r.name || "").toLowerCase().includes(q) ||
        (r.path || "").toLowerCase().includes(q)
      );
    }

    // Most "work" first: missing model docs, then missing cols, then name
    rows.sort((a, b) => {
      const aw = (a.described ? 0 : 100000) + (a.colMissing || 0);
      const bw = (b.described ? 0 : 100000) + (b.colMissing || 0);
      if (bw !== aw) return bw - aw;
      return String(a.name).localeCompare(String(b.name));
    });

    return rows;
  }

  function rowNode(r) {
    const pills = el("div", { class: "docRowPills" },
      r.described
        ? el("span", { class: "pillSmall pillGood" }, "Model described")
        : el("span", { class: "pillSmall pillBad" }, "No model docs"),
      cov.withSchema
        ? (r.colTotal
            ? el("span", { class: `pillSmall ${r.colMissing ? "pillBad" : "pillGood"}` }, `${r.colDoc}/${r.colTotal} cols`)
            : el("span", { class: "pillSmall" }, "No cols")
          )
        : el("span", { class: "pillSmall" }, "Schema off"),
    );

    const main = el("div", { class: "docRowMain" },
      el("div", { class: "docRowTitle" }, el("code", {}, r.name)),
      r.path ? el("div", { class: "docRowSub" }, r.path) : null
    );

    return el("li", { class: "docRow" },
      el("a", {
        href: routeWithFacets(`#/model/${escapeHashPart(r.name)}`),
        onclick: (e) => { e.preventDefault(); location.hash = routeWithFacets(`#/model/${escapeHashPart(r.name)}`); },
        title: r.path || r.name,
      }, main, pills)
    );
  }

  function renderList() {
    const rows = getRows();
    countNode.textContent = `${rows.length} model${rows.length === 1 ? "" : "s"}`;

    if (!rows.length) {
      list.replaceChildren(el("li", { class: "empty" }, "No matches."));
      return;
    }
    list.replaceChildren(...rows.map(rowNode));
  }

  renderList();

  return el("div", { class: "card", id: "undocModels" },
    el("h2", {}, "Undocumented models"),
    el("p", { class: "empty" }, "Jump straight to models that need documentation."),
    el("div", { class: "colTools" }, qInput, countNode),
    tabRow,
    list
  );
}

// ----------- Contracts --------------------
function hasContract(m) {
  const c = m && m.contract;
  if (!c) return false;
  if (c === true) return true;
  const cols = contractColumnsFrom(m);
  const tbl = contractTableConstraintsFrom(m);
  return (cols && cols.length) || (tbl && tbl.length) || (c.enforced != null);
}

function contractColumnsFrom(m) {
  const c = m && m.contract;
  if (!c) return [];
  if (Array.isArray(c)) return c;

  const colsSpec = c.columns ?? c.schema ?? c.fields;
  if (!colsSpec) return [];

  if (Array.isArray(colsSpec)) return colsSpec.filter(x => x && typeof x === "object");

  if (colsSpec && typeof colsSpec === "object") {
    return Object.entries(colsSpec).map(([name, spec]) => {
      if (spec && typeof spec === "object" && !Array.isArray(spec)) return { name, ...spec };
      if (typeof spec === "string") return { name, dtype: spec };
      return { name };
    });
  }
  return [];
}

function contractTableConstraintsFrom(m) {
  const c = m && m.contract;
  if (!c || typeof c !== "object") return [];
  const v = c.constraints ?? c.table_constraints;
  if (!v) return [];
  return Array.isArray(v) ? v : [v];
}

function normalizeContractCol(col) {
  const name = String(col?.name || "").trim();
  if (!name) return null;

  const dtype = col?.dtype ?? col?.type ?? col?.data_type;
  let nullable = col?.nullable;
  if (nullable == null && col?.not_null != null) nullable = !col.not_null;

  let constraints = col?.constraints ?? col?.tests ?? [];
  if (constraints && !Array.isArray(constraints)) constraints = [constraints];

  return {
    name,
    dtype: dtype != null ? String(dtype) : "",
    nullable: (nullable === true || nullable === false) ? nullable : null,
    constraints: constraints || [],
  };
}

function renderConstraintsList(items) {
  const arr = Array.isArray(items) ? items : (items ? [items] : []);
  if (!arr.length) return el("span", { class:"empty" }, "—");

  const wrap = el("span", { class:"pillRow" });
  for (const it of arr) {
    if (typeof it === "string") {
      wrap.appendChild(el("span", { class:"pillSmall" }, it));
    } else if (it && typeof it === "object") {
      wrap.appendChild(
        el("details", { style:"display:inline-block;" },
          el("summary", { class:"codeSummary" }, "constraint"),
          el("pre", { class:"mono", style:"white-space:pre-wrap; margin:8px 0 0 0;" }, jsonPreview(it))
        )
      );
    }
  }
  return wrap;
}

function canonicalType(t) {
  const s = String(t || "").trim().toLowerCase();
  if (!s) return "";
  // normalize whitespace + strip trailing params like varchar(255), numeric(10,2)
  return s.replace(/\s+/g, " ").replace(/\(.*\)\s*$/, "").trim();
}

function computeContractDrift(m, withSchema) {
  const contracted = hasContract(m);
  if (!contracted) {
    return { status: "none", missing: [], extra: [], mismatches: [], byName: new Map(), schemaAvailable: false, constraintsComparable: false };
  }

  const schemaAvailable = !!withSchema && Array.isArray(m.columns) && m.columns.length > 0;
  if (!schemaAvailable) {
    return { status: "unavailable", missing: [], extra: [], mismatches: [], byName: new Map(), schemaAvailable: false, constraintsComparable: false };
  }

  const contractCols = contractColumnsFrom(m).map(normalizeContractCol).filter(Boolean);
  const actualCols = (m.columns || []).map(c => ({
    name: String(c.name || ""),
    dtype: c.dtype != null ? String(c.dtype) : "",
    nullable: !!c.nullable,
    constraints: c.constraints ?? null, // only comparable if your schema collector starts emitting it
  }));

  const cMap = new Map(); // lowerName -> contract col
  for (const c of contractCols) cMap.set(c.name.toLowerCase(), c);

  const aMap = new Map(); // lowerName -> actual col
  for (const a of actualCols) aMap.set(a.name.toLowerCase(), a);

  const constraintsComparable = actualCols.some(a => Array.isArray(a.constraints));

  const missing = [];
  const mismatches = [];
  const byName = new Map(); // lowerName -> { missing?:true, extra?:true, issues:[], expected, actual }

  for (const c of contractCols) {
    const key = c.name.toLowerCase();
    const a = aMap.get(key);

    if (!a) {
      missing.push(c.name);
      byName.set(key, { missing: true, issues: ["missing"], expected: c, actual: null });
      continue;
    }

    const issues = [];

    // type mismatch (if contract specifies dtype)
    if (c.dtype) {
      const expT = canonicalType(c.dtype);
      const actT = canonicalType(a.dtype);
      if (expT && actT && expT !== actT) issues.push("type");
    }

    // nullability mismatch (if contract specifies nullable)
    if (c.nullable != null) {
      if (!!c.nullable !== !!a.nullable) issues.push("nullability");
    }

    // constraints mismatch (only if warehouse schema exposes constraints)
    if (constraintsComparable && (c.constraints || []).length) {
      const exp = JSON.stringify(c.constraints || []);
      const act = JSON.stringify(a.constraints || []);
      if (exp !== act) issues.push("constraints");
    }

    if (issues.length) {
      mismatches.push({ name: c.name, issues });
      byName.set(key, { issues, expected: c, actual: a });
    }
  }

  const extra = [];
  for (const a of actualCols) {
    const key = a.name.toLowerCase();
    if (!cMap.has(key)) {
      extra.push(a.name);
      byName.set(key, { extra: true, issues: ["extra"], expected: null, actual: a });
    }
  }

  const status = (missing.length || extra.length || mismatches.length) ? "drift" : "verified";
  return { status, missing, extra, mismatches, byName, schemaAvailable: true, constraintsComparable };
}


// -------- Code Viewer helpers ---------------------

function copyToClipboard(text) {
  const s = String(text || "");
  if (!s) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(s).catch(() => {});
  } else {
    // fallback
    const ta = document.createElement("textarea");
    ta.value = s;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch {}
    ta.remove();
  }
}

function renderCodeBlock(text, { wrap = false } = {}) {
  return el("pre", { class: `codeBlock ${wrap ? "codeWrap" : ""}` }, String(text || ""));
}

function renderModelCodeTab(state, m, codeView) {
  const isSql = (m.kind || "sql") !== "python";

  const raw = m.raw_sql || m.sql || m.code || "";
  const rendered = m.rendered_sql || m.compiled_sql || "";

  // Default view: rendered if available, else raw
  let view = (codeView || "").toLowerCase();
  if (!["rendered", "raw", "refs"].includes(view)) {
    view = rendered ? "rendered" : "raw";
  }

  state.codeUI ||= {};
  const ui = (state.codeUI[m.name] ||= { wrap: false });

  const setView = (v) => {
    setModelCodeQuery({ code: v });
  };

  const headRight = el("div", { class: "row", style: "gap:8px; justify-content:flex-end;" },
    el("button", {
      class: "btnTiny",
      type: "button",
      onclick: () => { ui.wrap = !ui.wrap; updateMain(); }
    }, ui.wrap ? "No wrap" : "Wrap"),
    el("button", {
      class: "btnTiny",
      type: "button",
      onclick: () => {
        const txt =
          view === "rendered" ? (rendered || "Rendered SQL not available. Enable docs.include_rendered_sql in the generator.") :
          view === "raw" ? raw :
          ""; // refs: nothing to copy
        copyToClipboard(txt);
      }
    }, "Copy")
  );

  const tabs = el("div", { class: "pillRow" },
    el("button", { class: `tab ${view === "rendered" ? "active" : ""}`, type:"button", onclick: () => setView("rendered") }, "Rendered"),
    el("button", { class: `tab ${view === "raw" ? "active" : ""}`, type:"button", onclick: () => setView("raw") }, "Raw"),
    el("button", { class: `tab ${view === "refs" ? "active" : ""}`, type:"button", onclick: () => setView("refs") }, "Refs resolved"),
  );

  const body = (() => {
    if (!isSql) {
      // Optional: show python source if you later add it to manifest
      const py = m.python_source || m.source || "";
      return py
        ? renderCodeBlock(py, { wrap: ui.wrap })
        : el("p", { class:"empty" }, "No source available for this model.");
    }

    if (view === "rendered") {
      return rendered
        ? renderCodeBlock(rendered, { wrap: ui.wrap })
        : el("p", { class:"empty" }, "Rendered SQL not available. Enable docs.include_rendered_sql in the generator.");
    }

    if (view === "raw") {
      return raw
        ? renderCodeBlock(raw, { wrap: ui.wrap })
        : el("p", { class:"empty" }, "Raw SQL not available in the manifest.");
    }

    // refs resolved
    // Prefer explicit rendered_refs mapping if provided, else derive from deps/sources_used.
    const rows = [];

    if (m.rendered_refs) {
      if (Array.isArray(m.rendered_refs)) {
        for (const r of m.rendered_refs) rows.push({ kind:"model", name:r.name || "", relation:r.relation || "" });
      } else if (typeof m.rendered_refs === "object") {
        for (const [k, v] of Object.entries(m.rendered_refs)) rows.push({ kind:"model", name:k, relation:String(v || "") });
      }
    } else {
      // derive from deps (models) + sources_used (sources)
      const byName = new Map((state.manifest.models || []).map(x => [x.name, x]));
      for (const d of (m.deps || [])) {
        const md = byName.get(d);
        rows.push({ kind:"model", name:d, relation: md?.relation || "" });
      }
      for (const s of (m.sources_used || [])) {
        // sources_used entries are {source_name, table_name, relation}
        const nm = `${s.source_name}.${s.table_name}`;
        rows.push({ kind:"source", name:nm, relation: s.relation || "" });
      }
    }

    if (!rows.length) return el("p", { class:"empty" }, "No references detected for this model.");

    return el("table", { class:"table" },
      el("thead", {}, el("tr", {},
        el("th", {}, "Kind"),
        el("th", {}, "Reference"),
        el("th", {}, "Resolved relation"),
      )),
      el("tbody", {},
        ...rows.map(r => el("tr", {},
          el("td", {}, el("span", { class:"pillSmall" }, r.kind)),
          el("td", {}, r.kind === "model"
            ? el("a", { href: routeWithFacets(`#/model/${escapeHashPart(r.name)}`),
                        onclick:(e)=>{ e.preventDefault(); location.hash = routeWithFacets(`#/model/${escapeHashPart(r.name)}`); } }, r.name)
            : el("span", {}, r.name)
          ),
          el("td", {}, r.relation ? el("code", {}, r.relation) : el("span", { class:"empty" }, "—"))
        ))
      )
    );
  })();

  return el("div", { class:"card" },
    el("div", { class:"row", style:"align-items:center; justify-content:space-between;" },
      el("h3", { style:"margin:0;" }, isSql ? "SQL" : "Code"),
      headRight
    ),
    tabs,
    body
  );
}

// -------- Landing page (overview dashboard) helpers ---------------------

function setModelFacetsAndGoHome(nextFacets, extra = {}) {
  const { query } = parseHashWithQuery();
  const q = new URLSearchParams(query.toString());
  writeModelFacetsToQuery(q, nextFacets);

  // Optional extra params (e.g. { home: "undoc" })
  for (const [k, v] of Object.entries(extra || {})) {
    if (v == null || String(v).trim() === "") q.delete(k);
    else q.set(k, String(v));
  }

  const qs = q.toString();
  location.hash = `#/${qs ? "?" + qs : ""}`;
}

function getModelChangeTs(m) {
  const meta = (m && typeof m === "object") ? (m.meta || {}) : {};
  const candidates = [
    m.updated_at, m.modified_at, m.last_modified, m.changed_at, m.created_at,
    meta.updated_at, meta.modified_at, meta.last_modified, meta.changed_at, meta.created_at,
  ];

  for (const v of candidates) {
    if (v == null || v === "") continue;
    if (typeof v === "number" && Number.isFinite(v)) {
      // seconds vs ms
      return v < 1e12 ? Math.floor(v * 1000) : Math.floor(v);
    }
    const d = new Date(v);
    if (!Number.isNaN(d.getTime())) return d.getTime();
  }
  return null;
}

function fmtDateShort(ms) {
  try {
    return new Date(ms).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "2-digit" });
  } catch {
    return "";
  }
}

function computeImpactOverview(state, models) {
  const rows = (models || []).map(m => {
    const deps = (m.deps || []).length;
    const usedBy = (m.used_by || []).length;
    const score = (usedBy + 1) * (deps + 1);
    return {
      name: m.name,
      kind: m.kind,
      materialized: m.materialized || "",
      path: m.path || "",
      deps,
      usedBy,
      score,
    };
  });

  const topFanOut = rows
    .slice()
    .sort((a, b) => (b.usedBy - a.usedBy) || a.name.localeCompare(b.name))
    .slice(0, 10);

  const topCritical = rows
    .slice()
    .sort((a, b) => (b.score - a.score) || (b.usedBy - a.usedBy) || (b.deps - a.deps) || a.name.localeCompare(b.name))
    .slice(0, 10);

  const edges = rows.reduce((acc, r) => acc + (r.deps || 0), 0);

  return { rows, topFanOut, topCritical, edges };
}

function computeRecentChangedOverview(models) {
  const rows = (models || [])
    .map(m => {
      const ts = getModelChangeTs(m);
      return ts ? { name: m.name, kind: m.kind, materialized: m.materialized || "", path: m.path || "", ts } : null;
    })
    .filter(Boolean)
    .sort((a, b) => b.ts - a.ts)
    .slice(0, 10);

  return { available: rows.length > 0, rows };
}

function renderRankList(state, rows, metricNode) {
  const list = el("ul", { class: "docList" });

  if (!rows.length) {
    list.replaceChildren(el("li", { class: "empty" }, "—"));
    return list;
  }

  list.replaceChildren(
    ...rows.map(r =>
      el("li", { class: "docRow" },
        el("a", {
          href: routeWithFacets(`#/model/${escapeHashPart(r.name)}`),
          onclick: (e) => { e.preventDefault(); location.hash = routeWithFacets(`#/model/${escapeHashPart(r.name)}`); },
          title: r.path || r.name,
        },
          el("div", { class: "docRowMain" },
            el("div", { class: "docRowTitle" }, r.name),
            r.path ? el("div", { class: "docRowSub" }, r.path) : null
          ),
          el("div", { class: "docRowPills" },
            pillForKind(normalizeModelKind(r.kind)),
            r.materialized ? el("span", { class: "pillSmall" }, r.materialized) : null,
            metricNode(r)
          )
        )
      )
    )
  );

  return list;
}

function renderOverviewDashboardCard(state, facets, modelsSubset, cov, impact, changed) {
  const allModels = state.manifest.models || [];
  const totalModels = allModels.length;
  const subsetN = (modelsSubset || []).length;

  const pythonN = (modelsSubset || []).filter(m => normalizeModelKind(m.kind) === "python").length;

  const filtN = facetsActiveCount(facets);
  const filterPills = [];
  if (filtN) {
    const kinds = new Set((facets.kinds || []).map(k => (k || "").toLowerCase()));
    if (!(kinds.has("sql") && kinds.has("python"))) {
      filterPills.push(el("span", { class: "pillSmall" }, `kind:${(facets.kinds || []).join(",")}`));
    }
    if (facets.materialized) filterPills.push(el("span", { class: "pillSmall" }, `mat:${facets.materialized}`));
    if (facets.pathPrefix) filterPills.push(el("span", { class: "pillSmall" }, `path:${facets.pathPrefix}`));
  }

  const links = el("div", { style: "display:flex; gap:10px; flex-wrap:wrap; margin-top:10px;" },
    el("a", {
      class: "btnTiny",
      href: routeWithFacets("#/?home=undoc"),
      onclick: (e) => { e.preventDefault(); location.hash = routeWithFacets("#/?home=undoc"); },
    }, `Undocumented (${cov.undocumented.length})`),

    el("a", {
      class: "btnTiny",
      href: routeWithFacets("#/?home=impact"),
      onclick: (e) => { e.preventDefault(); location.hash = routeWithFacets("#/?home=impact"); },
    }, "High impact"),

    el("button", {
      class: "btnTiny",
      type: "button",
      onclick: () => {
        // Set facets to python-only, clearing other facet constraints.
        setModelFacetsAndGoHome({ kinds: ["python"], materialized: "", pathPrefix: "" }, { home: "" });
      },
    }, `Python models (${(state.manifest.models || []).filter(m => normalizeModelKind(m.kind) === "python").length})`),

    filtN
      ? el("button", {
          class: "btnTiny",
          type: "button",
          onclick: () => setModelFacetsAndGoHome({ kinds: ["sql", "python"], materialized: "", pathPrefix: "" }, { home: "" }),
        }, "Clear filters")
      : null
  );

  const newestLine = changed.available
    ? (() => {
        const r = changed.rows[0];
        return el("div", {},
          el("span", { class: "pillSmall" }, "Newest"),
          " ",
          el("span", {}, `${r.name} • ${fmtDateShort(r.ts)}`)
        );
      })()
    : el("span", { class: "empty" }, "No change timestamps available in manifest.");

  return el("div", { class: "card" },
    el("div", { class: "grid2" },
      el("div", {},
        el("h2", {}, "Overview dashboard"),
        el("p", { class: "empty" }, "Stats, impact hotspots, docs coverage, and quick links."),
        filterPills.length
          ? el("div", { class: "docPills docPillsCompact", style: "margin-top:8px;" }, ...filterPills)
          : null,
        links
      ),
      el("div", {},
        el("h3", {}, "Stats"),
        el("div", { class: "kv" },
          el("div", { class: "k" }, "Models"),
          el("div", {}, filtN ? `${subsetN}/${totalModels}` : `${subsetN}`),

          el("div", { class: "k" }, "Python models"),
          el("div", {}, String(pythonN)),

          el("div", { class: "k" }, "Edges"),
          el("div", {}, String(impact.edges)),

          el("div", { class: "k" }, "Model described"),
          el("div", {}, `${cov.modelsDescribed}/${cov.modelsTotal}`),

          el("div", { class: "k" }, "Columns documented"),
          el("div", {}, cov.withSchema ? `${cov.colsDoc}/${cov.colsTotal}` : "Schema off"),

          el("div", { class: "k" }, "Undocumented"),
          el("div", {}, `${cov.undocumented.length}/${cov.modelsTotal}`),

          el("div", { class: "k" }, "Newest/changed"),
          el("div", {}, newestLine),
        )
      )
    )
  );
}

function renderTopFanOutCard(state, modelsSubset, impact) {
  const rows = (impact.topFanOut || []).filter(r => r.usedBy > 0);

  return el("div", { class: "card", id: "fanoutModels" },
    el("h2", {}, "Top fan-out nodes"),
    el("p", { class: "empty" }, "Models with the most downstream consumers (direct)."),
    renderRankList(state, rows, (r) => el("span", { class: "pillSmall" }, `used by ${r.usedBy}`))
  );
}

function renderCriticalModelsCard(state, impact) {
  const rows = (impact.topCritical || []).slice();

  return el("div", { class: "card", id: "impactModels" },
    el("h2", {}, "Most critical models"),
    el("p", { class: "empty" }, "Heuristic score combining upstream deps and downstream usage."),
    renderRankList(state, rows, (r) => el("span", { class: "pillSmall" }, `score ${r.score}`))
  );
}

function renderRecentChangedCard(state, changed) {
  if (!changed.available) {
    return el("div", { class: "card", id: "changedModels" },
      el("h2", {}, "Newest / changed"),
      el("p", { class: "empty" }, "No per-model change timestamps were found in the manifest."),
      el("p", { class: "empty" }, "If you add fields like updated_at / modified_at to model entries, this list will populate.")
    );
  }

  const rows = changed.rows || [];
  return el("div", { class: "card", id: "changedModels" },
    el("h2", {}, "Newest / changed"),
    el("p", { class: "empty" }, "Models sorted by last change timestamp (if provided)."),
    renderRankList(state, rows, (r) => el("span", { class: "pillSmall" }, fmtDateShort(r.ts)))
  );
}



function renderHome(state) {
  const { manifest } = state;
  const graph = manifest.dag?.graph;

  const graphHost = el("div", { class: "graphHost" });
  const miniHost  = el("div", { class: "minimapHost" });

  const modeBtn = (id, label) =>
    el("button", {
      class: `btn ${state.graphUI.mode === id ? "active" : ""}`,
      onclick: () => { state.graphUI.mode = id; state._graphCtl?.refresh?.(); }
    }, label);

  const depthPill = el("span", { class: "pill" }, `Depth ${state.graphUI.depth}`);
  const depthSlider = el("input", {
    type: "range", min: "1", max: "8",
    value: String(state.graphUI.depth),
    oninput: (e) => {
      state.graphUI.depth = Number(e.target.value || 2);
      depthPill.textContent = `Depth ${state.graphUI.depth}`;
      rerenderMini();
    }
  });

  const fitBtn = el("button", { class: "btnTiny", title: "Fit to screen", onclick: () => state._graphCtl?.fit?.() }, "Fit");
  const resetBtn = el("button", { class: "btnTiny", title: "Reset pan/zoom", onclick: () => state._graphCtl?.reset?.() }, "Reset");
  const zoomOutBtn = el("button", { class: "btnTiny", title: "Zoom out", onclick: () => state._graphCtl?.zoomOut?.() }, "–");
  const zoomInBtn  = el("button", { class: "btnTiny", title: "Zoom in",  onclick: () => state._graphCtl?.zoomIn?.() }, "+");

  const dirPill = el("span", { class: "pillSmall" },
    state.graphUI.dir === "TB" ? "Top → Bottom" : "Left → Right"
  );

  const lrBtn = el("button", { class: `tab ${state.graphUI.dir === "LR" ? "active" : ""}` }, "LR");
  const tbBtn = el("button", { class: `tab ${state.graphUI.dir === "TB" ? "active" : ""}` }, "TB");

  function setDir(dir) {
    dir = (dir || "LR").toUpperCase();
    if (state.graphUI.dir === dir) return;

    state.graphUI.dir = dir;

    // update segmented control UI
    lrBtn.classList.toggle("active", dir === "LR");
    tbBtn.classList.toggle("active", dir === "TB");
    dirPill.textContent = dir === "TB" ? "Top → Bottom" : "Left → Right";

    // remount graph
    const g = graphTransformDirection(state.manifest.dag.graph, dir);
    state._graphCtl = mountGraph(state, graphHost, g, { miniHost, showMini: true });
  }

  lrBtn.onclick = () => setDir("LR");
  tbBtn.onclick = () => setDir("TB");

  // use this in your toolbar row:
  const layoutTabs = el("div", { class: "tabs" }, lrBtn, tbBtn);

  const graphCard = el("div", { class: "card" },
    el("div", { class: "grid" },
      el("div", { class: "dagHeader" },
        el("div", { class: "dagHeaderLeft" },
          el("div", { class: "dagTitleRow" },
            el("h2", {}, "DAG"),
            dirPill
          ),
          el("p", { class: "dagSubtle" },
            "Pan/zoom • click a node to pin • click again to unpin • Ctrl/Cmd-click opens."
          )
        ),

        el("div", { class: "dagHeaderRight" },
          el("div", { class: "dagToolsRow" },
            fitBtn, resetBtn, zoomOutBtn, zoomInBtn,
            layoutTabs
          ),
          el("div", { class: "dagToolsRow" },
            el("div", { class: "tabs dagModeTabs" },
              modeBtn("up", "Up"),
              modeBtn("down", "Down"),
              modeBtn("both", "Both"),
              modeBtn("off", "Off"),
            ),
            el("div", { class: "dagDepth" }, depthPill, depthSlider),
          )
        )
      ),

      el("div", { class: "graphWrap" }, graphHost, miniHost)
    )
  );

  queueMicrotask(() => {
    const g0 = graphTransformDirection(graph, state.graphUI.dir);
    state._graphCtl = mountGraph(state, graphHost, g0, { miniHost, showMini: true });

    const r = parseRoute();
    if (r.route === "home" && r.focus) {
      state._graphCtl?.focus?.(r.focus, { zoom: 1.25, pin: true });
    }
  });

  // Dashboard data respects the current model facets (kind/materialized/path prefix).
  const facets = currentModelFacets();
  const allModels = manifest.models || [];
  const modelsSubset = filterModelsWithFacets(allModels, facets);

  // If the URL requests a section, set the default undoc tab.
  const homeMode = parseHashWithQuery().query.get("home") || "";
  if (homeMode === "undoc") {
    state.coverageUI ||= { tab: "undoc", q: "" };
    state.coverageUI.tab = "undoc";
  }

  const cov = computeDocsCoverage(state, modelsSubset);
  const impact = computeImpactOverview(state, modelsSubset);
  const changed = computeRecentChangedOverview(modelsSubset);

  const dashCard = renderOverviewDashboardCard(state, facets, modelsSubset, cov, impact, changed);
  const coverageCard = renderDocsCoverageCard(state, cov);
  const fanOutCard = renderTopFanOutCard(state, modelsSubset, impact);
  const criticalCard = renderCriticalModelsCard(state, impact);
  const changedCard = renderRecentChangedCard(state, changed);
  const undocCard = renderUndocumentedModelsCard(state, cov);

  const root = el("div", { class: "grid" },
    dashCard,
    graphCard,
    coverageCard,
    fanOutCard,
    criticalCard,
    changedCard,
    undocCard
  );

  queueMicrotask(() => {
    if (homeMode === "undoc") document.getElementById("undocModels")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    if (homeMode === "impact") document.getElementById("impactModels")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    if (homeMode === "changed") document.getElementById("changedModels")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    if (homeMode === "fanout") document.getElementById("fanoutModels")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  });

  return root;

}


function renderModel(state, name, tabFromRoute, colFromRoute) {
  const m = state.byModel.get(name);
  if (!m) {
    return el("div", { class: "card" }, el("h2", {}, "Model not found"), el("p", { class: "empty" }, name));
  }

  const active = (tabFromRoute || state.modelTabDefault || "overview").toLowerCase();
  const hasCol = !!(colFromRoute && String(colFromRoute).trim());

  let tab = ["overview","columns","contract","lineage","code","meta"].includes(active) ? active : "overview";
  // Only force columns if col is present AND the URL didn't explicitly set a tab
  if (hasCol && !tabFromRoute) tab = "columns";

  const header = el("div", { class: "card" },
    el("div", { class: "grid2" },
      el("div", {},
        el("h2", {}, m.name),
        el("p", { class: "empty" }, m.relation ? `Relation: ${m.relation}` : ""),
        renderDocsBadges(state, m)
      ),
      el("div", {},
        el("button", {
          class: "btn",
          onclick: () => { location.hash = routeWithFacets("#/"); }
        }, "← Overview"),
        el("button", {
          class: "btn",
          onclick: async () => { try { await navigator.clipboard.writeText(m.path || ""); } catch {} }
        }, "Copy path")
      )
    ),
    renderTabs(tab, (next) => {
      // Persist default for convenience
      state.modelTabDefault = next;
      safeSet(state.STORE.modelTab, next);

      setModelQuery({
        tab: next,
        col: (next === "columns") ? (colFromRoute || "") : ""  // clear col when leaving Columns
      });

    })
  );

  const panel = el("div", { class: "tabPanel" }, renderModelPanel(state, m, tab, colFromRoute));

  return el("div", { class: "grid" }, header, panel);
}

function renderModelPanel(state, m, tab, colFromRoute) {
  if (tab === "overview") {
    const deps = (m.deps || []).map(d => el("a", { href: routeWithFacets(`#/model/${escapeHashPart(d)}`) }, d));
    const usedBy = (m.used_by || []).map(u => el("a", { href: routeWithFacets(`#/model/${escapeHashPart(u)}`) }, u));
    const sourcesUsed = (m.sources_used || []).map(s =>
      el("a", { href: routeWithFacets(`#/source/${escapeHashPart(s.source_name)}/${escapeHashPart(s.table_name)}`) }, `${s.source_name}.${s.table_name}`)
    );
    const modelId = m.name;

    // --- Neighborhood mini-graph (MODEL PAGE) ---
    state.modelMini = state.modelMini || { mode: "both", depth: 2 };

    const miniGraphHost = el("div", { class: "miniGraphHost" });
    let miniCtl = null;

    const depthPill = el("span", { class: "pill" }, `Depth ${state.modelMini.depth}`);
    const depthSlider = el("input", {
      type: "range", min: "1", max: "6",
      value: String(state.modelMini.depth),
      oninput: (e) => {
        state.modelMini.depth = Number(e.target.value || 2);
        depthPill.textContent = `Depth ${state.modelMini.depth}`;
        rerenderMini();
      }
    });

    const miniModeTabs = el("div", { class: "tabs" },
      el("button", { class: `tab ${state.modelMini.mode==="up"?"active":""}`,   onclick:()=>{ state.modelMini.mode="up";   syncMiniTabs(); rerenderMini(); } }, "Upstream"),
      el("button", { class: `tab ${state.modelMini.mode==="down"?"active":""}`, onclick:()=>{ state.modelMini.mode="down"; syncMiniTabs(); rerenderMini(); } }, "Downstream"),
      el("button", { class: `tab ${state.modelMini.mode==="both"?"active":""}`, onclick:()=>{ state.modelMini.mode="both"; syncMiniTabs(); rerenderMini(); } }, "Both"),
    );

    function syncMiniTabs() {
      const btns = miniModeTabs.querySelectorAll(".tab");
      btns.forEach(b => b.classList.remove("active"));
      const idx = state.modelMini.mode === "up" ? 0 : state.modelMini.mode === "down" ? 1 : 2;
      btns[idx]?.classList.add("active");
    }

    function rerenderMini() {
      miniGraphHost.textContent = "";
      const centerNode = (state.manifest.dag?.graph?.nodes || [])
        .find(n => n.kind === "model" && n.name === m.name);

      const centerId = centerNode?.id || `m:${m.name}`;

      const g = buildNeighborhoodGraph(state.manifest.dag.graph, centerId, state.modelMini);
      miniCtl = mountGraph(state, miniGraphHost, g, { showMini: false, nodeClick: "navigate" });
      miniCtl?.fit?.();
    }

    const miniPanel = el("div", { class: "card" },
      el("div", { class: "dagHeader" },
        el("div", { class: "dagHeaderLeft" },
          el("div", { class: "dagTitleRow" }, el("h3", {}, "Neighborhood")),
          el("p", { class: "dagSubtle" }, "Drag to pan • wheel to zoom • click nodes to open")
        ),
        el("div", { class: "dagHeaderRight" },
          el("div", { class: "dagToolsRow" },
            miniModeTabs,
            el("div", { class: "dagDepth" }, depthPill, depthSlider),
            el("button", { class: "btnTiny", onclick: () => miniCtl?.fit?.() }, "Fit"),
          )
        )
      ),
      miniGraphHost
    );

    queueMicrotask(rerenderMini);

    return el("div", { class: "grid" },
      el("div", { class: "card" },
        el("h3", {}, "Summary"),
        el("div", { class: "kv" },
          el("div", { class: "k" }, "Kind"), el("div", {}, m.kind),
          el("div", { class: "k" }, "Materialized"), el("div", {}, m.materialized || "—"),
          el("div", { class: "k" }, "Path"), el("div", {}, el("code", {}, m.path || "—")),
          el("div", { class: "k" }, "Model docs"), el("div", {}, modelDocsStatus(state, m).described ? el("span", { class: "pillSmall pillGood" }, "Model described") : el("span", { class: "pillSmall pillBad" }, "No model docs")),
          el("div", { class: "k" }, "Columns docs"), el("div", {}, (() => { const st = modelDocsStatus(state, m); if (!st.withSchema) return el("span", { class: "empty" }, "Schema disabled"); if (!st.colTotal) return el("span", { class: "empty" }, "No columns found"); return el("span", { class: `pillSmall ${st.colMissing ? "pillBad" : "pillGood"}` }, `${st.colDoc}/${st.colTotal} columns documented`); })()),
          el("div", { class: "k" }, "Deps"), el("div", {}, deps.length ? joinInline(deps) : el("span", { class: "empty" }, "—")),
          el("div", { class: "k" }, "Used by"), el("div", {}, usedBy.length ? joinInline(usedBy) : el("span", { class: "empty" }, "—")),
          el("div", { class: "k" }, "Sources"), el("div", {}, sourcesUsed.length ? joinInline(sourcesUsed) : el("span", { class: "empty" }, "—")),
        )
      ),
      renderModelConfigMetaCard(m),
      miniPanel,
      m.description_html
        ? el("div", { class: "card" }, el("h3", {}, "Description"), el("div", { class: "desc", html: m.description_html }))
        : el("div", { class: "card" }, el("h3", {}, "Description"), el("p", { class: "empty" }, "No description."))
    );
  }

  if (tab === "columns") {
    return buildColumnsCard(state, m, colFromRoute);
  }

  if (tab === "lineage") {
    const cols = m.columns || [];
    const rows = cols
      .filter(c => (c.lineage || []).length)
      .map(c =>
        el("tr", {},
          el("td", {}, el("code", {}, c.name)),
          el("td", {}, renderLineage(c.lineage || []))
        )
      );

    return el("div", { class: "card" },
      el("h3", {}, "Column lineage"),
      rows.length
        ? el("table", { class: "table" },
            el("thead", {}, el("tr", {}, el("th", {}, "Column"), el("th", {}, "Lineage"))),
            el("tbody", {}, ...rows)
          )
        : el("p", { class: "empty" }, "No lineage available for this model’s columns.")
    );
  }

  if (tab === "code") {
    const { query } = parseHashWithQuery();
    const codeView = query.get("code") || "";
    return renderModelCodeTab(state, m, codeView);
  }

  if (tab === "meta") {
    return el("div", {},
      renderModelConfigMetaCard(m, { includeRaw: true })
    );
  }

  if (tab === "contract") {
    return buildContractCard(state, m);
  }

  return el("div", { class: "card" }, el("p", { class: "empty" }, "Unknown tab."));
}

function cssSafeId(s) {
  return String(s || "").replace(/[^a-zA-Z0-9_-]+/g, "_");
}

function renderSource(state, sourceName, tableName) {
  const key = `${sourceName}.${tableName}`;
  const s = state.bySource.get(key);

  if (!s) {
    return el("div", { class: "card" }, el("h2", {}, "Source not found"), el("p", { class: "empty" }, key));
  }

  const consumers = (s.consumers || []).map(m =>
    el("a", { href: routeWithFacets(`#/model/${escapeHashPart(m)}`) }, m)
  );

  const fc = sourceFreshnessConfig(s);

  const statusBadge = fc.configured
    ? el("span", { class: "pillSmall pillGood", title: fc.reason }, "Configured")
    : el("span", { class: "pillSmall pillBad", title: fc.reason }, "Missing freshness");

  const loadedAtNode = fc.loadedAtField
    ? el("code", {}, fc.loadedAtField)
    : el("span", { class: "empty" }, "—");

  const warnNode = fc.warnMinutes != null
    ? el("span", { class: "pillSmall pillWarn", title: `${Math.round(fc.warnMinutes)} minutes` }, `Warn after ${formatMinutesCompact(fc.warnMinutes)}`)
    : el("span", { class: "empty" }, "—");

  const errNode = fc.errorMinutes != null
    ? el("span", { class: "pillSmall pillBad", title: `${Math.round(fc.errorMinutes)} minutes` }, `Error after ${formatMinutesCompact(fc.errorMinutes)}`)
    : el("span", { class: "empty" }, "—");

  return el("div", { class: "grid" },
    el("div", { class: "card" },
      el("div", { class: "grid2" },
        el("div", {}, el("h2", {}, key)),
        el("div", {},
          el("button", { class: "btn", onclick: () => { location.hash = routeWithFacets("#/"); } }, "← Overview")
        )
      ),
      el("div", { class: "kv" },
        el("div", { class: "k" }, "Relation"), el("div", {}, el("code", {}, s.relation || "—")),
        el("div", { class: "k" }, "Freshness"), el("div", {}, statusBadge),
        el("div", { class: "k" }, "Loaded at field"), el("div", {}, loadedAtNode),
        el("div", { class: "k" }, "Warn threshold"), el("div", {}, warnNode),
        el("div", { class: "k" }, "Error threshold"), el("div", {}, errNode),
        el("div", { class: "k" }, "Consumers"), el("div", {}, consumers.length ? joinInline(consumers) : el("span", { class: "empty" }, "—"))
      )
    ),
    s.description_html
      ? el("div", { class: "card" }, el("h2", {}, "Description"), el("div", { class: "desc", html: s.description_html }))
      : null
  );
}

function macroSourceText(m) {
  if (!m) return "";
  return (
    m.source ||
    m.raw_sql ||
    m.sql ||
    m.definition ||
    m.code ||
    (m.meta && (m.meta.source || m.meta.raw_sql || m.meta.sql || m.meta.code)) ||
    ""
  );
}

function renderMacros(state, qFromRoute) {
  const ms = state.manifest.macros || [];
  const wrap = el("div", { class: "card" }, el("h2", {}, "Macros"));

  if (!ms.length) {
    wrap.appendChild(el("p", { class: "empty" }, "No macros discovered."));
    return wrap;
  }

  const q0 = (qFromRoute || "").trim();
  state.macroQuery = q0;

  const countEl = el("div", { class: "muted", style: "margin-top:6px;" }, "");
  const list = el("ul", { class: "docList" });

  const input = el("input", {
    class: "search",
    type: "search",
    placeholder: "Search macros…",
    value: q0,
  });

  const apply = () => {
    const q = (input.value || "").trim().toLowerCase();

    const filtered = q
      ? ms.filter(m => {
          const name = (m.name || "").toLowerCase();
          const kind = (m.kind || "").toLowerCase();
          const path = (m.path || "").toLowerCase();
          const src = macroSourceText(m).toLowerCase();
          return name.includes(q) || kind.includes(q) || path.includes(q) || src.includes(q);
        })
      : ms.slice();

    filtered.sort((a, b) => (a.name || "").localeCompare(b.name || ""));

    countEl.textContent = q
      ? `${filtered.length} of ${ms.length} macros`
      : `${ms.length} macros`;

    list.replaceChildren(
      ...filtered.map(m => {
        const src = macroSourceText(m);
        const snip = q ? makeSnippet(src, q, 90) : "";
        const subParts = [
          (m.kind || "macro").toUpperCase(),
          m.path ? `• ${m.path}` : "",
          snip ? `• ${snip}` : "",
        ].filter(Boolean).join(" ");

        const href = routeWithFacets(`#/macro/${escapeHashPart(m.name)}`);

        return el("li", { class: "docRow" },
          el("a", {
            href,
            onclick: (e) => { e.preventDefault(); location.hash = href; },
            title: m.path || m.name,
          },
            el("div", { class: "docRowMain" },
              el("div", { class: "docRowTitle" }, m.name),
              el("div", { class: "docRowSub" }, subParts)
            ),
            el("div", { class: "docRowPills" },
              el("span", { class: "pillSmall" }, m.kind || "macro")
            )
          )
        );
      })
    );
  };

  const syncUrl = debounce(() => {
    const v = (input.value || "").trim();
    replaceHashQuery((q) => {
      if (v) q.set("mq", v);
      else q.delete("mq");
    });
  }, 200);

  input.oninput = () => {
    syncUrl();
    apply();
  };

  input.onkeydown = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      input.value = "";
      syncUrl();
      apply();
    }
  };

  wrap.appendChild(input);
  wrap.appendChild(countEl);
  wrap.appendChild(list);

  apply();
  return wrap;
}

function renderMacro(state, name) {
  const ms = state.manifest.macros || [];
  const m =
    ms.find(x => x.name === name) ||
    ms.find(x => (x.name || "").toLowerCase() === (name || "").toLowerCase());

  const back = state.macroQuery
    ? `#/macros?mq=${encodeURIComponent(state.macroQuery)}`
    : "#/macros";

  if (!m) {
    return el("div", { class: "card" },
      el("div", { class: "row" },
        el("a", {
          class: "btn",
          href: routeWithFacets(back),
          onclick: (e) => { e.preventDefault(); location.hash = routeWithFacets(back); }
        }, "← Macros")
      ),
      el("h2", {}, "Macro not found"),
      el("p", { class: "empty" }, name)
    );
  }

  const src = macroSourceText(m);
  const maxChars = 16000;
  const clipped = src && src.length > maxChars ? (src.slice(0, maxChars) + "\n\n… (truncated)") : src;

  return el("div", { class: "card" },
    el("div", { class: "row" },
      el("a", {
        class: "btn",
        href: routeWithFacets(back),
        onclick: (e) => { e.preventDefault(); location.hash = routeWithFacets(back); }
      }, "← Macros"),
      el("span", { class: "pill" }, m.kind || "macro")
    ),
    el("h2", {}, m.name),
    el("div", { class: "kvRows" },
      el("div", { class: "kvRow" }, el("span", { class: "k" }, "Kind"), el("span", { class: "v" }, m.kind || "macro")),
      el("div", { class: "kvRow" }, el("span", { class: "k" }, "Path"), el("span", { class: "v" }, m.path ? el("code", {}, m.path) : "—"))
    ),
    clipped
      ? el("details", { class: "codeDetails" },
          el("summary", { class: "codeSummary" }, "Show macro source"),
          el("pre", { class: "codeBlock" }, clipped)
        )
      : el("p", { class: "empty" }, "No macro source available in manifest.")
  );
}

function joinInline(nodes) {
  const wrap = el("span", {});
  nodes.forEach((n, i) => {
    if (i) wrap.appendChild(document.createTextNode(", "));
    wrap.appendChild(n);
  });
  return wrap;
}

// -------- Structured meta/config panel ---------------------------------

function tryParseRelationParts(rel) {
  const s = String(rel || "").trim();
  if (!s) return {};
  // Avoid guessing when relation contains quoting or brackets
  if (/[`"\[\]]/.test(s)) return {};
  const parts = s.split(".").map(p => p.trim()).filter(Boolean);
  if (parts.length === 3) return { database: parts[0], schema: parts[1], identifier: parts[2] };
  if (parts.length === 2) return { schema: parts[0], identifier: parts[1] };
  return {};
}

function asStringArray(v) {
  if (!v) return [];
  if (Array.isArray(v)) return v.map(x => String(x)).filter(Boolean);
  if (typeof v === "string") {
    // allow comma-separated
    return v.split(",").map(s => s.trim()).filter(Boolean);
  }
  return [String(v)];
}

function renderPillList(values) {
  const vs = asStringArray(values);
  if (!vs.length) return el("span", { class: "empty" }, "—");
  return joinInline(vs.map(x => el("span", { class: "pillSmall" }, x)));
}

function jsonPreview(obj, maxChars = 6000) {
  let s;
  try { s = JSON.stringify(obj, null, 2); }
  catch { s = String(obj); }
  if (s.length > maxChars) s = s.slice(0, maxChars) + "\n… (truncated)";
  return s;
}

function renderMetaValue(v) {
  if (v == null || v === "") return el("span", { class: "empty" }, "—");
  if (typeof v === "boolean") return el("span", { class: "pillSmall" }, v ? "true" : "false");
  if (typeof v === "number") return el("code", {}, String(v));
  if (Array.isArray(v)) return renderPillList(v);
  if (typeof v === "object") {
    return el("details", {},
      el("summary", { class: "codeSummary" }, "View"),
      el("pre", { class: "mono", style: "white-space:pre-wrap; margin:8px 0 0 0;" }, jsonPreview(v))
    );
  }
  const s = String(v);
  // prefer code styling for short config-y strings
  return s.length <= 80 && !/\s/.test(s) ? el("code", {}, s) : el("span", {}, s);
}

function pick(obj, keys) {
  for (const k of keys) {
    const v = obj && obj[k];
    if (v != null && String(v).trim() !== "") return v;
  }
  return "";
}

function renderModelConfigMetaCard(m, { includeRaw = false } = {}) {
  const relParts = tryParseRelationParts(m.relation);

  const database = pick(m, ["database"]) || relParts.database || "";
  const schema = pick(m, ["schema"]) || relParts.schema || "";
  const alias = pick(m, ["alias", "identifier", "name"]) || relParts.identifier || "";

  const tags = pick(m, ["tags"]) || (m.meta && (m.meta.tags || m.meta.tag)) || "";
  const owners = pick(m, ["owners", "owner"]) || (m.meta && (m.meta.owners || m.meta.owner)) || "";

  const reserved = new Set([
    "name","kind","relation","materialized","path",
    "database","schema","alias","identifier",
    "tags","tag","owners","owner",
  ]);

  // Custom meta/config: show whatever else is present without duplicating known fields.
  const custom = {};
  const metaObj = (m.meta && typeof m.meta === "object" && !Array.isArray(m.meta)) ? m.meta : null;
  const cfgObj = (m.config && typeof m.config === "object" && !Array.isArray(m.config)) ? m.config : null;

  function addCustomFrom(obj) {
    if (!obj) return;
    for (const [k, v] of Object.entries(obj)) {
      if (reserved.has(k)) continue;
      if (v == null || v === "" || (Array.isArray(v) && !v.length)) continue;
      if (custom[k] == null) custom[k] = v;
    }
  }
  addCustomFrom(cfgObj);
  addCustomFrom(metaObj);

  const customKeys = Object.keys(custom).sort((a, b) => a.localeCompare(b));

  const customDetails = customKeys.length
    ? el("details", {},
        el("summary", { class: "codeSummary" }, `Custom meta (${customKeys.length})`),
        el("table", { class: "table", style: "margin-top:10px;" },
          el("thead", {}, el("tr", {}, el("th", {}, "Key"), el("th", {}, "Value"))),
          el("tbody", {},
            ...customKeys.map(k =>
              el("tr", {},
                el("td", {}, el("code", {}, k)),
                el("td", {}, renderMetaValue(custom[k]))
              )
            )
          )
        )
      )
    : el("p", { class: "empty", style: "margin:10px 0 0 0;" }, "No custom meta.");

  const rawBlock = includeRaw
    ? el("details", { class: "codeDetails", style: "margin-top:10px;" },
        el("summary", { class: "codeSummary" }, "Raw meta/config JSON"),
        el("pre", { class: "mono", style: "white-space:pre-wrap; margin:8px 0 0 0;" },
          jsonPreview({ config: cfgObj || null, meta: metaObj || null })
        )
      )
    : null;

  return el("div", { class: "card" },
    el("h3", {}, "Config & meta"),
    el("div", { class: "kv" },
      el("div", { class: "k" }, "Materialized"), el("div", {}, renderMetaValue(m.materialized || "")),
      el("div", { class: "k" }, "Database"), el("div", {}, renderMetaValue(database)),
      el("div", { class: "k" }, "Schema"), el("div", {}, renderMetaValue(schema)),
      el("div", { class: "k" }, "Alias"), el("div", {}, renderMetaValue(alias)),
      el("div", { class: "k" }, "Relation"), el("div", {}, m.relation ? el("code", {}, m.relation) : el("span", { class: "empty" }, "—")),
      el("div", { class: "k" }, "Path"), el("div", {}, el("code", {}, m.path || "—")),
      el("div", { class: "k" }, "Tags"), el("div", {}, renderPillList(tags)),
      el("div", { class: "k" }, "Owners"), el("div", {}, renderPillList(owners)),
    ),
    customDetails,
    rawBlock
  );
}

function renderLineage(items) {
  if (!items || !items.length) return el("span", { class: "empty" }, "—");
  // items are already normalized by docs.py lineage logic:
  // { from_relation, from_column, transformed }
  const ul = el("ul", { style: "margin:0; padding-left:16px;" });
  for (const it of items) {
    const label = `${it.from_relation}.${it.from_column}` + (it.transformed ? " (xform)" : "");
    ul.appendChild(el("li", {}, el("code", {}, label)));
  }
  return ul;
}

function toastOnce({ key, title, body, actionLabel, onAction }) {
  try {
    if (localStorage.getItem(key) === "1") return;
    localStorage.setItem(key, "1");
  } catch {}

  const node = el("div", { class: "toast" },
    el("div", {},
      el("div", { class: "toastTitle" }, title),
      el("div", { class: "toastBody" }, body)
    ),
    el("div", { class: "toastActions" },
      actionLabel ? el("button", { class: "toastBtn", onclick: () => { try { onAction?.(); } finally { node.remove(); } } }, actionLabel) : null,
      el("button", { class: "toastBtn", onclick: () => node.remove() }, "Got it")
    )
  );

  document.body.appendChild(node);
  setTimeout(() => { try { node.remove(); } catch {} }, 5500);
}

function renderTabs(active, onPick) {
  const tabs = [
    ["overview", "Overview"],
    ["columns", "Columns"],
    ["contract", "Contract"], 
    ["lineage", "Lineage"],
    ["code", "Code"],
    ["meta", "Meta"],
  ];

  return el("div", { class: "tabs" },
    ...tabs.map(([id, label]) =>
      el("button", {
        class: `tab ${active === id ? "active" : ""}`,
        onclick: () => onPick(id),
      }, label)
    )
  );
}

function makeSnippet(text, query, maxLen = 90) {
  const t = (text || "").replace(/\s+/g, " ").trim();
  if (!t) return "";

  const q = (query || "").trim().toLowerCase();
  if (!q) return t.length > maxLen ? t.slice(0, maxLen - 1) + "…" : t;

  const idx = t.toLowerCase().indexOf(q);
  if (idx < 0) return t.length > maxLen ? t.slice(0, maxLen - 1) + "…" : t;

  const start = Math.max(0, idx - Math.floor(maxLen * 0.35));
  const end = Math.min(t.length, start + maxLen);

  const prefix = start > 0 ? "…" : "";
  const suffix = end < t.length ? "…" : "";
  return prefix + t.slice(start, end) + suffix;
}

function snippet(text, maxLen = 70) {
  const t = (text || "").replace(/\s+/g, " ").trim();
  if (!t) return "";
  return t.length > maxLen ? t.slice(0, maxLen - 1) + "…" : t;
}

function buildColumnsCard(state, m, colFromRoute) {
  const cols = m.columns || [];
  const withSchema = !!state.manifest.project?.with_schema;

  // UI state (persisted in memory per model; easy to persist later if you want)
  state.colUI ||= {};
  const uiState = (state.colUI[m.name] ||= {
    q: "",
    sortKey: "name",   // name | dtype | nullable | documented
    sortDir: "asc",    // asc | desc
    undocOnly: false,
    lineageOnly: false, 
    expanded: new Set(),
  });

  const card = el("div", { class: "card" });
  const tools = el("div", { class: "colTools" });

  const qInput = el("input", {
    class: "search",
    type: "search",
    placeholder: "Filter columns…",
    value: uiState.q || "",
    oninput: (e) => {
      uiState.q = e.target.value || "";
      renderBody(); // updates tbody only
    },
  });

  const undocBtn = el("button", {
    class: "btn",
    onclick: () => {
      uiState.undocOnly = !uiState.undocOnly;
      undocBtn.textContent = uiState.undocOnly ? "Showing undocumented" : "Undocumented only";
      renderBody();
    }
  }, uiState.undocOnly ? "Showing undocumented" : "Undocumented only");

  const lineageOnlyBtn = el("button", {
    class: "btn",
    onclick: () => {
      uiState.lineageOnly = !uiState.lineageOnly;
      lineageOnlyBtn.textContent = uiState.lineageOnly ? "Showing lineage-only" : "Lineage only";
      renderBody();
    }
  }, uiState.lineageOnly ? "Showing lineage-only" : "Lineage only");

  const expandAllBtn = el("button", {
    class: "btn",
    onclick: () => {
      // Expand all rows currently visible (after filters)
      const visible = getVisibleRows();
      uiState.expanded = new Set(visible.map(c => c.name));
      renderBody();
      queueMicrotask(() => qInput.focus());
    }
  }, "Expand all");

  const collapseAllBtn = el("button", {
    class: "btn",
    onclick: () => {
      uiState.expanded.clear();
      renderBody();
      queueMicrotask(() => qInput.focus());
    }
  }, "Collapse all");

  const countNode = el("span", { class: "colCount" }, "");

  tools.append(
    qInput,
    undocBtn,
    lineageOnlyBtn,
    expandAllBtn,
    collapseAllBtn,
    countNode
  );

  const table = el("table", { class: "table" });
  const thead = el("thead");
  const tbody = el("tbody");
  table.append(thead, tbody);

  function sortArrow(key) {
    if (uiState.sortKey !== key) return "";
    return uiState.sortDir === "asc" ? "▲" : "▼";
  }

  function setSort(key) {
    if (uiState.sortKey === key) uiState.sortDir = (uiState.sortDir === "asc" ? "desc" : "asc");
    else { uiState.sortKey = key; uiState.sortDir = "asc"; }
    renderBody();
    renderHead();
  }

  function renderHead() {
    thead.replaceChildren(
      el("tr", {},
        el("th", {},
          el("button", { class: "thBtn", onclick: () => setSort("name") }, "Name", el("span", { class: "sortArrow" }, sortArrow("name")))
        ),
        el("th", {},
          el("button", { class: "thBtn", onclick: () => setSort("dtype") }, "Type", el("span", { class: "sortArrow" }, sortArrow("dtype")))
        ),
        el("th", {},
          el("button", { class: "thBtn", onclick: () => setSort("nullable") }, "Null", el("span", { class: "sortArrow" }, sortArrow("nullable")))
        ),
        el("th", {},
          el("button", { class: "thBtn", onclick: () => setSort("documented") }, "Docs", el("span", { class: "sortArrow" }, sortArrow("documented")))
        ),
        el("th", {}, "Description")
      )
    );
  }

  function isDocumented(c) {
    const txt = (c.description_text || "").trim();
    const html = (c.description_html || "").trim();
    return !!(txt || html);
  }

  function lineageCount(c) {
    return (c.lineage || []).length;
  }

  function renderDrawer(c) {
    const descHtml = (c.description_html && c.description_html.trim())
      ? c.description_html
      : '<span class="empty">No description.</span>';

    const lin = c.lineage || [];
    const linNode = lin.length
      ? renderLineage(lin)
      : el("span", { class: "empty" }, "No lineage available.");

    const copyName = el("button", {
      class: "btnTiny",
      onclick: async (e) => {
        e.stopPropagation();
        await copyText(`${m.name}.${c.name}`);
      }
    }, "Copy name");

    const copyRelation = el("button", {
      class: "btnTiny",
      onclick: async (e) => {
        e.stopPropagation();
        await copyText(`${m.relation || ""}`.trim());
      }
    }, "Copy relation");

    const copyDtype = el("button", {
      class: "btnTiny",
      onclick: async (e) => {
        e.stopPropagation();
        await copyText(c.dtype || "");
      }
    }, "Copy dtype");

    const copyLineageJSON = el("button", {
      class: "btnTiny",
      onclick: async (e) => {
        e.stopPropagation();
        await copyText(JSON.stringify(lin || [], null, 2));
      }
    }, "Copy lineage JSON");

    const copyLineageCSV = el("button", {
      class: "btnTiny",
      onclick: async (e) => {
        e.stopPropagation();
        const rows = (lin || []).map(x =>
          [x.from_relation ?? "", x.from_column ?? "", x.transformed ? "1" : "0"].join(",")
        );
        await copyText(["from_relation,from_column,transformed", ...rows].join("\n"));
      }
    }, "Copy lineage CSV");

    return el("div", { class: "drawer" },
      el("div", { class: "colTools" },
        el("span", { class: "pillSmall" }, "COLUMN"),
        el("code", {}, `${m.name}.${c.name}`),
        el("span", { class: "colCount" }, lin.length ? `${lin.length} lineage refs` : "")
      ),
      el("div", { class: "drawerTools" },
        copyName,
        copyRelation,
        copyDtype,
        copyLineageJSON,
        copyLineageCSV
      ),
      el("div", { class: "drawerGrid" },
        el("div", { class: "drawerBox" },
          el("div", { class: "drawerTitle" }, "Description"),
          el("div", { class: "desc", html: descHtml })
        ),
        el("div", { class: "drawerBox" },
          el("div", { class: "drawerTitle" }, "Lineage"),
          linNode
        )
      )
    );
  }

  function isDocumented(c) {
    const txt = (c.description_text || "").trim();
    const html = (c.description_html || "").trim();
    return !!(txt || html);
  }

  function lineageCount(c) {
    return (c.lineage || []).length;
  }

  function getVisibleRows() {
    if (!withSchema || !cols.length) return [];
    const q = (uiState.q || "").trim().toLowerCase();

    let rows = cols.slice();

    if (q) {
      rows = rows.filter(c => {
        const name = (c.name || "").toLowerCase();
        const dtype = (c.dtype || "").toLowerCase();
        const dtxt = (c.description_text || "").toLowerCase();
        return name.includes(q) || dtype.includes(q) || dtxt.includes(q);
      });
    }

    if (uiState.undocOnly) rows = rows.filter(c => !isDocumented(c));
    if (uiState.lineageOnly) rows = rows.filter(c => lineageCount(c) > 0);

    // Respect sort settings so "expand all" expands the visible ordering
    const dir = uiState.sortDir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      if (uiState.sortKey === "name") return dir * String(a.name).localeCompare(String(b.name));
      if (uiState.sortKey === "dtype") return dir * String(a.dtype || "").localeCompare(String(b.dtype || ""));
      if (uiState.sortKey === "nullable") return dir * ((a.nullable === b.nullable) ? 0 : (a.nullable ? 1 : -1));
      if (uiState.sortKey === "documented") {
        const da = isDocumented(a) ? 1 : 0;
        const db = isDocumented(b) ? 1 : 0;
        return dir * (da - db);
      }
      return 0;
    });

    return rows;
  }

  function renderBody() {
    if (!withSchema) {
      tbody.replaceChildren(
        el("tr", {}, el("td", { colspan: "5" }, "Schema collection disabled."))
      );
      countNode.textContent = "";
      return;
    }

    if (!cols.length) {
      tbody.replaceChildren(
        el("tr", {}, el("td", { colspan: "5" }, "No columns found."))
      );
      countNode.textContent = "";
      return;
    }

    const q = (uiState.q || "").trim().toLowerCase();
    let rows = cols.slice();

    // filter
    if (q) {
      rows = rows.filter(c => {
        const name = (c.name || "").toLowerCase();
        const dtype = (c.dtype || "").toLowerCase();
        const dtxt = (c.description_text || "").toLowerCase();
        return name.includes(q) || dtype.includes(q) || dtxt.includes(q);
      });
    }

    // undocumented-only toggle
    if (uiState.undocOnly) rows = rows.filter(c => !isDocumented(c));

    if (uiState.lineageOnly) rows = rows.filter(c => lineageCount(c) > 0);

    const undocCount = cols.filter(c => !isDocumented(c)).length;
    const linCount = cols.filter(c => lineageCount(c) > 0).length;
    countNode.textContent = `Showing ${rows.length}/${cols.length} • Undocumented: ${undocCount} • With lineage: ${linCount}`;

    // sort
    const dir = uiState.sortDir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      if (uiState.sortKey === "name") return dir * String(a.name).localeCompare(String(b.name));
      if (uiState.sortKey === "dtype") return dir * String(a.dtype || "").localeCompare(String(b.dtype || ""));
      if (uiState.sortKey === "nullable") return dir * ((a.nullable === b.nullable) ? 0 : (a.nullable ? 1 : -1));
      if (uiState.sortKey === "documented") {
        const da = isDocumented(a) ? 1 : 0;
        const db = isDocumented(b) ? 1 : 0;
        return dir * (da - db);
      }
      return 0;
    });

    // build tbody
    const out = [];
    for (const c of rows) {
      const documented = isDocumented(c);
      const linN = lineageCount(c);

      const rowId = `col-${cssSafeId(m.name)}-${cssSafeId(c.name)}`;

      const tr = el("tr", {
        id: rowId,
        class: `colRow ${documented ? "" : "undoc"}`,
        onclick: () => {
          if (uiState.expanded.has(c.name)) uiState.expanded.delete(c.name);
          else uiState.expanded.add(c.name);
          renderBody(); // re-render tbody only
          // keep focus in filter input
          queueMicrotask(() => qInput.focus());
        }
      },
        el("td", {}, el("code", {}, c.name)),
        el("td", {}, el("code", {}, c.dtype || "")),
        el("td", {},
          c.nullable
            ? el("span", { class: "pillSmall pillBad" }, "NULL")
            : el("span", { class: "pillSmall pillGood" }, "NOT NULL")
        ),
        el("td", {},
          documented
            ? el("span", { class: "pillSmall pillGood" }, "DOCS")
            : el("span", { class: "pillSmall pillBad" }, "MISSING")
        ),
        el("td", {},
          // short preview + lineage count
          (c.description_text && c.description_text.trim())
            ? el("span", {}, snippet(c.description_text, 70), linN ? ` • ${linN} lineage` : "")
            : el("span", { class: "empty" }, "—", linN ? ` • ${linN} lineage` : "")
        ),
      );

      out.push(tr);

      if (uiState.expanded.has(c.name)) {
        out.push(
          el("tr", { class: "drawerRow" },
            el("td", { colspan: "5" }, renderDrawer(c))
          )
        );
      }
    }

    tbody.replaceChildren(...out);

    // Column deep-link: highlight + scroll + auto-expand
    const colName = (colFromRoute || "").trim();
    if (colName) {
      // ensure expanded
      uiState.expanded.add(colName);

      queueMicrotask(() => {
        const rowId = `col-${cssSafeId(m.name)}-${cssSafeId(colName)}`;
        const row = document.getElementById(rowId);
        if (!row) return;

        document.querySelectorAll("tr.colHit").forEach(n => n.classList.remove("colHit"));
        row.classList.add("colHit");
        row.scrollIntoView({ block: "center", behavior: "smooth" });
        setTimeout(() => row.classList.remove("colHit"), 2200);
      });
    }
  }

  // initial render
  card.append(el("h3", {}, `Columns (${cols.length})`), tools, table);
  renderHead();
  renderBody();

  return card;
}

function buildContractCard(state, m) {
  const withSchema = !!state.manifest.project?.with_schema;
  const drift = computeContractDrift(m, withSchema);

  const rawCols = contractColumnsFrom(m).map(normalizeContractCol).filter(Boolean);
  const tblConstraints = contractTableConstraintsFrom(m);

  // UI state per model
  state.contractUI ||= {};
  const uiState = (state.contractUI[m.name] ||= { q: "" });

  const card = el("div", { class:"card" });

  const tools = el("div", { class:"colTools" });
  const qInput = el("input", {
    class:"search",
    type:"search",
    placeholder:"Filter contract columns…",
    value: uiState.q || "",
    oninput: (e) => { uiState.q = e.target.value || ""; renderBody(); }
  });

  const headRow = el("div", { class:"row", style:"align-items:center; justify-content:space-between;" },
    el("h3", { style:"margin:0;" }, "Contract"),
    hasContract(m)
      ? el("div", { class:"pillRow" },
          el("span", { class:"pillSmall pillGood" }, "Contracted"),
          drift.status === "verified"
         ? el("span", { class:"pillSmall pillGood", title:"Contract matches warehouse schema" }, "Verified")
         : drift.status === "drift"
           ? el("span", { class:"pillSmall pillBad", title:"Contract differs from warehouse schema" }, "Drift detected")
           : el("span", { class:"pillSmall pillWarn", title:"Warehouse schema not available (run with schema collection enabled)" }, "Schema unavailable"),
          (m.contract && typeof m.contract === "object" && m.contract.enforced != null)
            ? el("span", { class:"pillSmall" }, m.contract.enforced ? "enforced" : "not enforced")
            : null
        )
      : null
  );

  tools.appendChild(qInput);

  const body = el("div", {});

  function renderBody() {
    const q = (uiState.q || "").trim().toLowerCase();

    const rows = rawCols
      .filter(c => {
        if (!q) return true;
        const cstr = [
          c.name,
          c.dtype || "",
          (c.nullable === true ? "nullable" : c.nullable === false ? "not null" : ""),
          JSON.stringify(c.constraints || []),
        ].join(" ").toLowerCase();
        return cstr.includes(q);
      })
      .sort((a,b) => a.name.localeCompare(b.name))
      .map(c => {
        // Optional: show “missing/mismatch” if schema is available
        let statusNode = null;
        if (drift.schemaAvailable) {
          const key = c.name.toLowerCase();
          const rec = drift.byName.get(key);
          if (!rec || rec.missing) {
            statusNode = el("span", { class:"pillSmall pillBad" }, "missing");
          } else if (rec.issues && rec.issues.length) {
            const title = (() => {
              const exp = rec.expected ? `${rec.expected.dtype || "—"} / ${rec.expected.nullable == null ? "—" : (rec.expected.nullable ? "nullable" : "not null")}` : "";
              const act = rec.actual ? `${rec.actual.dtype || "—"} / ${(rec.actual.nullable ? "nullable" : "not null")}` : "";
              return (exp && act) ? `expected: ${exp}\nactual: ${act}` : "";
            })();
            statusNode = el("span", { class:"pillSmall pillWarn", title }, `mismatch: ${rec.issues.join(", ")}`);
          } else {
            statusNode = el("span", { class:"pillSmall pillGood" }, "ok");
          }
        } else if (withSchema) {
          // with_schema enabled but no columns returned for this model
          statusNode = el("span", { class:"pillSmall pillWarn" }, "unavailable");
        }

        const colLink = routeWithFacets(
          `#/model/${escapeHashPart(m.name)}?tab=columns&col=${encodeURIComponent(c.name)}`
        );

        return el("tr", {},
          el("td", {},
            el("a", {
              href: colLink,
              onclick: (e) => { e.preventDefault(); location.hash = colLink; },
              style:"text-decoration:none;"
            }, el("code", {}, c.name))
          ),
          el("td", {}, c.dtype ? el("code", {}, c.dtype) : el("span", { class:"empty" }, "—")),
          el("td", {},
            c.nullable === true ? el("span", { class:"pillSmall" }, "nullable") :
            c.nullable === false ? el("span", { class:"pillSmall pillBad" }, "not null") :
            el("span", { class:"empty" }, "—")
          ),
          el("td", {}, renderConstraintsList(c.constraints)),
          el("td", {}, statusNode || el("span", { class:"empty" }, "—"))
        );
      });

    const hasAnything = rawCols.length || (tblConstraints && tblConstraints.length);

    body.replaceChildren(
      !hasAnything
        ? el("p", { class:"empty", style:"margin:10px 0 0 0;" },
            "No contract defined for this model."
          )
        : el("div", {},
            drift.status !== "none"
            ? el("div", { style:"margin:10px 0 14px 0;" },
                el("div", { class:"k", style:"margin-bottom:6px;" }, "Drift summary"),
                drift.status === "unavailable"
                  ? el("p", { class:"empty", style:"margin:0;" }, "Schema unavailable for diff. Enable schema collection to verify the contract.")
                  : el("div", { class:"pillRow" },
                      el("span", { class:"pillSmall" }, `missing: ${drift.missing.length}`),
                      el("span", { class:"pillSmall" }, `extra: ${drift.extra.length}`),
                      el("span", { class:"pillSmall" }, `mismatched: ${drift.mismatches.length}`),
                      (!drift.constraintsComparable && rawCols.some(c => (c.constraints || []).length))
                        ? el("span", { class:"pillSmall pillWarn", title:"Warehouse schema does not include constraints yet" }, "constraints: not verifiable")
                        : null
                    )
              )
            : null,

            (tblConstraints && tblConstraints.length)
              ? el("div", { style:"margin:10px 0 14px 0;" },
                  el("div", { class:"k", style:"margin-bottom:6px;" }, "Table constraints"),
                  el("div", {}, renderConstraintsList(tblConstraints))
                )
              : null,

            (drift.status !== "unavailable" && drift.extra.length)
              ? el("div", { style:"margin:10px 0 14px 0;" },
                  el("div", { class:"k", style:"margin-bottom:6px;" }, "Extra columns in warehouse (not in contract)"),
                  el("div", {},
                    joinInline(
                      drift.extra
                        .slice(0, 60)
                        .map(nm => {
                          const href = routeWithFacets(`#/model/${escapeHashPart(m.name)}?tab=columns&col=${encodeURIComponent(nm)}`);
                          return el("a", { href, onclick:(e)=>{ e.preventDefault(); location.hash = href; } }, nm);
                        })
                    ),
                    drift.extra.length > 60 ? el("span", { class:"empty" }, ` … +${drift.extra.length - 60} more`) : null
                  )
                )
              : null,

            rawCols.length
              ? el("table", { class:"table" },
                  el("thead", {}, el("tr", {},
                    el("th", {}, "Column"),
                    el("th", {}, "Type"),
                    el("th", {}, "Nullability"),
                    el("th", {}, "Constraints"),
                    el("th", {}, withSchema ? "Status vs actual" : "Status"),
                  )),
                  el("tbody", {}, ...rows)
                )
              : el("p", { class:"empty" }, "No contract columns specified.")
          )
    );
  }

  renderBody();
  card.appendChild(headRow);
  card.appendChild(tools);
  card.appendChild(body);

  // Small “how to define” hint (kept lightweight + collapsible)
  card.appendChild(
    el("details", { class:"codeDetails", style:"margin-top:12px;" },
      el("summary", { class:"codeSummary" }, "How to define a contract"),
      el("pre", { class:"mono", style:"white-space:pre-wrap; margin:8px 0 0 0;" },
`# project.yml
docs:
  models:
    ${m.name}:
      contract:
        enforced: true
        columns:
          some_col:
            dtype: text
            nullable: false
            constraints: ["unique"]`
      )
    )
  );

  return card;
}

function normalizeMermaidKey(s) {
  s = (s || "").trim();
  if (!s) return "";
  // common prefixes some generators use
  const prefixes = ["model:", "model__", "model_", "m__", "m_", "source:", "source__", "src__", "src_"];
  for (const p of prefixes) {
    if (s.startsWith(p)) return s.slice(p.length);
  }
  return s;
}

function extractMermaidNodeLabel(g) {
  // Mermaid typically renders <g class="node"> with a <text> element and multiple <tspan>s.
  const text = g.querySelector("text");
  if (!text) return "";
  let out = "";
  const tspans = text.querySelectorAll("tspan");
  if (tspans && tspans.length) {
    for (const t of tspans) out += (t.textContent || "") + " ";
  } else {
    out = text.textContent || "";
  }
  return out.replace(/\s+/g, " ").trim();
}

function svgEl(tag, attrs = {}, ...children) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.setAttribute("class", v);
    else n.setAttribute(k, String(v));
  }
  for (const c of children) {
    if (c == null) continue;
    n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return n;
}

function pathForEdgeLR(a, b) {
  const x1 = a.x + a.w;
  const y1 = a.y + a.h / 2;
  const x2 = b.x;
  const y2 = b.y + b.h / 2;
  const dx = Math.max(40, (x2 - x1) * 0.5);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

function pathForEdgeTB(a, b) {
  const x1 = a.x + a.w / 2;
  const y1 = a.y + a.h;
  const x2 = b.x + b.w / 2;
  const y2 = b.y;
  const dy = Math.max(40, (y2 - y1) * 0.5);
  return `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
}

function mountGraph(state, host, graph, opts = {}) {
  const { miniHost = null, showMini = true, nodeClick = "pin" } = opts;

  host.textContent = "";
  if (!graph || !graph.nodes || !graph.nodes.length) {
    host.appendChild(el("p", { class: "empty" }, "No DAG data available."));
    return;
  }

  const dir = (graph.direction || "LR").toUpperCase();
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  const byId = new Map(nodes.map(n => [n.id, n]));

  // SVG skeleton
  const svg = svgEl("svg", { class: "dagSvg", tabindex: "0" });
  const defs = svgEl("defs", {},
    svgEl("marker", {
      id: "arrow",
      markerWidth: "10",
      markerHeight: "10",
      refX: "9",
      refY: "3",
      orient: "auto",
      markerUnits: "strokeWidth"
    }, svgEl("path", { d: "M0,0 L10,3 L0,6 Z", class: "dagArrow" }))
  );
  svg.appendChild(defs);

  const viewport = svgEl("g", { class: "dagViewport" });
  svg.appendChild(viewport);

  const edgeLayer = svgEl("g", { class: "dagEdges" });
  const nodeLayer = svgEl("g", { class: "dagNodes" });
  viewport.append(edgeLayer, nodeLayer);

  // adjacency + element maps live INSIDE mountGraph
  const outAdj = new Map();  // id -> [{to, edgeEl}]
  const inAdj  = new Map();  // id -> [{from, edgeEl}]
  const edgeEls = [];        // [{from,to,el}]
  const nodeEls = new Map(); // id -> <g>

  function pushAdj(map, key, val) {
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(val);
  }

  // Edges
  for (const e of edges) {
    const a = byId.get(e.from);
    const b = byId.get(e.to);
    if (!a || !b) continue;

    const d = (dir === "TB") ? pathForEdgeTB(a, b) : pathForEdgeLR(a, b);
    const p = svgEl("path", {
      d,
      class: `dagEdge ${e.kind || ""}`,
      "data-from": e.from,
      "data-to": e.to
    });
    edgeLayer.appendChild(p);

    edgeEls.push({ from: e.from, to: e.to, el: p });
    pushAdj(outAdj, e.from, { to: e.to, el: p });
    pushAdj(inAdj,  e.to,   { from: e.from, el: p });
  }

  function setHover(id, on) {
    const sel = (q) => Array.from(svg.querySelectorAll(q));
    const nodes = sel(`g.dagNode[data-id="${CSS.escape(id)}"]`);
    for (const n of nodes) n.classList.toggle("hover", on);

    const connected = sel(`path.dagEdge[data-from="${CSS.escape(id)}"], path.dagEdge[data-to="${CSS.escape(id)}"]`);
    for (const p of connected) p.classList.toggle("hover", on);
  }

  function bfsSet(startId, mode, depth) {
    const nodesSet = new Set([startId]);
    const edgesSet = new Set();

    let frontier = new Set([startId]);

    for (let d = 0; d < depth; d++) {
      const next = new Set();

      for (const id of frontier) {
        if (mode === "down" || mode === "both") {
          for (const e of (outAdj.get(id) || [])) {
            nodesSet.add(e.to);
            edgesSet.add(e.el);
            next.add(e.to);
          }
        }
        if (mode === "up" || mode === "both") {
          for (const e of (inAdj.get(id) || [])) {
            nodesSet.add(e.from);
            edgesSet.add(e.el);
            next.add(e.from);
          }
        }
      }

      frontier = next;
      if (!frontier.size) break;
    }

    return { nodesSet, edgesSet };
  }

  function applyHighlight() {
    const pinned = state.graphUI?.pinned || "";
    const mode = state.graphUI?.mode || "off";
    const depth = Number(state.graphUI?.depth || 0);

    // reset
    for (const [id, g] of nodeEls) {
      g.classList.toggle("selected", id === pinned);
      g.classList.remove("dim", "hl");
    }
    for (const e of edgeEls) e.el.classList.remove("dim", "hl");

    if (!pinned || mode === "off" || depth <= 0) return;

    const { nodesSet, edgesSet } = bfsSet(pinned, mode, depth);

    for (const [id, g] of nodeEls) {
      const on = nodesSet.has(id);
      g.classList.toggle("hl", on);
      g.classList.toggle("dim", !on);
    }
    for (const e of edgeEls) {
      const on = edgesSet.has(e.el);
      e.el.classList.toggle("hl", on);
      e.el.classList.toggle("dim", !on);
    }
  }

  function setPinned(id) {
    state.graphUI.pinned = id || "";
    applyHighlight();
  }

  function togglePinned(id) {
    setPinned(state.graphUI.pinned === id ? "" : id);
  }

  // Nodes
  for (const n of nodes) {
    const isModel = n.kind === "model";
    const g = svgEl("g", {
      class: `dagNode ${n.kind} ${isModel ? (n.type || "sql") : "source"}`,
      transform: `translate(${n.x} ${n.y})`,
      tabindex: "0",
      role: "link",
      "data-id": n.id
    });

    const rect = svgEl("rect", {
      width: n.w,
      height: n.h,
      rx: 14,
      ry: 14,
      class: "dagRect"
    });

    const title = isModel ? (n.name || "") : `${n.source_name}.${n.table_name}`;
    const subtitle = n.relation || "";

    const t1 = svgEl("text", { x: 12, y: 20, class: "dagTitle" }, title);
    const t2 = svgEl("text", { x: 12, y: 38, class: "dagSub" }, subtitle);

    // badges (right side)
    const badges = [];
    if (isModel) {
      const b1 = svgEl("text", { x: n.w - 12, y: 20, class: "dagBadge", "text-anchor": "end" }, (n.type || "sql"));
      badges.push(b1);
      if (n.materialized) {
        const b2 = svgEl("text", { x: n.w - 12, y: 38, class: "dagBadge2", "text-anchor": "end" }, n.materialized);
        badges.push(b2);
      }
    } else {
      const b1 = svgEl("text", { x: n.w - 12, y: 20, class: "dagBadge", "text-anchor": "end" }, "source");
      badges.push(b1);
    }

    g.append(rect, t1);
    if (subtitle) g.appendChild(t2);
    for (const b of badges) g.appendChild(b);

    const route = n.route || "";
    const go = (ev) => {
      if (!route) return;
      try { ev?.preventDefault?.(); ev?.stopPropagation?.(); } catch {}
      // route contains raw parts; encode at the last moment
      if (route.startsWith("#/model/")) {
        const nm = route.slice("#/model/".length);
        location.hash = routeWithFacets(`#/model/${escapeHashPart(nm)}`);
      } else if (route.startsWith("#/source/")) {
        const rest = route.slice("#/source/".length).split("/");
        const s = rest[0] || "";
        const t = rest[1] || "";
        location.hash = routeWithFacets(`#/source/${escapeHashPart(s)}/${escapeHashPart(t)}`);
      } else {
        location.hash = routeWithFacets(route);
      }
    };

    g.addEventListener("click", (ev) => {
      if (nodeClick === "navigate") return go(ev);

      // Ctrl/Cmd click keeps the old "navigate" behavior
      if (ev.ctrlKey || ev.metaKey) return go(ev);

      ev.preventDefault();
      ev.stopPropagation();

      // toggle pinned selection
      togglePinned(n.id);
    });

    // optional: double click navigates too
    g.addEventListener("dblclick", (ev) => go(ev));

    g.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") go(ev);
    });

    // hover highlight
    g.addEventListener("mouseenter", () => setHover(n.id, true));
    g.addEventListener("mouseleave", () => setHover(n.id, false));

    nodeLayer.appendChild(g);

    nodeEls.set(n.id, g);
  }

  // --- pan/zoom (package-free) -----------------------------------------
  let scale = 1;
  let tx = 0;
  let ty = 0;
  let panning = false;
  let panStart = null;

  let miniSvg = null;
  let miniView = null;

  function updateMini() {
    if (!miniSvg || !miniView) return;

    const r = svg.getBoundingClientRect();
    const vx = (0 - tx) / scale;
    const vy = (0 - ty) / scale;
    const vw = r.width / scale;
    const vh = r.height / scale;

    miniView.setAttribute("x", String(vx));
    miniView.setAttribute("y", String(vy));
    miniView.setAttribute("width", String(vw));
    miniView.setAttribute("height", String(vh));
  }

  function apply() {
    viewport.setAttribute("transform", `translate(${tx} ${ty}) scale(${scale})`);
    updateMini();
  }

  function fit() {
    const r = host.getBoundingClientRect();
    const b = graph.bounds || {};
    const gw = (b.width || 1000);
    const gh = (b.height || 600);

    const pad = 24;
    const sx = (r.width - pad * 2) / gw;
    const sy = (r.height - pad * 2) / gh;
    scale = Math.max(0.1, Math.min(2.5, Math.min(sx, sy)));

    tx = pad;
    ty = pad;
    apply();
  }

  function reset() {
    scale = 1; tx = 0; ty = 0; apply();
  }

  function zoomBy(factor, cx, cy) {
    const rect = svg.getBoundingClientRect();
    const px = cx - rect.left;
    const py = cy - rect.top;

    const wx = (px - tx) / scale;
    const wy = (py - ty) / scale;

    const next = Math.max(0.1, Math.min(3.0, scale * factor));
    scale = next;

    tx = px - wx * scale;
    ty = py - wy * scale;
    apply();
  }

  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.08 : 1 / 1.08;
    zoomBy(factor, ev.clientX, ev.clientY);
  }, { passive: false });

  svg.addEventListener("pointerdown", (ev) => {
    // don't pan when clicking a node
    if (ev.target && ev.target.closest && ev.target.closest("g.dagNode")) return;
    panning = true;
    panStart = { x: ev.clientX, y: ev.clientY, tx, ty };
    svg.setPointerCapture(ev.pointerId);
  });

  svg.addEventListener("pointermove", (ev) => {
    if (!panning || !panStart) return;
    tx = panStart.tx + (ev.clientX - panStart.x);
    ty = panStart.ty + (ev.clientY - panStart.y);
    apply();
  });

  svg.addEventListener("pointerup", (ev) => {
    panning = false;
    panStart = null;
    try { svg.releasePointerCapture(ev.pointerId); } catch {}
  });

  // initial mount
  host.appendChild(svg);
  queueMicrotask(() => fit());

  if (showMini && miniHost) {
    miniHost.textContent = "";
    miniSvg = svgEl("svg", { class: "miniSvg" });
    // miniSvg.setAttribute("preserveAspectRatio", "none");

    // viewBox = graph bounds
    const b = graph.bounds || {};
    miniSvg.setAttribute("viewBox", `${b.minx || 0} ${b.miny || 0} ${b.width || 1000} ${b.height || 600}`);

    const miniEdges = svgEl("g");
    const miniNodes = svgEl("g");
    miniSvg.append(miniEdges, miniNodes);

    for (const e of edgeEls) {
      const a = byId.get(e.from), c = byId.get(e.to);
      if (!a || !c) continue;
      const d = (dir === "TB") ? pathForEdgeTB(a, c) : pathForEdgeLR(a, c);
      miniEdges.appendChild(svgEl("path", { d, class: "miniEdge" }));
    }

    for (const n of nodes) {
      miniNodes.appendChild(svgEl("rect", {
        x: n.x, y: n.y, width: n.w, height: n.h, rx: 6, ry: 6,
        class: "miniNode"
      }));
    }

    miniView = svgEl("rect", { class: "miniView", x: 0, y: 0, width: 10, height: 10, rx: 4, ry: 4 });
    miniView.style.cursor = "grab";
    miniSvg.appendChild(miniView);

    // ensure it is visible + hittable
    miniView.setAttribute("fill", "#000");
    miniView.setAttribute("fill-opacity", "0.12");
    miniView.setAttribute("stroke", "#000");
    miniView.setAttribute("stroke-opacity", "0.45");
    miniView.setAttribute("stroke-width", "1");

    // critical: make sure it can receive pointer events even if CSS disables it
    miniView.setAttribute("pointer-events", "all");
    miniView.style.pointerEvents = "all";

    // helpful on touch devices
    miniSvg.style.touchAction = "none";

    function miniClientToGraph(ev) {
      const rect = miniSvg.getBoundingClientRect();
      const px = (ev.clientX - rect.left) / rect.width;
      const py = (ev.clientY - rect.top) / rect.height;
      const vb = miniSvg.viewBox.baseVal;
      return { x: vb.x + px * vb.width, y: vb.y + py * vb.height };
    }
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

    let miniDrag = null;
    let miniSuppressClick = false;

    function miniGetViewRect() {
      const x = parseFloat(miniView.getAttribute("x")) || 0;
      const y = parseFloat(miniView.getAttribute("y")) || 0;
      const w = parseFloat(miniView.getAttribute("width")) || 0;
      const h = parseFloat(miniView.getAttribute("height")) || 0;
      return { x, y, w, h };
    }

    miniSvg.addEventListener("pointerdown", (ev) => {
      if (ev.button != null && ev.button !== 0) return;

      const p0 = miniClientToGraph(ev);
      const r = miniGetViewRect();

      // Only start dragging if you pressed INSIDE the viewport rectangle
      const inside =
        p0.x >= r.x && p0.x <= r.x + r.w &&
        p0.y >= r.y && p0.y <= r.y + r.h;

      if (!inside) return;

      ev.preventDefault();
      ev.stopPropagation();

      miniSvg.setPointerCapture(ev.pointerId);
      miniView.style.cursor = "grabbing";

      miniDrag = {
        id: ev.pointerId,
        p0,
        vx0: (-tx) / scale,
        vy0: (-ty) / scale,
        moved: false,
      };
    });

    miniSvg.addEventListener("pointermove", (ev) => {
      if (!miniDrag || ev.pointerId !== miniDrag.id) return;
      ev.preventDefault();

      const p = miniClientToGraph(ev);
      const dx = p.x - miniDrag.p0.x;
      const dy = p.y - miniDrag.p0.y;

      // visible size (graph coords)
      const sr = svg.getBoundingClientRect();
      const vw = sr.width / scale;
      const vh = sr.height / scale;

      const vb = miniSvg.viewBox.baseVal;
      const boundX = vb.x + vb.width - vw;
      const boundY = vb.y + vb.height - vh;

      const vx = clamp(miniDrag.vx0 + dx, Math.min(vb.x, boundX), Math.max(vb.x, boundX));
      const vy = clamp(miniDrag.vy0 + dy, Math.min(vb.y, boundY), Math.max(vb.y, boundY));

      tx = -vx * scale;
      ty = -vy * scale;

      if (Math.abs(dx) + Math.abs(dy) > 0.5) miniDrag.moved = true;
      apply();
    });

    function endMiniDrag(ev) {
      if (!miniDrag || ev.pointerId !== miniDrag.id) return;
      ev.preventDefault();

      miniSuppressClick = miniDrag.moved;
      miniDrag = null;

      try { miniSvg.releasePointerCapture(ev.pointerId); } catch {}
      miniView.style.cursor = "grab";
    }

    miniSvg.addEventListener("pointerup", endMiniDrag);
    miniSvg.addEventListener("pointercancel", endMiniDrag);

    miniSvg.addEventListener("click", (ev) => {
      if (miniSuppressClick) { miniSuppressClick = false; return; }
      const rect = miniSvg.getBoundingClientRect();
      const px = (ev.clientX - rect.left) / rect.width;
      const py = (ev.clientY - rect.top) / rect.height;

      const vb = miniSvg.viewBox.baseVal;
      const gx = vb.x + px * vb.width;
      const gy = vb.y + py * vb.height;

      // center clicked point
      const sr = svg.getBoundingClientRect();
      tx = sr.width / 2 - gx * scale;
      ty = sr.height / 2 - gy * scale;
      apply();
    });

    miniHost.appendChild(miniSvg);
    updateMini();
  }

  function centerOn(id, zoom = 1.25) {
    const n = byId.get(id);
    if (!n) return;

    const sr = svg.getBoundingClientRect();
    const cx = n.x + n.w / 2;
    const cy = n.y + n.h / 2;

    scale = Math.max(0.1, Math.min(3.0, zoom));
    tx = sr.width / 2 - cx * scale;
    ty = sr.height / 2 - cy * scale;
    apply();
  }

  return {
    fit, reset,
    zoomIn: () => zoomBy(1.12, svg.getBoundingClientRect().left + 10, svg.getBoundingClientRect().top + 10),
    zoomOut: () => zoomBy(1 / 1.12, svg.getBoundingClientRect().left + 10, svg.getBoundingClientRect().top + 10),
    svg,
    focus: (id, { zoom = 1.25, pin = true } = {}) => { centerOn(id, zoom); if (pin) setPinned(id); },
    refresh: () => applyHighlight(),
    setPinned,
  };
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(String(text ?? "")); return true; }
  catch { return false; }
}

async function loadManifest() {
  const res = await fetch(MANIFEST_URL, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to load manifest: ${res.status}`);
  return await res.json();
}

async function main() {
  const app = document.getElementById("app");
  app.textContent = "Loading…";

  const manifest = await loadManifest();
  const state = {
    manifest,
    filter: "",
    byModel: byName(manifest.models || [], (m) => m.name),
    bySource: byName(manifest.sources || [], (s) => `${s.source_name}.${s.table_name}`),
  };
  state.sidebarMatches = { models: 0, sources: 0 };
  state.graphUI = {
    mode: "both",   // "up" | "down" | "both" | "off"
    depth: 2,
    pinned: "",     // node id like "m:orders"
  };
  state.graphUI.dir = state.manifest.dag.graph.direction || "LR";

  const ui = {
    app: document.getElementById("app"),
    sidebarHost: null,
    mainHost: null,
    paletteOverlay: null,
    paletteInput: null,
    paletteList: null,
  };
  state.ui = ui;

  // Mount shell once
  const shell = el("div", { class: "shell" },
    (ui.sidebarHost = el("div")),
    (ui.mainHost = el("div", { class: "main" }))
  );
  ui.app.replaceChildren(shell);

  const projKey = (manifest.project?.name || "fft")
    .toLowerCase()
    .replace(/\s+/g, "_")
    .replace(/[^a-z0-9_]+/g, "");

  const STORE = {
    filter: `fft_docs:${projKey}:sidebar_filter`,
    collapsed: `fft_docs:${projKey}:sidebar_collapsed`,
    lastHash: `fft_docs:${projKey}:last_hash`,
    paletteQuery: `fft_docs:${projKey}:palette_query`,
  };
  STORE.modelTab = `fft_docs:${projKey}:model_tab_default`;
  STORE.modelCodeView = `fft_docs:${projKey}:model_code_view_default`;

  state.modelTabDefault = safeGet(STORE.modelTab) || "overview";
  state.modelCodeViewDefault = safeGet(STORE.modelCodeView) || "";
  state.STORE = STORE;

  // Allow replaceHashQuery() (global) to keep lastHash in sync even when we use history.replaceState.
  window.__fftLastHashKey = STORE.lastHash;


  // Persisted UI state
  state.filter = safeGet(STORE.filter) ?? "";
  state.sidebarCollapsed = safeGetJSON(STORE.collapsed, {
    models: false,
    sources: false,
    macros: false,
  });

  // Restore last route only if user is on the default route
  const last = safeGet(STORE.lastHash);
  if ((!location.hash || location.hash === "#/" || location.hash === "#") && last) {
    location.hash = last;
  }

  // Initialize model facets from URL (shareable filters)
  state.modelFacets = currentModelFacets();

  toastOnce({
    key: `fft_docs_search_toast_seen:${projKey}`,
    title: "Quick search",
    body: "Press / (or Ctrl+K) to search models, sources, and columns.",
    actionLabel: "Open search",
    onAction: () => openPalette(""),
  });

  // Build a flat searchable index: models, sources, columns
  const searchIndex = [];

  for (const m of (manifest.models || [])) {
    const descTxt = (m.description_text != null && m.description_text !== "")
      ? m.description_text
      : stripHtml(m.description_html);

    const baseHay = [
      `model ${m.name}`,
      m.relation || "",
      descTxt || "",
      m.path || "",
      m.kind || "",
      m.materialized || "",
    ].join(" | ");

    searchIndex.push({
      kind: "model",
      title: m.name,
      subtitle: m.relation || (m.path || ""),
      route: `#/model/${escapeHashPart(m.name)}`,
      haystack: baseHay,
      graphId: `m:${m.name}`,
    });

    // Columns as their own results (so you can jump directly)
    for (const c of (m.columns || [])) {
      const cDesc = (c.description_text != null && c.description_text !== "")
        ? c.description_text
        : stripHtml(c.description_html);

      const colHay = [
        `column ${m.name}.${c.name}`,
        c.name,
        c.dtype || "",
        cDesc || "",
        m.name,
        m.relation || "",
      ].join(" | ");

      searchIndex.push({
        kind: "column",
        model: m.name,
        column: c.name,
        relation: m.relation || "",
        dtype: c.dtype || "",
        descText: cDesc || "",
        title: `${m.name}.${c.name}`,
        subtitle: `${m.relation || ""}${c.dtype ? " • " + c.dtype : ""}`,
        route: `#/model/${escapeHashPart(m.name)}?tab=columns&col=${escapeHashPart(c.name)}`,
        haystack: colHay,
        graphId: `m:${m.name}`, // focus model node
      });
    }
  }

  for (const s of (manifest.sources || [])) {
    const key = `${s.source_name}.${s.table_name}`;
    const descTxt = (s.description_text != null && s.description_text !== "")
      ? s.description_text
      : stripHtml(s.description_html);

    const hay = [
      `source ${key}`,
      s.relation || "",
      descTxt || "",
      s.loaded_at_field || "",
      (s.consumers || []).join(" "),
    ].join(" | ");

    searchIndex.push({
      kind: "source",
      title: key,
      subtitle: s.relation || "",
      route: `#/source/${escapeHashPart(s.source_name)}/${escapeHashPart(s.table_name)}`,
      haystack: hay,
      graphId: `s:${s.source_name}.${s.table_name}`,
    });
  }

  state.search = {
    open: false,
    query: "",
    selected: 0,
    results: [],
  };

  function runSearch(q) {
    const query = (q || "").trim();
    if (!query) {
      // show a helpful default: top models + sources (no scoring)
      const defaults = [];
      for (const it of searchIndex) {
        if (it.kind === "model" || it.kind === "source") defaults.push({ ...it, score: 0 });
        if (defaults.length >= 30) break;
      }
      state.search.results = defaults;
      state.search.selected = 0;
      return;
    }

    const scored = [];
    for (const it of searchIndex) {
      const score = fuzzyScore(query, it.haystack);
      if (score >= 0) scored.push({ ...it, score });
    }
    state.search.results = topN(scored, 80);
    state.search.selected = 0;
  }

  function renderPaletteResults() {
    const results = state.search.results || [];
    const sel = Math.max(0, Math.min(state.search.selected || 0, results.length - 1));

    const q = (state.search.query || "").trim();
    const subFor = (r) => {
      if (r.kind === "column") {
        const parts = [
          "COLUMN",
          r.model || "",
          r.relation ? `• ${r.relation}` : "",
          r.dtype ? `• ${r.dtype}` : "",
        ].filter(Boolean).join(" ");
        const snip = makeSnippet(r.descText || "", q, 90);
        return snip ? `${parts} • ${snip}` : parts;
      }
      if (r.kind === "model") {
        const snip = makeSnippet((r.descText || ""), q, 90);
        return snip ? `MODEL • ${r.subtitle || ""} • ${snip}` : `MODEL • ${r.subtitle || ""}`;
      }
      if (r.kind === "source") {
        const snip = makeSnippet((r.descText || ""), q, 90);
        return snip ? `SOURCE • ${r.subtitle || ""} • ${snip}` : `SOURCE • ${r.subtitle || ""}`;
      }
      return `${(r.kind || "").toUpperCase()} • ${r.subtitle || ""}`;
    };

    const rightFor = (r) =>
      (r.kind === "column" && r.dtype)
      ? el("span", { class: "pill" }, r.dtype)
      : el("div", { class: "kbd" }, "↵");

    state.ui.paletteList.replaceChildren(
      ...(results.length
        ? results.map((r, idx) =>
            el("div", {
              class: `result ${idx === sel ? "sel" : ""}`,
              onclick: () => {
                closePalette();
                location.hash = routeWithFacets(r.route);
              },
            },
              el("div", { class: "resultMain" },
                el("div", { class: "resultTitle" }, r.title),
                el("div", { class: "resultSub" }, subFor(r))
              ),
              rightFor(r)
            )
          )
        : [el("div", { class: "result" },
            el("div", { class: "resultMain" },
              el("div", { class: "resultTitle" }, "No results"),
              el("div", { class: "resultSub" }, "Try a different query.")
            )
          )]
      )
    );
  }

  function buildPalette() {
    if (state.ui.paletteOverlay) return;

    state.ui.paletteList = el("div", { class: "paletteList" });
    state.ui.paletteInput = el("input", {
      id: "globalSearch",
      class: "paletteInput",
      type: "search",
      placeholder: "Search models, sources, columns…",
      value: state.search.query || "",
      oninput: (e) => {
        state.search.query = e.target.value || "";
        safeSet(STORE.paletteQuery, state.search.query);
        runSearch(state.search.query);
        renderPaletteResults();
      },
      onkeydown: (e) => {
        // Key handling while focused in the input
        if (e.key === "Escape") {
          e.preventDefault();
          if (state.search.query) {
            state.search.query = "";
            state.ui.paletteInput.value = "";
            runSearch("");
            renderPaletteResults();
          } else {
            closePalette();
          }
          return;
        }
        if (e.key === "ArrowDown") {
          e.preventDefault();
          const n = (state.search.results || []).length;
          if (n) state.search.selected = (state.search.selected + 1) % n;
          renderPaletteResults();
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          const n = (state.search.results || []).length;
          if (n) state.search.selected = (state.search.selected - 1 + n) % n;
          renderPaletteResults();
          return;
        }
        if (e.key === "Enter") {
          e.preventDefault();
          const results = state.search.results || [];
          const idx = Math.max(0, Math.min(state.search.selected || 0, results.length - 1));
          const hit = results[idx];
          if (!hit) return;

          // Ctrl/Cmd+Enter => focus in DAG
          if (e.ctrlKey || e.metaKey) {
            closePalette();

            const gid = hit.graphId;
            if (!gid) return;

            // If not on home, go home with focus param
            const r = parseRoute();
            if (r.route !== "home") {
              location.hash = routeWithFacets(`#/?focus=${encodeURIComponent(gid)}`);
              return;
            }

            // Already on home: focus immediately
            state._graphCtl?.focus?.(gid, { zoom: 1.25, pin: true });
            return;
          }

          // Normal Enter => navigate
          closePalette();
          location.hash = routeWithFacets(hit.route);
        }
      }
    });

    const overlay = el("div", {
      class: "overlay",
      onclick: (e) => {
        if (e.target.classList.contains("overlay")) closePalette();
      }
    },
      el("div", { class: "palette" },
        el("div", { class: "paletteHead" },
          state.ui.paletteInput,
          el("div", { class: "paletteHint" },
            el("span", { class: "kbd" }, "Esc"), " close ",
            el("span", { class: "kbd" }, "↑↓"), " select ",
            el("span", { class: "kbd" }, "Enter"), " go"
          )
        ),
        state.ui.paletteList
      )
    );

    overlay.style.display = "none";
    state.ui.paletteOverlay = overlay;
    document.body.appendChild(overlay);
  }

  function openPalette(prefill = "") {
    buildPalette();

    const remembered = safeGet(STORE.paletteQuery) ?? "";
    const initial = prefill != null && prefill !== "" ? prefill : remembered;

    state.search.open = true;
    state.search.query = initial;
    state.search.selected = 0;

    state.ui.paletteOverlay.style.display = "flex";
    state.ui.paletteInput.value = state.search.query;

    runSearch(state.search.query);
    renderPaletteResults();

    // focus once, no re-render
    queueMicrotask(() => {
      state.ui.paletteInput.focus();
      state.ui.paletteInput.select();
    });
  }

  function closePalette() {
    if (!state.ui.paletteOverlay) return;
    state.search.open = false;
    state.ui.paletteOverlay.style.display = "none";
  }

  // Sidebar UI handles (persistent DOM nodes)
  ui.sidebar = {
    root: null,
    input: null,

    // facet UI (near sidebar search)
    facetBox: null,
    kindSqlBtn: null,
    kindPyBtn: null,
    matSelect: null,
    pathInput: null,
    pathDatalist: null,
    clearFacetsBtn: null,
    brandLink: null,
    overviewLink: null,

    modelsTitle: null,
    sourcesTitle: null,
    modelsList: null,
    sourcesList: null,
  };
  ui.sidebar.macrosList = null;
  ui.sidebar.modelsSection = null;
  ui.sidebar.sourcesSection = null;
  ui.sidebar.macrosSection = null;

  function sectionHeader(titleNode, key, labelWhenOpen) {
    const btn = el("button", {
      class: "btn",
      style: "width:100%; display:flex; justify-content:space-between; align-items:center; padding:8px 10px;",
      onclick: () => {
        state.sidebarCollapsed[key] = !state.sidebarCollapsed[key];
        safeSetJSON(STORE.collapsed, state.sidebarCollapsed);
        applySidebarCollapse(); // show/hide without rebuilding
      }
    },
      el("span", {}, labelWhenOpen),
      el("span", { class: "kbd" }, state.sidebarCollapsed[key] ? "+" : "–")
    );
    // store reference for label updates
    titleNode.replaceChildren(btn);
    return btn;
  }

  function buildSidebar() {
  if (ui.sidebar.root) return;

  ui.sidebar.input = el("input", {
    class: "search",
    type: "search",
    placeholder: "Filter sidebar… (press /)",
    value: state.filter || "",
    oninput: (e) => {
      state.filter = e.target.value || "";
      safeSet(STORE.filter, state.filter);
      updateSidebarLists();
    },
    onkeydown: (e) => {
      if (e.key !== "Enter") return;

      const q = (state.filter || "").trim();
      const total = (state.sidebarMatches.models || 0) + (state.sidebarMatches.sources || 0);

      if (!q) {
        e.preventDefault();
        openPalette("");
        return;
      }

      if (total === 0) {
        e.preventDefault();
        openPalette(q);
        return;
      }
    },
  });

  // --- Facets live next to the sidebar search (not under Models) ---
  const applyFacetsToUrl = () => {
    replaceHashQuery((q) => writeModelFacetsToQuery(q, state.modelFacets));
    // keep state in sync (routeWithFacets reads from URL)
    state.modelFacets = currentModelFacets();
  };

  const debouncedPath = debounce((val) => {
    state.modelFacets.pathPrefix = (val || "").trim();
    applyFacetsToUrl();
    updateSidebarLists();
  }, 180);

  ui.sidebar.kindSqlBtn = el("button", {
    class: "facetChip",
    type: "button",
    onclick: () => {
      const kinds = new Set(state.modelFacets.kinds || ["sql", "python"]);
      if (kinds.has("sql")) kinds.delete("sql"); else kinds.add("sql");
      if (kinds.size === 0) { kinds.add("sql"); kinds.add("python"); } // avoid empty selection
      state.modelFacets.kinds = Array.from(kinds);

      applyFacetsToUrl();
      updateSidebarLists();
    }
  }, "SQL");

  ui.sidebar.kindPyBtn = el("button", {
    class: "facetChip",
    type: "button",
    onclick: () => {
      const kinds = new Set(state.modelFacets.kinds || ["sql", "python"]);
      if (kinds.has("python")) kinds.delete("python"); else kinds.add("python");
      if (kinds.size === 0) { kinds.add("sql"); kinds.add("python"); }
      state.modelFacets.kinds = Array.from(kinds);

      applyFacetsToUrl();
      updateSidebarLists();
    }
  }, "Python");

  ui.sidebar.matSelect = el("select", {
    class: "facetSelect",
    onchange: (e) => {
      state.modelFacets.materialized = normalizeMaterialized(e.target.value || "");
      applyFacetsToUrl();
      updateSidebarLists();
    }
  });

  ui.sidebar.pathDatalist = el("datalist", { id: "modelPathPrefixes" });
  ui.sidebar.pathInput = el("input", {
    class: "facetInput",
    type: "search",
    placeholder: "Path prefix…",
    list: "modelPathPrefixes",
    value: state.modelFacets.pathPrefix || "",
    oninput: (e) => debouncedPath(e.target.value || ""),
    onkeydown: (e) => {
      if (e.key !== "Enter") return;
      debouncedPath.cancel();
      state.modelFacets.pathPrefix = (e.target.value || "").trim();
      applyFacetsToUrl();
      updateSidebarLists();
    }
  });

  ui.sidebar.clearFacetsBtn = el("button", {
    class: "facetClear",
    type: "button",
    onclick: () => {
      debouncedPath.cancel();
      state.modelFacets = { kinds: ["sql", "python"], materialized: "", pathPrefix: "", tags: [], groupBy: "" };
      ui.sidebar.pathInput.value = "";
      applyFacetsToUrl();
      updateSidebarLists();
    }
  }, "Clear");

  ui.sidebar.groupSelect = el("select", { class:"facetSelect", onchange:(e)=>{
    state.modelFacets.groupBy = (e.target.value === "owner" || e.target.value === "domain") ? e.target.value : "";
    replaceHashQuery((q)=> writeModelFacetsToQuery(q, state.modelFacets));
    state.modelFacets = currentModelFacets();
    updateSidebarLists();
  }});
  ui.sidebar.tagBox = el("div", { class:"facetChips facetChipsWrap" });
  ui.sidebar.tagInput = el("input", { class:"facetInput", placeholder:"Add tag…", list:"modelTags" });
  ui.sidebar.tagDatalist = el("datalist", { id:"modelTags" });
  ui.sidebar.groupSelect.replaceChildren(
    el("option", { value:"" }, "No grouping"),
    el("option", { value:"owner" }, "Group: owner"),
    el("option", { value:"domain" }, "Group: domain"),
  );
  ui.sidebar.tagInput.onkeydown = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const t = (ui.sidebar.tagInput.value || "").trim();
      if (!t) return;
      const tags = new Set(state.modelFacets.tags || []);
      tags.add(t);
      state.modelFacets.tags = Array.from(tags);
      ui.sidebar.tagInput.value = "";
      replaceHashQuery((q)=> writeModelFacetsToQuery(q, state.modelFacets));
      state.modelFacets = currentModelFacets();
      updateSidebarLists();
    }
    if (e.key === "Escape") {
      e.preventDefault();
      ui.sidebar.tagInput.value = "";
    }
  };
  ui.sidebar.facetBox = el("div", { class:"facetBox" },
    el("div", { class:"facetRow" },
      el("div", { class:"facetChips" }, ui.sidebar.kindSqlBtn, ui.sidebar.kindPyBtn),
      ui.sidebar.matSelect,
      ui.sidebar.groupSelect
    ),
    el("div", { class:"facetRow" },
      ui.sidebar.pathInput,
      ui.sidebar.clearFacetsBtn
    ),
    el("div", { class:"facetRow" },
      el("div", { class:"facetLabel" }, "Tags"),
      ui.sidebar.tagInput
    ),
    ui.sidebar.tagBox,
    ui.sidebar.tagDatalist,
    ui.sidebar.pathDatalist
  );

  const overviewSection = el("div", { class: "section" },
    el("div", {},
      (ui.sidebar.overviewLink = el("a", {
        href: routeWithFacets("#/"),
        onclick: (e) => { e.preventDefault(); location.hash = routeWithFacets("#/"); },
        class: "itemLink",
        style: "display:flex; align-items:center; justify-content:space-between; padding:8px 10px; border:1px solid var(--border); border-radius:12px; text-decoration:none; color:inherit;"
      },
        el("span", {}, "Overview (DAG)"),
        el("span", { class: "pill" }, "Home")
      ))
    )
  );

  ui.sidebar.modelsTitle = el("div");
  ui.sidebar.sourcesTitle = el("div");
  ui.sidebar.macrosTitle = el("div");

  ui.sidebar.modelsList = el("ul", { class: "list" });
  ui.sidebar.sourcesList = el("ul", { class: "list" });
  ui.sidebar.macrosList = el("ul", { class: "list" });

  ui.sidebar.modelsSection = el("div", { class: "section" }, ui.sidebar.modelsTitle, ui.sidebar.modelsList);
  ui.sidebar.sourcesSection = el("div", { class: "section" }, ui.sidebar.sourcesTitle, ui.sidebar.sourcesList);
  ui.sidebar.macrosSection = el("div", { class: "section" }, ui.sidebar.macrosTitle, ui.sidebar.macrosList);

  ui.sidebar.projectTitle = el("div");
  const statRow = (k, v) =>
    el("div", { class: "kvRow" },
      el("span", { class: "k" }, k),
      el("span", { class: "v" }, v)
    );

  ui.sidebar.projectBody = el("div", { class: "kvRows" },
    statRow("Models", String((state.manifest.models || []).length)),
    statRow("Sources", String((state.manifest.sources || []).length)),
    statRow("Macros", String((state.manifest.macros || []).length)),
    statRow("Schema", state.manifest.project?.with_schema ? "enabled" : "disabled"),
    statRow("Generated", state.manifest.project?.generated_at || "—"),
  );

  ui.sidebar.projectSection = el("div", { class: "section" },
    ui.sidebar.projectTitle,
    ui.sidebar.projectBody
  );

  ui.sidebar.root = el(
    "div",
    { class: "sidebar" },
    el(
      "div",
      { class: "brand" },
      (ui.sidebar.brandLink = el("a", {
        href: routeWithFacets("#/"),
        style: "color:inherit; text-decoration:none;",
        onclick: (e) => { e.preventDefault(); location.hash = routeWithFacets("#/"); }
      }, el("h1", {}, state.manifest.project?.name || "Docs"))),
      el("span", { class: "badge", title: `Generated: ${state.manifest.project?.generated_at || ""}` }, "SPA")
    ),
    el(
      "div",
      { class: "searchWrap" },
      ui.sidebar.input,
      el("span", { class: "searchKbd kbd" }, "/")
    ),
    ui.sidebar.facetBox,
    el("div", { class: "searchTip" }, "Tip: Press / (or Ctrl+K) to search everything (models, sources, columns)."),
    overviewSection,
    ui.sidebar.projectSection,
    ui.sidebar.modelsSection,
    ui.sidebar.sourcesSection,
    ui.sidebar.macrosSection,
  );

  ui.sidebarHost.replaceChildren(ui.sidebar.root);

  // Turn titles into toggle headers
  sectionHeader(ui.sidebar.modelsTitle, "models", "Models");
  sectionHeader(ui.sidebar.sourcesTitle, "sources", "Sources");
  sectionHeader(ui.sidebar.macrosTitle, "macros", "Macros");
  sectionHeader(ui.sidebar.projectTitle, "project", "Project");
}

  function applySidebarCollapse() {
    const c = state.sidebarCollapsed || {};
    ui.sidebar.modelsList.style.display = c.models ? "none" : "";
    ui.sidebar.sourcesList.style.display = c.sources ? "none" : "";
    ui.sidebar.macrosList.style.display = c.macros ? "none" : "";
    ui.sidebar.projectBody.style.display = c.project ? "none" : "";
  }

  function updateSidebarLists() {
    // Keep facets in sync with URL in case of back/forward or manual edits
    state.modelFacets = currentModelFacets();
    ui.sidebar.groupSelect.value = state.modelFacets.groupBy || "";

    const q = (state.filter || "").trim().toLowerCase();
    const models = state.manifest.models || [];
    const sources = state.manifest.sources || [];

    const modelsAfterText = q
      ? models.filter(m =>
          (m.name || "").toLowerCase().includes(q) ||
          (m.relation || "").toLowerCase().includes(q) ||
          (m.description_short || "").toLowerCase().includes(q)
        )
      : models;

    const sourcesAfterText = q
      ? sources.filter(s =>
          (`${s.source_name}.${s.table_name}`).toLowerCase().includes(q) ||
          (s.relation || "").toLowerCase().includes(q)
        )
      : sources;

    // Apply model facets (kinds/materialized/pathPrefix) on top of text filtering
    const filteredModels = filterModelsWithFacets(modelsAfterText, state.modelFacets);
    const filteredSources = sourcesAfterText;

    state.sidebarMatches.models = filteredModels.length;
    state.sidebarMatches.sources = filteredSources.length;

    // ---- Facet UI: counts + selected state ----
    const facets = state.modelFacets || { kinds: ["sql", "python"], materialized: "", pathPrefix: "" };

    // counts for kind based on other facets (materialized + pathPrefix)
    const forKindCounts = filterModelsWithFacets(modelsAfterText, { ...facets, kinds: ["sql", "python"] });
    let sqlCount = 0, pyCount = 0;
    for (const m of forKindCounts) {
      if (normalizeModelKind(m.kind) === "python") pyCount++; else sqlCount++;
    }

    // counts for materialized based on other facets (kind + pathPrefix)
    const forMatCounts = filterModelsWithFacets(modelsAfterText, { ...facets, materialized: "" });
    const matCounts = new Map();
    let unknownCount = 0;
    for (const m of forMatCounts) {
      const mm = normalizeMaterialized(m.materialized || "");
      if (!mm) unknownCount++;
      else matCounts.set(mm, (matCounts.get(mm) || 0) + 1);
    }
    const matsSorted = Array.from(matCounts.entries()).sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]));

    // path prefix suggestions based on other facets (kind + materialized)
    const forPathCounts = filterModelsWithFacets(modelsAfterText, { ...facets, pathPrefix: "" });
    const prefixCounts = new Map();
    for (const m of forPathCounts) {
      const p = stripModelsPrefix(m.path || "");
      if (!p) continue;
      const parts = p.split("/").filter(Boolean);

      // suggest first segment and first two segments
      for (const depth of [1, 2]) {
        if (parts.length >= depth) {
          const pref = parts.slice(0, depth).join("/") + "/";
          prefixCounts.set(pref, (prefixCounts.get(pref) || 0) + 1);
        }
      }

      // also include full directory if available (without file name)
      if (parts.length > 1) {
        const fullDir = parts.slice(0, parts.length - 1).join("/") + "/";
        prefixCounts.set(fullDir, (prefixCounts.get(fullDir) || 0) + 1);
      }
    }
    const prefixesSorted = Array.from(prefixCounts.entries())
      .sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]))
      .slice(0, 40);

    const kindsSet = new Set((facets.kinds || ["sql", "python"]).map(k => (k || "").toLowerCase()));
    ui.sidebar.kindSqlBtn.textContent = `SQL (${sqlCount})`;
    ui.sidebar.kindPyBtn.textContent = `Python (${pyCount})`;
    ui.sidebar.kindSqlBtn.classList.toggle("active", kindsSet.has("sql"));
    ui.sidebar.kindPyBtn.classList.toggle("active", kindsSet.has("python"));

    // Update materialized select options (keep selection)
    const currentMat = normalizeMaterialized(facets.materialized || "");
    ui.sidebar.matSelect.replaceChildren(
      el("option", { value: "" }, `Any materialization (${forMatCounts.length})`),
      ...(unknownCount ? [el("option", { value: MAT_UNKNOWN }, `(unknown) (${unknownCount})`)] : []),
      ...matsSorted.map(([mm, n]) => el("option", { value: mm }, `${mm} (${n})`))
    );
    ui.sidebar.matSelect.value = currentMat;

    // Update datalist suggestions and keep input in sync if URL changed
    ui.sidebar.pathDatalist.replaceChildren(
      ...prefixesSorted.map(([p, n]) => el("option", { value: p }, `${p} (${n})`))
    );
    if ((ui.sidebar.pathInput.value || "") !== (facets.pathPrefix || "")) {
      ui.sidebar.pathInput.value = facets.pathPrefix || "";
    }

    const activeN = facetsActiveCount(facets);
    ui.sidebar.clearFacetsBtn.textContent = activeN ? `Clear (${activeN})` : "Clear";
    ui.sidebar.clearFacetsBtn.disabled = activeN === 0;

    const baseForTags = filterModelsWithFacets(modelsAfterText, { ...state.modelFacets, tags: [] });
    const tagCounts = new Map();
    for (const m of baseForTags) {
      for (const t of (Array.isArray(m.tags) ? m.tags : [])) {
        const key = String(t).trim();
        if (!key) continue;
        tagCounts.set(key, (tagCounts.get(key) || 0) + 1);
      }
    }
    const topTags = Array.from(tagCounts.entries())
      .sort((a,b)=> (b[1]-a[1]) || a[0].localeCompare(b[0]))
      .slice(0, 30);

    // Keep "home" hrefs up-to-date for copy/open-in-new-tab
    if (ui.sidebar.brandLink) ui.sidebar.brandLink.href = routeWithFacets("#/");
    if (ui.sidebar.overviewLink) ui.sidebar.overviewLink.href = routeWithFacets("#/");

    // ---- Sidebar lists ----
    ui.sidebar.modelsTitle.textContent = `Models (${filteredModels.length})`;
    ui.sidebar.sourcesTitle.textContent = `Sources (${filteredSources.length})`;

    ui.sidebar.tagDatalist.replaceChildren(
      ...topTags.map(([t,n]) => el("option", { value:t }, `${t} (${n})`))
    );
    
    const activeTags = new Set((state.modelFacets.tags || []).map(x => x.toLowerCase()));

    ui.sidebar.tagBox.replaceChildren(
      ...topTags.map(([t,n]) => {
        const on = activeTags.has(t.toLowerCase());
        const b = el("button", {
          class: `facetChip ${on ? "active" : ""}`,
          type:"button",
          onclick: () => {
            const tags = new Set(state.modelFacets.tags || []);
            if (on) {
              // remove case-insensitively
              for (const x of Array.from(tags)) if (String(x).toLowerCase() === t.toLowerCase()) tags.delete(x);
            } else {
              tags.add(t);
            }
            state.modelFacets.tags = Array.from(tags);
            replaceHashQuery((q)=> writeModelFacetsToQuery(q, state.modelFacets));
            state.modelFacets = currentModelFacets();
            updateSidebarLists();
          }
        }, `${t} (${n})`);
        return b;
      })
    );

    function modelGroupKey(m) {
      if (state.modelFacets.groupBy === "owner") {
        const owners = Array.isArray(m.owners) ? m.owners : [];
        return owners.length ? String(owners[0]) : "(unowned)";
      }
      if (state.modelFacets.groupBy === "domain") {
        return (m.domain || stripModelsPrefix(m.path || "").split("/")[0] || "(no domain)");
      }
      return "";
    }

    function renderModelLi(m) {
      const href = routeWithFacets(`#/model/${escapeHashPart(m.name)}`);
      return el("li", { class:"item" },
        el("a", {
          href,
          onclick:(e)=>{ e.preventDefault(); location.hash = href; },
          title: [m.description_short || "", m.path ? `(${m.path})` : ""].filter(Boolean).join(" ") || m.name,
        },
          el("span", {}, m.name),
          pillForKind(m.kind === "python" ? "python" : "sql")
        )
      );
    }

    if (!state.modelFacets.groupBy) {
      ui.sidebar.modelsList.replaceChildren(...filteredModels.map(renderModelLi));
    } else {
      const groups = new Map();
      for (const m of filteredModels) {
        const k = modelGroupKey(m);
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(m);
      }
      const keys = Array.from(groups.keys()).sort((a,b)=> a.localeCompare(b));

      const children = [];
      for (const k of keys) {
        const items = groups.get(k);
        items.sort((a,b)=> (a.name||"").localeCompare(b.name||""));

        children.push(el("li", { class:"groupHeader" },
          el("div", { class:"groupHeaderRow" },
            el("span", {}, k),
            el("span", { class:"pill" }, String(items.length))
          )
        ));
        for (const m of items) children.push(renderModelLi(m));
      }
      ui.sidebar.modelsList.replaceChildren(...children);
    }

    ui.sidebar.sourcesList.replaceChildren(
      ...filteredSources.map(s => {
        const key = `${s.source_name}.${s.table_name}`;
        const href = routeWithFacets(`#/source/${escapeHashPart(s.source_name)}/${escapeHashPart(s.table_name)}`);
        return el("li", { class: "item" },
          el("a", {
            href,
            onclick: (e) => { e.preventDefault(); location.hash = href; },
            title: s.relation || key,
          },
            el("span", {}, key),
            el("span", { class: "pill" }, (s.consumers || []).length ? `${s.consumers.length}` : "–")
          )
        );
      })
    );

    const macros = state.manifest.macros || [];
    ui.sidebar.macrosTitle.textContent = `Macros (${macros.length})`;
    ui.sidebar.macrosList.replaceChildren(
      ...macros.map(m =>
        el("li", { class: "item" },
          el("a", {
            href: routeWithFacets(`#/macro/${escapeHashPart(m.name)}`),
            onclick: (e) => { e.preventDefault(); location.hash = routeWithFacets(`#/macro/${escapeHashPart(m.name)}`); },
            title: m.path || m.name,
          },
            el("span", {}, m.name),
            el("span", { class: "pill" }, m.kind)
          )
        )
      )
    );

    // re-attach section headers with live counts
    sectionHeader(ui.sidebar.modelsTitle, "models", `Models (${filteredModels.length})`);
    sectionHeader(ui.sidebar.sourcesTitle, "sources", `Sources (${filteredSources.length})`);
    sectionHeader(ui.sidebar.macrosTitle, "macros", `Macros (${macros.length})`);

    applySidebarCollapse();
  }

  function updateMain() {
    const route = parseRoute();
    let view;
    if (route.route === "model") view = renderModel(state, route.name, route.tab, route.col);
    else if (route.route === "source") view = renderSource(state, route.source, route.table);
    else if (route.route === "macro") view = renderMacro(state, route.name);
    else if (route.route === "macros") { state.macroQuery = route.q || ""; view = renderMacros(state, state.macroQuery); }
    else view = renderHome(state);

    state.ui.mainHost.replaceChildren(view);

    // If home view contains mermaid, render it now (same as before)
    if (route.route === "home") {
      queueMicrotask(async () => {
        const target = document.getElementById("mermaidTarget");
        if (!target) return;
        const dagSrc = state.manifest.dag?.mermaid || "";
        if (!state.mermaid) {
          target.textContent = dagSrc;
          return;
        }
        target.innerHTML = `<pre class="mermaid">${dagSrc}</pre>`;
        try { await state.mermaid.run({ querySelector: "#mermaidTarget .mermaid" }); } catch {}
      });
    }
  }

  window.addEventListener("keydown", (e) => {
    const tag = e.target?.tagName?.toLowerCase();
    const typing = tag === "input" || tag === "textarea" || e.target?.isContentEditable;

    // Ctrl+K (or Cmd+K on mac) opens palette
    const ctrlK = (e.key.toLowerCase() === "k") && (e.ctrlKey || e.metaKey);

    if (!typing && (e.key === "/" || ctrlK)) {
      e.preventDefault();
      openPalette("");
    }
  });

  runSearch("");

  window.addEventListener("hashchange", () => {
    safeSet(STORE.lastHash, location.hash || "#/");
    closePalette();   // optional: close palette on navigation
    updateSidebarLists();
    updateMain();
  });

  safeSet(STORE.lastHash, location.hash || "#/");

  buildSidebar();
  updateSidebarLists();
  buildPalette();       // palette exists but hidden
  updateMain();

}

main().catch((e) => {
  const app = document.getElementById("app");
  app.replaceChildren(
    el("div", { class: "main" },
      el("div", { class: "card" },
        el("h2", {}, "Docs failed to load"),
        el("p", { class: "empty" }, String(e?.message || e)),
        el("p", { class: "empty" }, `Manifest URL: ${MANIFEST_URL}`)
      )
    )
  );
});