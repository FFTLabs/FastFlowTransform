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
function parseHash() {
  const raw = (location.hash || "#/").slice(1);
  const parts = raw.split("/").filter(Boolean);
  if (parts.length === 0) return { route: "home" };
  if (parts[0] === "model" && parts[1]) return { route: "model", name: decodeURIComponent(parts.slice(1).join("/")) };
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

function renderSidebar(state, onNavigate) {
  const { manifest, filter } = state;
  const models = manifest.models || [];
  const sources = manifest.sources || [];

  const q = (filter || "").trim().toLowerCase();

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

  return el(
    "div",
    { class: "sidebar" },
    el(
      "div",
      { class: "brand" },
      el("h1", {}, manifest.project?.name || "Docs"),
      el("span", { class: "badge", title: `Generated: ${manifest.project?.generated_at || ""}` }, "SPA")
    ),
    el("div", { class: "searchWrap" },
      el("input", {
        class: "search",
        type: "search",
        placeholder: "Filter sidebar… (press /)",
        value: filter || "",
        oninput: (e) => onNavigate({ type: "filter", value: e.target.value }),
      }),
      el("span", { class: "searchKbd kbd" }, "/")
    ),
    el("div", { class: "searchTip" }, "Tip: Press / (or Ctrl+K) to search everything (models, sources, columns)."),
    el(
      "div",
      { class: "section" },
      el("h2", {}, `Models (${filteredModels.length})`),
      el(
        "ul",
        { class: "list" },
        ...filteredModels.map(m =>
          el(
            "li",
            { class: "item" },
            el(
              "a",
              {
                href: `#/model/${escapeHashPart(m.name)}`,
                onclick: (e) => { e.preventDefault(); location.hash = `#/model/${escapeHashPart(m.name)}`; },
                title: m.description_short || m.name,
              },
              el("span", {}, m.name),
              pillForKind(m.kind === "python" ? "python" : "sql")
            )
          )
        )
      )
    ),
    el(
      "div",
      { class: "section" },
      el("h2", {}, `Sources (${filteredSources.length})`),
      el(
        "ul",
        { class: "list" },
        ...filteredSources.map(s => {
          const key = `${s.source_name}.${s.table_name}`;
          return el(
            "li",
            { class: "item" },
            el(
              "a",
              {
                href: `#/source/${escapeHashPart(s.source_name)}/${escapeHashPart(s.table_name)}`,
                onclick: (e) => { e.preventDefault(); location.hash = `#/source/${escapeHashPart(s.source_name)}/${escapeHashPart(s.table_name)}`; },
                title: s.relation || key,
              },
              el("span", {}, key),
              el("span", { class: "pill" }, (s.consumers || []).length ? `${s.consumers.length}` : "–")
            )
          );
        })
      )
    ),
    el(
      "div",
      { class: "section" },
      el("h2", {}, "Other"),
      el("ul", { class: "list" },
        el("li", { class: "item" },
          el("a", {
            href: "#/macros",
            onclick: (e) => { e.preventDefault(); location.hash = "#/macros"; },
          }, el("span", {}, "Macros"), el("span", { class: "pill" }, String((manifest.macros || []).length))))
      )
    )
  );
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

function renderModel(state, name) {
  const { manifest } = state;
  const byModel = state.byModel;
  const m = byModel.get(name);

  if (!m) {
    return el("div", { class: "card" }, el("h2", {}, "Model not found"), el("p", { class: "empty" }, name));
  }

  const deps = (m.deps || []).map(d => el("a", { href: `#/model/${escapeHashPart(d)}` }, d));
  const usedBy = (m.used_by || []).map(u => el("a", { href: `#/model/${escapeHashPart(u)}` }, u));

  const sourcesUsed = (m.sources_used || []).map(s =>
    el("a", { href: `#/source/${escapeHashPart(s.source_name)}/${escapeHashPart(s.table_name)}` }, `${s.source_name}.${s.table_name}`)
  );

  const head = el("div", { class: "card" },
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
    el("div", { class: "kv" },
      el("div", { class: "k" }, "Kind"), el("div", {}, m.kind),
      el("div", { class: "k" }, "Materialized"), el("div", {}, m.materialized || "—"),
      el("div", { class: "k" }, "Path"), el("div", {}, el("code", {}, m.path || "—")),
      el("div", { class: "k" }, "Deps"), el("div", {}, deps.length ? joinInline(deps) : el("span", { class: "empty" }, "—")),
      el("div", { class: "k" }, "Used by"), el("div", {}, usedBy.length ? joinInline(usedBy) : el("span", { class: "empty" }, "—")),
      el("div", { class: "k" }, "Sources"), el("div", {}, sourcesUsed.length ? joinInline(sourcesUsed) : el("span", { class: "empty" }, "—")),
    )
  );

  const desc = m.description_html
    ? el("div", { class: "card" }, el("h2", {}, "Description"), el("div", { class: "desc", html: m.description_html }))
    : null;

  const cols = (m.columns || []);
  const colsCard = cols.length
    ? el("div", { class: "card" },
        el("h2", {}, "Columns"),
        el("table", { class: "table" },
          el("thead", {}, el("tr", {},
            el("th", {}, "Name"),
            el("th", {}, "Type"),
            el("th", {}, "Nullable"),
            el("th", {}, "Description"),
            el("th", {}, "Lineage"),
          )),
          el("tbody", {},
            ...cols.map(c => el("tr", {},
              el("td", {}, el("code", {}, c.name)),
              el("td", {}, el("code", {}, c.dtype || "")),
              el("td", {}, c.nullable ? "true" : "false"),
              el("td", { html: c.description_html || '<span class="empty">—</span>' }),
              el("td", {}, renderLineage(c.lineage || []))
            ))
          )
        )
      )
    : el("div", { class: "card" }, el("h2", {}, "Columns"), el("p", { class: "empty" }, manifest.project?.with_schema ? "No columns found." : "Schema collection disabled."));

  return el("div", { class: "grid" }, head, desc, colsCard);
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

  const projKey = (manifest.project?.name || "fft").toLowerCase().replace(/\s+/g, "_");
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
        title: `${m.name}.${c.name}`,
        subtitle: `${m.relation || ""}${c.dtype ? " • " + c.dtype : ""}`,
        route: `#/model/${escapeHashPart(m.name)}`, // navigates to model; we can later auto-scroll to column
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
                el("div", { class: "resultSub" }, `${r.kind.toUpperCase()} • ${r.subtitle || ""}`)
              ),
              el("div", { class: "kbd" }, "↵")
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
        runSearch(state.search.query);
        renderPaletteResults(); // ✅ no app rerender
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

    state.search.open = true;
    state.search.query = prefill;
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
    macrosCount: null,
  };

  function buildSidebar() {
    if (ui.sidebar.root) return;

    ui.sidebar.input = el("input", {
      class: "search",
      type: "search",
      placeholder: "Filter sidebar… (press /)",
      value: state.filter || "",
      oninput: (e) => {
        state.filter = e.target.value || "";
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

    ui.sidebar.modelsTitle = el("h2", {}, "Models");
    ui.sidebar.sourcesTitle = el("h2", {}, "Sources");
    ui.sidebar.macrosCount = el("span", { class: "pill" }, "0");

    ui.sidebar.modelsList = el("ul", { class: "list" });
    ui.sidebar.sourcesList = el("ul", { class: "list" });

    ui.sidebar.root = el(
      "div",
      { class: "sidebar" },
      el(
        "div",
        { class: "brand" },
        el("h1", {}, state.manifest.project?.name || "Docs"),
        el("span", { class: "badge" }, "SPA")
      ),
      el(
        "div",
        { class: "searchWrap" },
        ui.sidebar.input,
        el("span", { class: "searchKbd kbd" }, "/")
      ),
      el(
        "div",
        { class: "searchTip" },
        "Tip: Press / (or Ctrl+K) to search everything (models, sources, columns)."
      ),

      el("div", { class: "section" }, ui.sidebar.modelsTitle, ui.sidebar.modelsList),
      el("div", { class: "section" }, ui.sidebar.sourcesTitle, ui.sidebar.sourcesList),

      el("div", { class: "section" },
        el("h2", {}, "Other"),
        el("ul", { class: "list" },
          el("li", { class: "item" },
            el("a", {
              href: "#/macros",
              onclick: (e) => { e.preventDefault(); location.hash = "#/macros"; },
            }, el("span", {}, "Macros"), ui.sidebar.macrosCount)
          )
        )
      )
    );

    ui.sidebarHost.replaceChildren(ui.sidebar.root);
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
    ui.sidebar.macrosCount.textContent = String((state.manifest.macros || []).length);

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
  }

  function updateMain() {
    const route = parseHash();
    let view;
    if (route.route === "model") view = renderModel(state, route.name);
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
  buildPalette();

  window.addEventListener("hashchange", () => {
    closePalette();   // optional: close palette on navigation
    updateMain();
  });

  buildSidebar();
  updateSidebarLists();

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
