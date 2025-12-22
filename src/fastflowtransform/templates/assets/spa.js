const MANIFEST_URL = window.__FFT_MANIFEST_PATH__ || "assets/docs_manifest.json";

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
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
    };
  }
  if (parts[0] === "source" && parts[1] && parts[2]) {
    return { route: "source", source: decodeURIComponent(parts[1]), table: decodeURIComponent(parts[2]) };
  }
  if (parts[0] === "macros") return { route: "macros" };

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
      state._graphCtl?.refresh?.();
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

    // ✅ update segmented control UI
    lrBtn.classList.toggle("active", dir === "LR");
    tbBtn.classList.toggle("active", dir === "TB");
    dirPill.textContent = dir === "TB" ? "Top → Bottom" : "Left → Right";

    // ✅ remount graph
    const g = graphTransformDirection(state.manifest.dag.graph, dir);
    state._graphCtl = mountGraph(state, graphHost, g, { miniHost });
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
    state._graphCtl = mountGraph(state, graphHost, g0, { miniHost });

    const r = parseRoute();
    if (r.route === "home" && r.focus) {
      state._graphCtl?.focus?.(r.focus, { zoom: 1.25, pin: true });
    }
  });

  return graphCard;
}

function renderModel(state, name, tabFromRoute, colFromRoute) {
  const m = state.byModel.get(name);
  if (!m) {
    return el("div", { class: "card" }, el("h2", {}, "Model not found"), el("p", { class: "empty" }, name));
  }

  const active = (tabFromRoute || state.modelTabDefault || "overview").toLowerCase();
  const hasCol = !!(colFromRoute && String(colFromRoute).trim());

  let tab = ["overview","columns","lineage","code","meta"].includes(active) ? active : "overview";
  // Only force columns if col is present AND the URL didn't explicitly set a tab
  if (hasCol && !tabFromRoute) tab = "columns";

  const header = el("div", { class: "card" },
    el("div", { class: "grid2" },
      el("div", {},
        el("h2", {}, m.name),
        el("p", { class: "empty" }, m.relation ? `Relation: ${m.relation}` : "")
      ),
      el("div", {},
        el("button", {
          class: "btn",
          onclick: () => { location.hash = "#/"; }
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
    const deps = (m.deps || []).map(d => el("a", { href: `#/model/${escapeHashPart(d)}` }, d));
    const usedBy = (m.used_by || []).map(u => el("a", { href: `#/model/${escapeHashPart(u)}` }, u));
    const sourcesUsed = (m.sources_used || []).map(s =>
      el("a", { href: `#/source/${escapeHashPart(s.source_name)}/${escapeHashPart(s.table_name)}` }, `${s.source_name}.${s.table_name}`)
    );

    return el("div", { class: "grid" },
      el("div", { class: "card" },
        el("h3", {}, "Summary"),
        el("div", { class: "kv" },
          el("div", { class: "k" }, "Kind"), el("div", {}, m.kind),
          el("div", { class: "k" }, "Materialized"), el("div", {}, m.materialized || "—"),
          el("div", { class: "k" }, "Path"), el("div", {}, el("code", {}, m.path || "—")),
          el("div", { class: "k" }, "Deps"), el("div", {}, deps.length ? joinInline(deps) : el("span", { class: "empty" }, "—")),
          el("div", { class: "k" }, "Used by"), el("div", {}, usedBy.length ? joinInline(usedBy) : el("span", { class: "empty" }, "—")),
          el("div", { class: "k" }, "Sources"), el("div", {}, sourcesUsed.length ? joinInline(sourcesUsed) : el("span", { class: "empty" }, "—")),
        )
      ),
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
    // Placeholder until we add compiled SQL / python source to manifest
    return el("div", { class: "card" },
      el("h3", {}, "Code"),
      el("p", { class: "empty" }, "Code view not yet available. Next step: include rendered SQL / Python source in the manifest.")
    );
  }

  if (tab === "meta") {
    // Show a structured dump of whatever we have
    const meta = {
      name: m.name,
      kind: m.kind,
      relation: m.relation,
      materialized: m.materialized,
      path: m.path,
      deps: m.deps || [],
      used_by: m.used_by || [],
      sources_used: m.sources_used || [],
    };
    return el("div", { class: "card" },
      el("h3", {}, "Meta"),
      el("pre", { class: "mono", style: "white-space:pre-wrap; margin:0;" }, JSON.stringify(meta, null, 2))
    );
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

  const consumers = (s.consumers || []).map(m => el("a", { href: `#/model/${escapeHashPart(m)}` }, m));

  const freshness = (() => {
    const warn = s.warn_after_minutes != null ? `${s.warn_after_minutes}m warn` : null;
    const err = s.error_after_minutes != null ? `${s.error_after_minutes}m error` : null;
    const parts = [warn, err].filter(Boolean);
    return parts.length ? parts.join(" • ") : "—";
  })();

  return el("div", { class: "grid" },
    el("div", { class: "card" },
      el("div", { class: "grid2" },
        el("div", {}, el("h2", {}, key)),
        el("div", {},
          el("button", { class: "btn", onclick: () => { location.hash = "#/"; } }, "← Overview")
        )
      ),
      el("div", { class: "kv" },
        el("div", { class: "k" }, "Relation"), el("div", {}, el("code", {}, s.relation || "—")),
        el("div", { class: "k" }, "Loaded at field"), el("div", {}, el("code", {}, s.loaded_at_field || "—")),
        el("div", { class: "k" }, "Freshness"), el("div", {}, freshness),
        el("div", { class: "k" }, "Consumers"), el("div", {}, consumers.length ? joinInline(consumers) : el("span", { class: "empty" }, "—")),
      )
    ),
    s.description_html
      ? el("div", { class: "card" }, el("h2", {}, "Description"), el("div", { class: "desc", html: s.description_html }))
      : null
  );
}

function renderMacros(state) {
  const ms = state.manifest.macros || [];
  return el("div", { class: "card" },
    el("h2", {}, "Macros"),
    ms.length
      ? el("table", { class: "table" },
          el("thead", {}, el("tr", {},
            el("th", {}, "Name"),
            el("th", {}, "Kind"),
            el("th", {}, "Path"),
          )),
          el("tbody", {},
            ...ms.map(m => el("tr", {},
              el("td", {}, el("code", {}, m.name)),
              el("td", {}, m.kind),
              el("td", {}, el("code", {}, m.path)),
            ))
          )
        )
      : el("p", { class: "empty" }, "No macros discovered.")
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
    class: "input",
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
  const miniHost = opts.miniHost || null;

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
        location.hash = `#/model/${escapeHashPart(nm)}`;
      } else if (route.startsWith("#/source/")) {
        const rest = route.slice("#/source/".length).split("/");
        const s = rest[0] || "";
        const t = rest[1] || "";
        location.hash = `#/source/${escapeHashPart(s)}/${escapeHashPart(t)}`;
      } else {
        location.hash = route;
      }
    };

    // g.addEventListener("click", (ev) => {
    //   if (ev.shiftKey) {
    //     ev.preventDefault();
    //     ev.stopPropagation();
    //     state.graphUI.pinned = n.id;
    //     applyHighlight();
    //     return;
    //   }
    //   go(ev); // existing navigate behavior
    // });

    g.addEventListener("click", (ev) => {
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

  if (miniHost) {
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

    // miniView.addEventListener("pointerdown", (ev) => {
    //   ev.preventDefault();
    //   ev.stopPropagation();

    //   miniView.setPointerCapture(ev.pointerId);
    //   miniView.style.cursor = "grabbing";

    //   const p0 = miniClientToGraph(ev);
    //   miniDrag = {
    //     id: ev.pointerId,
    //     p0,
    //     // current top-left of visible area in graph coords:
    //     vx0: (-tx) / scale,
    //     vy0: (-ty) / scale,
    //     moved: false,
    //   };
    // });

    // miniView.addEventListener("pointermove", (ev) => {
    //   if (!miniDrag || ev.pointerId !== miniDrag.id) return;
    //   ev.preventDefault();

    //   const p = miniClientToGraph(ev);
    //   const dx = p.x - miniDrag.p0.x;
    //   const dy = p.y - miniDrag.p0.y;

    //   // visible size (graph coords)
    //   const sr = svg.getBoundingClientRect();
    //   const vw = sr.width / scale;
    //   const vh = sr.height / scale;

    //   const vb = miniSvg.viewBox.baseVal;
    //   const maxVx = vb.x + vb.width - vw;
    //   const maxVy = vb.y + vb.height - vh;

    //   const vx = clamp(miniDrag.vx0 + dx, vb.x, maxVx);
    //   const vy = clamp(miniDrag.vy0 + dy, vb.y, maxVy);

    //   tx = -vx * scale;
    //   ty = -vy * scale;

    //   if (Math.abs(dx) + Math.abs(dy) > 0.5) miniDrag.moved = true;
    //   apply();
    // });

    // function endMiniDrag(ev) {
    //   if (!miniDrag || ev.pointerId !== miniDrag.id) return;
    //   ev.preventDefault();

    //   miniSuppressClick = miniDrag.moved; // prevents click-to-center after drag
    //   miniDrag = null;

    //   try { miniView.releasePointerCapture(ev.pointerId); } catch {}
    //   miniView.style.cursor = "grab";
    // }

    // miniView.addEventListener("pointerup", endMiniDrag);
    // miniView.addEventListener("pointercancel", endMiniDrag);

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
  state.modelTabDefault = safeGet(STORE.modelTab) || "overview";
  state.STORE = STORE;

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
    const sub = (() => {
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
    })();

    const right = r.kind === "column" && r.dtype
      ? el("span", { class: "pill" }, r.dtype)
      : el("div", { class: "kbd" }, "↵");

    state.ui.paletteList.replaceChildren(
      ...(results.length
        ? results.map((r, idx) =>
            el("div", {
              class: `result ${idx === sel ? "sel" : ""}`,
              onclick: () => {
                closePalette();
                location.hash = r.route;
              },
            },
              el("div", { class: "resultMain" },
                el("div", { class: "resultTitle" }, r.title),
                el("div", { class: "resultSub" }, sub)
              ),
              right
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
              location.hash = `#/?focus=${encodeURIComponent(gid)}`;
              return;
            }

            // Already on home: focus immediately
            state._graphCtl?.focus?.(gid, { zoom: 1.25, pin: true });
            return;
          }

          // Normal Enter => navigate
          closePalette();
          location.hash = hit.route;
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

        // Empty input => Enter opens global palette
        if (!q) {
          e.preventDefault();
          openPalette("");
          return;
        }

        // No sidebar matches => Enter escalates to global palette (prefilled)
        if (total === 0) {
          e.preventDefault();
          openPalette(q);
          return;
        }

        // Otherwise: normal behavior (do nothing special)
      },
    });

    const overviewSection = el("div", { class: "section" },
      el("div", {},
        el("a", {
          href: "#/",
          onclick: (e) => { e.preventDefault(); location.hash = "#/"; },
          class: "itemLink", // optional, if you have it; otherwise omit
          style: "display:flex; align-items:center; justify-content:space-between; padding:8px 10px; border:1px solid var(--border); border-radius:12px; text-decoration:none; color:inherit;"
        },
          el("span", {}, "Overview (DAG)"),
          el("span", { class: "pill" }, "Home")
        )
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
        el("a", {
          href: "#/",
          style: "color:inherit; text-decoration:none;",
          onclick: (e) => { e.preventDefault(); location.hash = "#/"; }
        }, el("h1", {}, state.manifest.project?.name || "Docs")),
        el("span", { class: "badge", title: `Generated: ${state.manifest.project?.generated_at || ""}` }, "SPA")
      ),
      el(
        "div",
        { class: "searchWrap" },
        ui.sidebar.input,
        el("span", { class: "searchKbd kbd" }, "/")
      ),
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
    const q = (state.filter || "").trim().toLowerCase();
    const models = state.manifest.models || [];
    const sources = state.manifest.sources || [];

    const filteredModels = q
      ? models.filter(m =>
          (m.name || "").toLowerCase().includes(q) ||
          (m.relation || "").toLowerCase().includes(q) ||
          (m.description_short || "").toLowerCase().includes(q)
        )
      : models;

    const filteredSources = q
      ? sources.filter(s =>
          (`${s.source_name}.${s.table_name}`).toLowerCase().includes(q) ||
          (s.relation || "").toLowerCase().includes(q)
        )
      : sources;

    state.sidebarMatches.models = filteredModels.length;
    state.sidebarMatches.sources = filteredSources.length;

    ui.sidebar.modelsTitle.textContent = `Models (${filteredModels.length})`;
    ui.sidebar.sourcesTitle.textContent = `Sources (${filteredSources.length})`;

    ui.sidebar.modelsList.replaceChildren(
      ...filteredModels.map(m =>
        el("li", { class: "item" },
          el("a", {
            href: `#/model/${escapeHashPart(m.name)}`,
            onclick: (e) => { e.preventDefault(); location.hash = `#/model/${escapeHashPart(m.name)}`; },
            title: m.description_short || m.name,
          },
            el("span", {}, m.name),
            pillForKind(m.kind === "python" ? "python" : "sql")
          )
        )
      )
    );

    ui.sidebar.sourcesList.replaceChildren(
      ...filteredSources.map(s => {
        const key = `${s.source_name}.${s.table_name}`;
        return el("li", { class: "item" },
          el("a", {
            href: `#/source/${escapeHashPart(s.source_name)}/${escapeHashPart(s.table_name)}`,
            onclick: (e) => { e.preventDefault(); location.hash = `#/source/${escapeHashPart(s.source_name)}/${escapeHashPart(s.table_name)}`; },
            title: s.relation || key,
          },
            el("span", {}, key),
            el("span", { class: "pill" }, (s.consumers || []).length ? `${s.consumers.length}` : "–")
          )
        );
      })
    );

    const macros = state.manifest.macros || [];
    
    sectionHeader(ui.sidebar.modelsTitle, "models", `Models (${filteredModels.length})`);
    sectionHeader(ui.sidebar.sourcesTitle, "sources", `Sources (${filteredSources.length})`);
    sectionHeader(ui.sidebar.macrosTitle, "macros", `Macros (${macros.length})`);

    ui.sidebar.macrosList.replaceChildren(
      ...macros.map(m =>
        el("li", { class: "item" },
          el("a", {
            href: "#/macros",
            onclick: (e) => { e.preventDefault(); location.hash = "#/macros"; },
            title: m.path || m.name,
          },
            el("span", {}, m.name),
            el("span", { class: "pill" }, m.kind)
          )
        )
      )
    );
    
    applySidebarCollapse();

  }

  function updateMain() {
    const route = parseRoute();
    let view;
    if (route.route === "model") view = renderModel(state, route.name, route.tab, route.col);
    else if (route.route === "source") view = renderSource(state, route.source, route.table);
    else if (route.route === "macros") view = renderMacros(state);
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
