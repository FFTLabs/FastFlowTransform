# fastflowtransform/docs.py
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from markupsafe import Markup

from fastflowtransform.core import REGISTRY, Node, relation_for
from fastflowtransform.dag import mermaid as dag_mermaid
from fastflowtransform.executors.base import ColumnInfo
from fastflowtransform.lineage import (
    infer_py_lineage,
    infer_sql_lineage,
    merge_lineage,
    parse_sql_lineage_overrides,
)


@dataclass
class ModelDoc:
    name: str
    kind: str
    path: str
    relation: str
    deps: list[str]
    materialized: str
    description_html: str | None = None
    description_short: str | None = None


@dataclass
class SourceDoc:
    source_name: str
    table_name: str
    relation: str
    description_html: str | None = None
    loaded_at_field: str | None = None
    warn_after_minutes: int | None = None
    error_after_minutes: int | None = None
    consumers: list[str] = field(default_factory=list)
    doc_filename: str = ""


def _safe_filename(name: str) -> str:
    """Filename sanitized while keeping dots/slashes meaningful."""
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return s or "_model"


def _collect_columns(executor: Any) -> dict[str, list[ColumnInfo]]:
    """
    Best-effort schema discovery delegated to the executor.
    Returns an empty mapping if unsupported or on errors.
    """
    fn = getattr(executor, "collect_docs_columns", None)
    if not callable(fn):
        return {}
    try:
        res = fn()
        return res if isinstance(res, dict) else {}
    except Exception:
        # Fail-open: no schema info, UI will simply hide the columns card.
        return {}


def _read_project_yaml_docs(project_dir: Path) -> dict[str, Any]:
    """
    Read optional docs metadata from project.yml:
      docs:
        models:
          <node_name>:
            description: "..."
            columns:
              <column_name>: "..."
    Returns a dict keyed by logical node name.
    """
    cfg_path = project_dir / "project.yml"
    if not cfg_path.exists():
        return {}
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    docs = (cfg or {}).get("docs") or {}
    models = (docs or {}).get("models") or {}
    return models if isinstance(models, dict) else {}


_FRONT_MATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_CODE_RE = re.compile(r"`([^`]+)`")


def _render_minimarkdown(md: str) -> str:
    """
    Very small Markdown-to-HTML converter (no external deps).
    Supports: paragraphs, inline code, links.
    """
    if not md:
        return ""
    # Strip front matter if present (already parsed elsewhere)
    body = _FRONT_MATTER_RE.sub("", md, count=1)
    # Inline code
    body = _CODE_RE.sub(r"<code>\1</code>", body)
    # Links
    body = _LINK_RE.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', body)
    # Split into paragraphs on blank lines
    parts = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    html = "".join(f"<p>{p.replace('\n', '<br/>')}</p>" for p in parts) if parts else ""
    return html


def _strip_html(text: str) -> str:
    """Remove very simple HTML tags for generating a short preview snippet."""
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", t).strip()


def _read_markdown_file(p: Path) -> tuple[dict[str, Any], str]:
    """
    Read a Markdown file with optional YAML front matter.
    Returns (front_matter_dict, body_text).
    """
    if not p.exists():
        return {}, ""
    raw = p.read_text(encoding="utf-8")
    m = _FRONT_MATTER_RE.match(raw)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            fm = {}
        body = raw[m.end() :]
        return (fm if isinstance(fm, dict) else {}), body
    return {}, raw


def _init_jinja() -> Environment:
    """Load bundled templates and return a Jinja environment."""
    tmpl_dir = Path(__file__).parent / "templates"
    return Environment(
        loader=FileSystemLoader([str(tmpl_dir)]),
        autoescape=select_autoescape(["html", "xml"]),
    )


_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(s: str | None) -> str | None:
    if not s:
        return None
    # fast + good-enough for docs descriptions
    txt = _TAG_RE.sub("", s)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or None


def _copy_template_assets(out_dir: Path) -> None:
    """
    Copy packaged static assets from templates/assets -> <out_dir>/assets.
    Safe no-op if no assets exist.
    """
    tmpl_dir = Path(__file__).parent / "templates"
    src = tmpl_dir / "assets"
    if not src.exists() or not src.is_dir():
        return
    dst = out_dir / "assets"
    dst.mkdir(parents=True, exist_ok=True)
    for p in src.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)


def _project_name(proj_dir: Path | None) -> str:
    if not proj_dir:
        return "FastFlowTransform"
    cfg_path = proj_dir / "project.yml"
    try:
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(cfg, dict) and cfg.get("name"):
                return str(cfg["name"])
    except Exception:
        pass
    return proj_dir.name


def _build_spa_manifest(
    *,
    proj_name: str,
    env_name: str | None,
    with_schema: bool,
    mermaid_src: str,
    models: list[ModelDoc],
    sources: list[SourceDoc],
    macros: list[dict[str, str]],
    used_by: dict[str, list[str]],
    cols_by_table: dict[str, list[ColumnInfo]],
    model_source_refs: dict[str, list[tuple[str, str]]],
    sources_by_key: dict[tuple[str, str], SourceDoc],
) -> dict[str, Any]:
    def _col_to_dict(c: ColumnInfo) -> dict[str, Any]:
        html = c.description_html
        html_s = str(html) if html is not None else None
        return {
            "name": c.name,
            "dtype": c.dtype,
            "nullable": bool(c.nullable),
            "description_html": c.description_html,
            "description_text": _html_to_text(html_s),
            "lineage": c.lineage or [],
        }

    out_models: list[dict[str, Any]] = []
    for m in models:
        # model -> sources used (source(), table)
        src_keys = model_source_refs.get(m.name, []) or []
        src_used = []
        for k in src_keys:
            doc = sources_by_key.get(k)
            if not doc:
                continue
            src_used.append(
                {
                    "source_name": doc.source_name,
                    "table_name": doc.table_name,
                    "relation": doc.relation,
                }
            )

        cols = []
        if with_schema and m.relation in cols_by_table:
            cols = [_col_to_dict(c) for c in (cols_by_table.get(m.relation) or [])]

        model_desc_html = m.description_html
        model_desc_html_s = str(model_desc_html) if model_desc_html is not None else None

        out_models.append(
            {
                "name": m.name,
                "kind": m.kind,
                "path": m.path,
                "relation": m.relation,
                "deps": list(m.deps or []),
                "used_by": list(used_by.get(m.name, []) or []),
                "materialized": m.materialized,
                "description_html": m.description_html,
                "description_text": _html_to_text(model_desc_html_s),
                "description_short": m.description_short,
                "sources_used": src_used,
                "columns": cols,
            }
        )

    out_sources: list[dict[str, Any]] = []
    for s in sources:
        src_desc_html = s.description_html
        src_desc_html_s = str(src_desc_html) if src_desc_html is not None else None

        out_sources.append(
            {
                "source_name": s.source_name,
                "table_name": s.table_name,
                "relation": s.relation,
                "description_html": s.description_html,
                "description_text": _html_to_text(src_desc_html_s),
                "loaded_at_field": s.loaded_at_field,
                "warn_after_minutes": s.warn_after_minutes,
                "error_after_minutes": s.error_after_minutes,
                "consumers": list(s.consumers or []),
            }
        )

    return {
        "project": {
            "name": proj_name,
            "generated_at": datetime.now(UTC).isoformat(),
            "env": env_name,
            "with_schema": bool(with_schema),
        },
        "dag": {"mermaid": mermaid_src},
        "models": out_models,
        "sources": out_sources,
        "macros": macros,
    }


def _get_project_dir() -> Path | None:
    """Best-effort resolution of the project dir from the registry."""
    if not hasattr(REGISTRY, "get_project_dir"):
        return None
    try:
        return REGISTRY.get_project_dir()
    except Exception:
        return None


def _materialization_legend() -> dict[str, dict[str, str]]:
    # Add 'incremental' to avoid UndefinedError in templates that render badges
    # for materialization types (index & model pages).
    return {
        "table": {"label": "table", "class": "badge-table"},
        "view": {"label": "view", "class": "badge-view"},
        "ephemeral": {"label": "ephemeral", "class": "badge-ephemeral"},
        "incremental": {"label": "incremental", "class": "badge-incremental"},
        "snapshot": {"label": "snapshot", "class": "badge-snapshot"},
    }


def _build_macro_list(proj_dir: Path | None) -> list[dict[str, str]]:
    macro_list: list[dict[str, str]] = []
    for name, p in getattr(REGISTRY, "macros", {}).items():
        mp = Path(p)
        rel = mp.name
        kind = "python" if p.suffix.lower() == ".py" else "sql"
        if proj_dir:
            try:
                rel = str(mp.relative_to(proj_dir))
            except Exception:
                rel = mp.name
        macro_list.append({"name": name, "path": rel, "kind": kind})
    macro_list.sort(key=lambda x: (x["kind"], x["name"]))
    return macro_list


def _collect_models(nodes: dict[str, Node]) -> list[ModelDoc]:
    models = [
        ModelDoc(
            name=n.name,
            kind=n.kind,
            path=str(n.path),
            relation=relation_for(n.name),
            deps=list(n.deps or []),
            materialized=(getattr(n, "meta", {}) or {}).get("materialized", "table"),
        )
        for n in nodes.values()
    ]
    models.sort(key=lambda m: m.name)
    return models


def _freshness_window_minutes(win: dict[str, Any] | None) -> int | None:
    if not isinstance(win, dict):
        return None
    count = win.get("count")
    period = win.get("period")
    if count is None or period is None:
        return None
    try:
        count_int = int(count)
    except (TypeError, ValueError):
        return None
    period = str(period).lower()
    if period == "minute":
        factor = 1
    elif period == "hour":
        factor = 60
    elif period == "day":
        factor = 60 * 24
    else:
        return None
    return count_int * factor


def _collect_sources() -> list[SourceDoc]:
    """Build SourceDoc objects from REGISTRY.sources (sources.yml)."""
    srcs = getattr(REGISTRY, "sources", {}) or {}
    out: list[SourceDoc] = []

    for src_name, tables in srcs.items():
        for tbl_name, entry in (tables or {}).items():
            base = entry.get("base") or {}
            descr = entry.get("description")
            freshness_cfg = entry.get("freshness") or {}

            desc_html = _render_minimarkdown(descr) if isinstance(descr, str) else None

            loaded_at = freshness_cfg.get("loaded_at_field")
            warn_after_minutes = _freshness_window_minutes(freshness_cfg.get("warn_after"))
            err_after_minutes = _freshness_window_minutes(freshness_cfg.get("error_after"))

            # Use the same relation logic as runtime (best-effort)
            # Here we don't know engine; we keep it simple and just use identifier/schema.
            ident = base.get("identifier") or tbl_name
            schema = base.get("schema") or base.get("dataset")
            database = base.get("database") or base.get("catalog") or base.get("project")

            if database and schema:
                relation = f"{database}.{schema}.{ident}"
            elif schema:
                relation = f"{schema}.{ident}"
            elif database:
                relation = f"{database}.{ident}"
            else:
                relation = str(ident)

            display = f"{src_name}.{tbl_name}"
            out.append(
                SourceDoc(
                    source_name=src_name,
                    table_name=tbl_name,
                    relation=relation,
                    description_html=desc_html,
                    loaded_at_field=loaded_at,
                    warn_after_minutes=warn_after_minutes,
                    error_after_minutes=err_after_minutes,
                    doc_filename=f"source_{_safe_filename(display)}.html",
                )
            )

    out.sort(key=lambda s: (s.source_name, s.table_name))
    return out


def _scan_source_refs(
    nodes: dict[str, Node],
) -> tuple[dict[tuple[str, str], list[str]], dict[str, list[tuple[str, str]]]]:
    """Return mappings: (source, table) → [models] and model → [(source, table)]."""

    pattern = re.compile(
        r"source\s*\(\s*['\"](?P<source>[A-Za-z0-9_.\-]+)['\"]\s*,\s*['\"](?P<table>[A-Za-z0-9_.\-]+)['\"]\s*\)"
    )

    by_source: dict[tuple[str, str], list[str]] = {}
    by_model: dict[str, list[tuple[str, str]]] = {}

    for n in nodes.values():
        if getattr(n, "kind", "sql") != "sql":
            continue
        try:
            txt = Path(n.path).read_text(encoding="utf-8")
        except Exception:
            continue
        hits: list[tuple[str, str]] = []
        for match in pattern.finditer(txt):
            key = (match.group("source"), match.group("table"))
            by_source.setdefault(key, []).append(n.name)
            hits.append(key)
        if hits:
            seen: set[tuple[str, str]] = set()
            order: list[tuple[str, str]] = []
            for key in hits:
                if key in seen:
                    continue
                seen.add(key)
                order.append(key)
            by_model[n.name] = order

    for names in by_source.values():
        names.sort()

    return by_source, by_model


def _attach_consumers_to_sources(
    sources: list[SourceDoc], source_consumers: dict[tuple[str, str], list[str]]
) -> None:
    for s in sources:
        s.consumers = source_consumers.get((s.source_name, s.table_name), [])


def _apply_descriptions_to_models(
    models: list[ModelDoc],
    docs_meta: dict[str, Any],
    cols_by_table: dict[str, list[ColumnInfo]],
    *,
    with_schema: bool,
) -> None:
    """Enrich ModelDoc + ColumnInfo mit Description-HTML (bereits gemergt in docs_meta)."""
    for m in models:
        model_meta = (
            (docs_meta.get("models", {}) or {}).get(m.name, {})
            if isinstance(docs_meta, dict)
            else {}
        )
        desc_html: str | None = model_meta.get("description_html")
        m.description_html = desc_html
        char_limit = 160
        if desc_html:
            short = _strip_html(desc_html)
            m.description_short = (short[:char_limit] + "…") if len(short) > char_limit else short
        else:
            m.description_short = None

        if not with_schema or m.relation not in cols_by_table:
            continue
        rel_desc_map = (docs_meta.get("columns", {}) or {}).get(m.relation, {})
        mdl_desc_map = model_meta.get("columns") or {}
        for c in cols_by_table[m.relation]:
            c.description_html = rel_desc_map.get(c.name) or mdl_desc_map.get(c.name)


def _infer_and_attach_lineage(
    models: list[ModelDoc],
    executor: Any | None,
    docs_meta: dict[str, Any],
    cols_by_table: dict[str, list[ColumnInfo]],
    *,
    with_schema: bool,
) -> None:
    """Best-effort Lineage ermitteln (SQL/Python) und auf Columns mappen."""
    for m in models:
        inferred: dict[str, list[dict[str, Any]]] = {}
        try:
            if m.kind == "sql" and executor is not None:
                try:
                    rendered = executor.render_sql(
                        REGISTRY.nodes[m.name],
                        REGISTRY.env,
                        ref_resolver=lambda nm: executor._resolve_ref(nm, REGISTRY.env),
                        source_resolver=executor._resolve_source,
                    )
                except Exception:
                    rendered = None
                if rendered:
                    inferred = infer_sql_lineage(rendered)
                    overrides = parse_sql_lineage_overrides(rendered)
                    inferred = merge_lineage(inferred, overrides)
            elif m.kind == "python":
                func = getattr(REGISTRY, "py_funcs", {}).get(m.name)
                inferred = infer_py_lineage(func)
        except Exception:
            inferred = {}

        # YAML overrides (bereits in docs_meta gemerged)
        model_meta = (
            (docs_meta.get("models", {}) or {}).get(m.name, {})
            if isinstance(docs_meta, dict)
            else {}
        )
        ylin = model_meta.get("lineage") if isinstance(model_meta, dict) else None
        if isinstance(ylin, dict):
            # Normalisieren: { out_col: {from:[{table,col}], transformed:bool} }
            norm: dict[str, list[dict[str, Any]]] = {}
            for out_col, spec in ylin.items():
                if not isinstance(spec, dict):
                    continue
                transformed_flag = bool(spec.get("transformed"))
                items: list[dict[str, Any]] = []
                for s in spec.get("from", []) or []:
                    if isinstance(s, dict) and "table" in s and "column" in s:
                        items.append(
                            {
                                "from_relation": s["table"],
                                "from_column": s["column"],
                                "transformed": transformed_flag,
                            }
                        )
                if items:
                    norm[out_col] = items
            if norm:
                inferred = merge_lineage(inferred, norm)

        if with_schema and (m.relation in cols_by_table) and inferred:
            for c in cols_by_table[m.relation]:
                if c.name in inferred:
                    c.lineage = inferred[c.name]


def _reverse_deps(nodes: dict[str, Node]) -> dict[str, list[str]]:
    rev: dict[str, list[str]] = {n: [] for n in nodes}
    for n in nodes.values():
        for d in n.deps or []:
            if d in rev:
                rev[d].append(n.name)
    return {k: sorted(v) for k, v in rev.items()}


def _render_index(env: Environment, out_dir: Path, **ctx: Any) -> None:
    html = env.get_template("index.html.j2").render(**ctx)
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def _render_model_pages(
    env: Environment,
    out_dir: Path,
    models: list[ModelDoc],
    used_by: dict[str, list[str]],
    cols_by_table: dict[str, list[ColumnInfo]],
    materialization_legend: dict[str, dict[str, str]],
    macros: list[dict[str, str]],
    model_sources: dict[str, list[tuple[str, str]]],
    sources_index: dict[tuple[str, str], SourceDoc],
) -> None:
    tmpl = env.get_template("model.html.j2")
    for m in models:
        phys = relation_for(m.name)
        cols = cols_by_table.get(phys, [])

        # Provide a 'this' context for templates that expect it.
        # Matches what SQL-rendering exposes: name, materialized,
        # (schema/database best-effort), and 'incremental'.
        this_ctx = {
            "name": phys,
            "materialized": m.materialized,
            "schema": None,  # optional: could derive from executor if you want
            "database": None,  # optional: could derive from executor if you want
            "incremental": (str(m.materialized).lower() == "incremental"),
        }

        source_refs = [
            sources_index[key] for key in model_sources.get(m.name, []) if key in sources_index
        ]

        html = tmpl.render(
            m=m,
            used_by=used_by.get(m.name, []),
            cols=cols,
            macros=macros,
            materialization_legend=materialization_legend,
            this=this_ctx,
            sources_used=source_refs,
        )
        (out_dir / f"{_safe_filename(m.name)}.html").write_text(html, encoding="utf-8")


def _render_source_pages(env: Environment, out_dir: Path, sources: list[SourceDoc]) -> None:
    """
    Render per-source HTML pages if the template 'source.html.j2' exists.
    Safe no-op otherwise.
    """
    try:
        tmpl = env.get_template("source.html.j2")
    except TemplateNotFound:
        return

    for s in sources:
        html = tmpl.render(source=s)
        fname = s.doc_filename
        if not fname:
            safe = _safe_filename(f"{s.source_name}.{s.table_name}")
            fname = f"source_{safe}.html"
        (out_dir / fname).write_text(html, encoding="utf-8")


def render_site(
    out_dir: Path,
    nodes: dict[str, Node],
    executor: Any | None = None,
    *,
    with_schema: bool = True,
    spa: bool = True,
    legacy_pages: bool = False,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _copy_template_assets(out_dir)
    env = _init_jinja()
    source_consumers, model_source_refs = _scan_source_refs(nodes)

    sources = _collect_sources()
    _attach_consumers_to_sources(sources, source_consumers)
    sources_by_key = {(s.source_name, s.table_name): s for s in sources}
    source_link_meta = {
        key: {"label": f"{doc.source_name}.{doc.table_name}", "file": doc.doc_filename}
        for key, doc in sources_by_key.items()
    }

    mermaid_src = dag_mermaid(
        nodes, source_links=source_link_meta, model_source_refs=model_source_refs
    )
    proj_dir = _get_project_dir()
    docs_meta = read_docs_metadata(proj_dir) if proj_dir else {"models": {}, "columns": {}}
    models = _collect_models(nodes)
    mat_legend = _materialization_legend()
    macro_list = _build_macro_list(proj_dir)
    cols_by_table = _collect_columns(executor) if (executor and with_schema) else {}

    _apply_descriptions_to_models(models, docs_meta, cols_by_table, with_schema=with_schema)
    _infer_and_attach_lineage(models, executor, docs_meta, cols_by_table, with_schema=with_schema)

    used_by = _reverse_deps(nodes)

    if spa:
        _copy_template_assets(out_dir)
        proj_name = _project_name(proj_dir)
        env_name = getattr(REGISTRY, "active_engine", None)  # best-effort, not perfect
        manifest = _build_spa_manifest(
            proj_name=proj_name,
            env_name=env_name,
            with_schema=with_schema,
            mermaid_src=str(mermaid_src),
            models=models,
            sources=sources,
            macros=macro_list,
            used_by=used_by,
            cols_by_table=cols_by_table,
            model_source_refs=model_source_refs,
            sources_by_key=sources_by_key,
        )
        assets_dir = out_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "docs_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        # SPA shell (index.html.j2)
        _render_index(
            env,
            out_dir,
            project_name=proj_name,
            manifest_path="assets/docs_manifest.json",
        )

        # Optional legacy pages (useful during transition)
        if legacy_pages:
            _render_model_pages(
                env,
                out_dir,
                models=models,
                used_by=used_by,
                cols_by_table=cols_by_table,
                materialization_legend=mat_legend,
                macros=macro_list,
                model_sources=model_source_refs,
                sources_index=sources_by_key,
            )
            _render_source_pages(env, out_dir, sources)
    else:
        # Legacy behavior
        _render_index(
            env,
            out_dir,
            mermaid_src=Markup(mermaid_src),
            models=models,
            sources=sources,
            materialization_legend=mat_legend,
            macros=macro_list,
        )
        _render_model_pages(
            env,
            out_dir,
            models=models,
            used_by=used_by,
            cols_by_table=cols_by_table,
            materialization_legend=mat_legend,
            macros=macro_list,
            model_sources=model_source_refs,
            sources_index=sources_by_key,
        )
        _render_source_pages(env, out_dir, sources)


def read_docs_metadata(project_dir: Path) -> dict[str, Any]:
    """
    Merge YAML + Markdown descriptions with priority: Markdown > YAML.
    Returns:
      {
        "models": {
          <model>: {
            "description_html": "<p>…</p>" | None,
            "columns": { <col>: "<p>…</p>" }
          },
        },
        "columns": { <relation>: { <col>: "<p>…</p>" } }
      }
    """
    # 1) YAML (from project.yml → docs.models)
    yaml_models = _read_project_yaml_docs(project_dir)  # {model: {description, columns{}}}
    out_models: dict[str, dict[str, Any]] = {}
    for model, meta in yaml_models.items() if isinstance(yaml_models, dict) else []:
        desc = (meta or {}).get("description")
        cols = (meta or {}).get("columns") or {}
        lineage_yaml = (meta or {}).get("lineage")

        out_models[model] = {
            "description_html": _render_minimarkdown(desc) if desc else None,
            "columns": {
                str(k): _render_minimarkdown(str(v))
                for k, v in (cols.items() if isinstance(cols, dict) else [])
            },
        }
        if isinstance(lineage_yaml, dict):
            out_models[model]["lineage"] = lineage_yaml

    # 2) Markdown model overrides: docs/models/<model>.md
    md_models_dir = project_dir / "docs" / "models"
    if md_models_dir.exists():
        for p in md_models_dir.glob("*.md"):
            model_name = p.stem
            _, body = _read_markdown_file(p)
            if body.strip():
                out_models.setdefault(model_name, {"description_html": None, "columns": {}})
                out_models[model_name]["description_html"] = _render_minimarkdown(body)

    # 3) Markdown column overrides: docs/columns/<relation>/<column>.md
    out_columns: dict[str, dict[str, str]] = {}
    cols_root = project_dir / "docs" / "columns"
    if cols_root.exists():
        for rel_dir in cols_root.iterdir():
            if not rel_dir.is_dir():
                continue
            rel = rel_dir.name
            for p in rel_dir.glob("*.md"):
                col = p.stem
                _, body = _read_markdown_file(p)
                if body.strip():
                    out_columns.setdefault(rel, {})[col] = _render_minimarkdown(body)

    return {"models": out_models, "columns": out_columns}
