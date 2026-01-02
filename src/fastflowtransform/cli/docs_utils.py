# fastflowtransform/cli/docs_utils.py
from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from fastflowtransform import lineage as lineage_mod
from fastflowtransform.core import REGISTRY, relation_for
from fastflowtransform.docs import _collect_columns, read_docs_metadata


def _resolve_dag_out_dir(proj: Path, override: Path | None) -> Path:
    if override:
        return override.expanduser().resolve()
    cfg_path = proj / "project.yml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    p = (cfg or {}).get("docs", {}).get("dag_dir")
    if p:
        return (proj / p).expanduser().resolve()
    return (proj / "site" / "dag").resolve()


def _strip_html(s: str | None) -> str | None:
    if not s:
        return None
    txt = re.sub(r"<[^>]+>", "", s)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or None


def _infer_sql_ref_aliases(rendered_sql: str) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    pattern = re.compile(
        r"\b(from|join)\s+([A-Za-z0-9_.\"`]+)\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*)",
        flags=re.IGNORECASE,
    )
    for _, rel, alias in pattern.findall(rendered_sql):
        rel_clean = rel.strip('"`')
        alias_map[alias] = rel_clean
    return alias_map


def _build_docs_manifest(
    project_dir: Path,
    nodes: dict[str, Any],
    executor: Any,
    env_name: str,
) -> dict[str, Any]:
    proj_yaml = project_dir / "project.yml"
    project_name = str(project_dir.name)
    try:
        cfg = yaml.safe_load(proj_yaml.read_text(encoding="utf-8")) if proj_yaml.exists() else {}
        if isinstance(cfg, dict) and cfg.get("name"):
            project_name = str(cfg["name"])
    except Exception:
        pass

    rev: dict[str, list[str]] = {k: [] for k in nodes}
    for n in nodes.values():
        for d in n.deps or []:
            if d in rev:
                rev[d].append(n.name)

    cols_by_table = _collect_columns(executor) if executor else {}
    docs_meta = read_docs_metadata(project_dir)

    models_out: list[dict[str, Any]] = []
    for n in sorted(nodes.values(), key=lambda x: x.name):
        relation = relation_for(n.name)
        mat = (getattr(n, "meta", {}) or {}).get("materialized", "table")

        model_doc = (
            (docs_meta.get("models", {}) or {}).get(n.name, {})
            if isinstance(docs_meta, dict)
            else {}
        )
        model_desc_html = model_doc.get("description_html")
        model_desc_txt = _strip_html(model_desc_html)

        cols = []
        col_desc_map = model_doc.get("columns", {}) or {}
        lineage_map: dict[str, list[dict[str, Any]]] = {}
        try:
            if n.kind == "sql" and hasattr(executor, "render_sql"):
                rendered = executor.render_sql(
                    n,
                    REGISTRY.env,
                    ref_resolver=lambda nm: executor._resolve_ref(nm, REGISTRY.env),
                    source_resolver=executor._resolve_source,
                )
                alias_map = _infer_sql_ref_aliases(rendered)
                lineage_map = lineage_mod.infer_sql_lineage(rendered, alias_map)
            elif n.kind == "python":
                func = REGISTRY.py_funcs[n.name]
                src = inspect.getsource(func)
                lineage_map = lineage_mod.infer_py_lineage(src, getattr(n, "requires", None))
        except Exception:
            lineage_map = {}

        for c in cols_by_table.get(relation, []):
            col_desc_html = None
            rel_map = (docs_meta.get("columns", {}) or {}).get(relation, {})
            if isinstance(rel_map, dict):
                col_desc_html = rel_map.get(c.name) or col_desc_map.get(c.name)
            else:
                col_desc_html = col_desc_map.get(c.name)
            col_desc_txt = _strip_html(col_desc_html)

            cols.append(
                {
                    "name": c.name,
                    "dtype": c.dtype,
                    "nullable": bool(c.nullable),
                    "description": col_desc_txt,
                    "lineage": lineage_map.get(c.name, []),
                }
            )

        models_out.append(
            {
                "name": n.name,
                "relation": relation,
                "materialized": mat,
                "description": model_desc_txt,
                "columns": cols,
                "depends_on": list(n.deps or []),
                "used_by": sorted(rev.get(n.name, [])),
            }
        )

    return {
        "project": project_name,
        "generated_at": datetime.now(UTC).isoformat(),
        "models": models_out,
    }


__all__ = [
    "_build_docs_manifest",
    "_infer_sql_ref_aliases",
    "_resolve_dag_out_dir",
    "_strip_html",
]
