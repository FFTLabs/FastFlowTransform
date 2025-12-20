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
  if (parts.length === 0) return { route: "home" };

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

async function initMermaid() {
  try {
    const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    const mod = await import("https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs");
    const mermaid = mod.default;
    mermaid.initialize({ startOnLoad: false, securityLevel: "loose", theme: prefersDark ? "dark" : "default" });
    return mermaid;
  } catch (e) {
    console.warn("Mermaid failed to load:", e);
    return null;
  }
}

function byName(arr, keyFn) {
  const m = new Map();
  for (const x of arr) m.set(keyFn(x), x);
  return m;
}

function pillForKind(kind) {
  return el("span", { class: `pill ${kind}` }, kind);
}

function renderHome(state) {
  const { manifest, mermaid } = state;
  const dagSrc = manifest.dag?.mermaid || "";

  const dagCard = el("div", { class: "card" },
    el("div", { class: "grid" },
      el("div", { class: "grid2" },
        el("div", {},
          el("h2", {}, "DAG"),
          el("p", { class: "empty" }, "Mermaid is rendered client-side.")
        ),
        el("div", {},
          el("button", {
            class: "btn",
            onclick: async () => {
              try { await navigator.clipboard.writeText(dagSrc); } catch {}
            }
          }, "Copy Mermaid")
        )
      ),
      el("div", { class: "mermaidWrap" },
        el("div", { id: "mermaidTarget" })
      )
    )
  );

  // Render mermaid after DOM is mounted
  queueMicrotask(async () => {
    const target = document.getElementById("mermaidTarget");
    if (!target) return;
    if (!mermaid) {
      target.textContent = dagSrc;
      return;
    }
    target.innerHTML = `<pre class="mermaid">${dagSrc}</pre>`;
    try { await mermaid.run({ querySelector: "#mermaidTarget .mermaid" }); } catch {}
  });

  const stats = el("div", { class: "card" },
    el("h2", {}, "Overview"),
    el("div", { class: "kv" },
      el("div", { class: "k" }, "Models"), el("div", {}, String((manifest.models || []).length)),
      el("div", { class: "k" }, "Sources"), el("div", {}, String((manifest.sources || []).length)),
      el("div", { class: "k" }, "Macros"), el("div", {}, String((manifest.macros || []).length)),
      el("div", { class: "k" }, "Schema"), el("div", {}, manifest.project?.with_schema ? "enabled" : "disabled"),
      el("div", { class: "k" }, "Generated"), el("div", {}, manifest.project?.generated_at || "—")
    )
  );

  return el("div", { class: "grid2" }, dagCard, stats);
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
    const cols = m.columns || [];

    const card = cols.length
      ? el("div", { class: "card" },
          el("h3", {}, `Columns (${cols.length})`),
          el("table", { class: "table" },
            el("thead", {}, el("tr", {},
              el("th", {}, "Name"),
              el("th", {}, "Type"),
              el("th", {}, "Nullable"),
              el("th", {}, "Description"),
            )),
            el("tbody", {},
              ...cols.map(c => el(
                "tr",
                { id: `col-${cssSafeId(m.name)}-${cssSafeId(c.name)}` },
                el("td", {}, el("code", {}, c.name)),
                el("td", {}, el("code", {}, c.dtype || "")),
                el("td", {}, c.nullable ? "true" : "false"),
                el("td", { html: c.description_html || '<span class="empty">—</span>' }),
              ))
            )
          )
        )
      : el("div", { class: "card" },
          el("h3", {}, "Columns"),
          el("p", { class: "empty" }, state.manifest.project?.with_schema ? "No columns found." : "Schema collection disabled.")
        );

    // Scroll + highlight if col query param is present
    const colName = (colFromRoute || "").trim();
    if (cols.length && colName) {
      queueMicrotask(() => {
        const rowId = `col-${cssSafeId(m.name)}-${cssSafeId(colName)}`;
        const row = document.getElementById(rowId);
        if (!row) return;

        // clear previous hit
        document.querySelectorAll("tr.colHit").forEach(n => n.classList.remove("colHit"));

        row.classList.add("colHit");
        row.scrollIntoView({ block: "center", behavior: "smooth" });

        // remove highlight after a moment (optional)
        setTimeout(() => row.classList.remove("colHit"), 2200);
      });
    }

    return card;
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
      el("h2", {}, key),
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

async function loadManifest() {
  const res = await fetch(MANIFEST_URL, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to load manifest: ${res.status}`);
  return await res.json();
}

async function main() {
  const app = document.getElementById("app");
  app.textContent = "Loading…";

  const [manifest, mermaid] = await Promise.all([loadManifest(), initMermaid()]);
  const state = {
    manifest,
    mermaid,
    filter: "",
    byModel: byName(manifest.models || [], (m) => m.name),
    bySource: byName(manifest.sources || [], (s) => `${s.source_name}.${s.table_name}`),
  };
  state.sidebarMatches = { models: 0, sources: 0 };

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
          if (hit) {
            closePalette();
            location.hash = hit.route;
          }
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

      ui.sidebar.modelsTitle = el("div");
      ui.sidebar.sourcesTitle = el("div");
      ui.sidebar.macrosTitle = el("div");

      ui.sidebar.modelsList = el("ul", { class: "list" });
      ui.sidebar.sourcesList = el("ul", { class: "list" });
      ui.sidebar.macrosList = el("ul", { class: "list" });

      ui.sidebar.modelsSection = el("div", { class: "section" }, ui.sidebar.modelsTitle, ui.sidebar.modelsList);
      ui.sidebar.sourcesSection = el("div", { class: "section" }, ui.sidebar.sourcesTitle, ui.sidebar.sourcesList);
      ui.sidebar.macrosSection = el("div", { class: "section" }, ui.sidebar.macrosTitle, ui.sidebar.macrosList);

      ui.sidebar.root = el(
        "div",
        { class: "sidebar" },
        el(
          "div",
          { class: "brand" },
          el("h1", {}, state.manifest.project?.name || "Docs"),
          el("span", { class: "badge", title: `Generated: ${state.manifest.project?.generated_at || ""}` }, "SPA")
        ),
        el(
          "div",
          { class: "searchWrap" },
          ui.sidebar.input,
          el("span", { class: "searchKbd kbd" }, "/")
        ),
        el("div", { class: "searchTip" }, "Tip: Press / (or Ctrl+K) to search everything (models, sources, columns)."),
        ui.sidebar.modelsSection,
        ui.sidebar.sourcesSection,
        ui.sidebar.macrosSection,
      );

      ui.sidebarHost.replaceChildren(ui.sidebar.root);

      // Turn titles into toggle headers
      sectionHeader(ui.sidebar.modelsTitle, "models", "Models");
      sectionHeader(ui.sidebar.sourcesTitle, "sources", "Sources");
      sectionHeader(ui.sidebar.macrosTitle, "macros", "Macros");

  }

  function applySidebarCollapse() {
    const c = state.sidebarCollapsed || {};
    ui.sidebar.modelsList.style.display = c.models ? "none" : "";
    ui.sidebar.sourcesList.style.display = c.sources ? "none" : "";
    ui.sidebar.macrosList.style.display = c.macros ? "none" : "";
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
