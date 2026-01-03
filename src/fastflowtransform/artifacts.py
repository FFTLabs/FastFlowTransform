# fastflowtransform/artifacts.py
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastflowtransform.core import REGISTRY, relation_for

# ---------- Paths ----------


def _target_dir(project_dir: Path) -> Path:
    """Return artifact target directory under the project."""
    d = (project_dir / ".fastflowtransform" / "target").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rel(p: Path, base: Path) -> str:
    """Return path p relative to base as posix string."""
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except Exception:
        return p.name


def _rel_safe(p_like: Any, base: Path) -> str:
    """
    Best-effort relative path rendering.
    Accepts None/str/Path; returns empty string on None or conversion failure.
    """
    if p_like is None:
        return ""
    try:
        if isinstance(p_like, Path):
            return _rel(p_like, base)
        # Try to coerce strings / os.PathLike to Path
        return _rel(Path(str(p_like)), base)
    except Exception:
        try:
            return str(p_like) if p_like is not None else ""
        except Exception:
            return ""


def _iso_now() -> str:
    """UTC timestamp with seconds precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_dump(path: Path, obj: Any) -> None:
    """
    Write JSON deterministically (sorted keys) with a trailing newline.
    Pretty-print by default (indent=2). Set FFT_ARTIFACTS_PRETTY=0 to use compact form.
    """
    pretty_env = os.getenv("FFT_ARTIFACTS_PRETTY", "1").lower()
    pretty = pretty_env not in {"0", "false", "no"}
    if pretty:
        txt = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2)
    else:
        txt = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    path.write_text(txt + "\n", encoding="utf-8")


# ---------- MANIFEST ----------


def write_manifest(project_dir: Path) -> Path:
    """
    Write manifest.json with minimal compatibility:
      - nodes: {name, path, deps, materialized, relation, kind}
      - macros: {name -> path}
      - sources: verbatim REGISTRY.sources
      - generated_at
    """
    project_dir = Path(project_dir)
    out_dir = _target_dir(project_dir)
    manifest_path = out_dir / "manifest.json"

    nodes = {}
    for name, node in sorted(REGISTRY.nodes.items(), key=lambda x: x[0]):
        nodes[name] = {
            "name": name,
            "kind": node.kind,
            # Be resilient to stubbed Nodes in tests (path may be None)
            "path": _rel_safe(getattr(node, "path", None), project_dir),
            "deps": sorted(list(node.deps or [])),
            "materialized": (node.meta or {}).get("materialized", "table"),
            "relation": relation_for(name),
        }

    macros = {}
    for mname, mpath in sorted(REGISTRY.macros.items(), key=lambda x: x[0]):
        macros[mname] = _rel_safe(mpath, project_dir)

    data = {
        "metadata": {
            "tool": "fastflowtransform",
            "generated_at": _iso_now(),
        },
        "nodes": nodes,
        "macros": macros,
        "sources": REGISTRY.sources or {},
    }
    _json_dump(manifest_path, data)
    return manifest_path


# ---------- RUN RESULTS ----------


@dataclass
class RunNodeResult:
    name: str
    status: str  # "success" | "error" | "skipped"
    started_at: str
    finished_at: str
    duration_ms: int
    message: str | None = None
    http: dict | None = None

    # per-node query stats (aggregated across all SQL queries)
    bytes_scanned: int | None = None
    rows: int | None = None
    query_duration_ms: int | None = None


def write_run_results(
    project_dir: Path,
    *,
    started_at: str,
    finished_at: str,
    node_results: list[RunNodeResult],
    budgets: dict[str, Any] | None = None,
) -> Path:
    """
    Write run_results.json containing run envelope and per-node results.
    Optionally includes a 'budgets' summary block.
    """
    project_dir = Path(project_dir)
    out_dir = _target_dir(project_dir)
    results_path = out_dir / "run_results.json"

    data = {
        "metadata": {"tool": "fastflowtransform", "generated_at": _iso_now()},
        "run_started_at": started_at,
        "run_finished_at": finished_at,
        "results": [asdict(nr) for nr in sorted(node_results, key=lambda r: r.name)],
    }

    if budgets is not None:
        data["budgets"] = budgets

    _json_dump(results_path, data)
    return results_path


# ---------- CATALOG ----------


def _duckdb_columns(con: Any, table: str) -> list[dict[str, Any]]:
    """Return column metadata for a DuckDB table, with robust fallback."""
    # Prefer information_schema with a schema filter for reliability
    sql_info = """
      select column_name as name, data_type as dtype, is_nullable
      from information_schema.columns
      where table_schema in ('main','temp')
        and lower(table_name)=lower(?)
      order by ordinal_position
    """
    rows = con.execute(sql_info, [table]).fetchall()
    if rows:
        return [{"name": r[0], "dtype": r[1], "nullable": str(r[2]).lower() == "yes"} for r in rows]

    # Fallback: PRAGMA table_info(<ident>) — quote identifier safely
    # DuckDB pragma returns: cid, name, type, notnull, dflt_value, pk
    def _q_ident(ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    pragma_sql = f"PRAGMA table_info({_q_ident(table)})"
    rows2 = con.execute(pragma_sql).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows2:
        name = r[1]
        dtype = r[2]
        nullable = r[3] == 0  # notnull == 0 → nullable True
        out.append({"name": name, "dtype": dtype, "nullable": nullable})
    return out


def _postgres_columns(con: Any, table: str, schema: str | None = None) -> list[dict[str, Any]]:
    sch = schema or "public"
    sql = """
      select column_name, data_type, is_nullable
      from information_schema.columns
      where table_schema=%s and lower(table_name)=lower(%s)
      order by ordinal_position
    """
    rows = con.execute(sql, (sch, table)).fetchall()
    return [{"name": r[0], "dtype": r[1], "nullable": (r[2] == "YES")} for r in rows]


def _spark_columns(spark: Any, table: str) -> list[dict[str, Any]]:
    """
    Retrieve column metadata for Spark/Databricks tables.
    Supports names with optional schema/catalog.
    """
    db = None
    tbl = table
    cat = None
    parts = table.split(".")

    if len(parts) == 2:
        db, tbl = parts
    elif len(parts) >= 3:
        cat, db, tbl = parts[-3], parts[-2], parts[-1]

    def _list_cols(target_tbl: str, target_db: str | None) -> list[dict[str, Any]]:
        ident = target_tbl if not target_db else f"{target_db}.{target_tbl}"
        try:
            cols = spark.catalog.listColumns(ident)
        except TypeError:
            cols = spark.catalog.listColumns(target_tbl, target_db)
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for c in cols:
            name = getattr(c, "name", None)
            if not name:
                continue
            dtype = str(getattr(c, "dataType", ""))
            nullable = bool(getattr(c, "nullable", False))
            out.append({"name": name, "dtype": dtype, "nullable": nullable})
        return out

    cols = _list_cols(tbl, db)
    if cols:
        return cols
    # Fallback: try fully qualified view via Spark SQL
    ident = tbl
    if db:
        ident = f"`{db}`.`{tbl}`"
    if cat and db:
        ident = f"`{cat}`.`{db}`.`{tbl}`"
    try:
        df = spark.table(ident)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for field in df.schema.fields:
        dtype = (
            field.dataType.simpleString()
            if hasattr(field.dataType, "simpleString")
            else str(field.dataType)
        )
        out.append({"name": field.name, "dtype": dtype, "nullable": field.nullable})
    return out


def _try_columns_for(executor: Any, table: str) -> list[dict[str, Any]]:
    """
    Best-effort column introspection for known engines. Returns [] if unsupported.
    """
    spark = getattr(executor, "spark", None)
    if spark is not None:
        cols = _spark_columns(spark, table)
        if cols:
            return cols
    con = getattr(executor, "con", None)
    # DuckDB detection (robust): class/module name contains 'duckdb'
    try:
        if con and "duckdb" in getattr(con.__class__, "__module__", "").lower():
            return _duckdb_columns(con, table)
    except Exception:
        # Never raise from catalog collection — return empty and let caller proceed
        return []
    # Postgres via psycopg connection inside executor (optional)
    try:
        schema = getattr(executor, "schema", None)
        if con and hasattr(con, "execute") and hasattr(con, "fetchall"):
            return _postgres_columns(con, table, schema=schema)
    except Exception:
        pass
    return []


def write_catalog(project_dir: Path, executor: Any) -> Path:
    """
    Write catalog.json:
      - relations: map of relation -> {columns:[{name,dtype,nullable}]}
    """
    project_dir = Path(project_dir)
    out_dir = _target_dir(project_dir)
    catalog_path = out_dir / "catalog.json"

    relations: dict[str, Any] = {}
    rel_names = sorted([relation_for(n) for n in REGISTRY.nodes])
    for rel in rel_names:
        # Per-relation guard: introspection must never break artifact emission
        try:
            cols = _try_columns_for(executor, rel)
        except Exception:
            cols = []
        relations[rel] = {"columns": cols}

    data = {
        "metadata": {"tool": "fastflowtransform", "generated_at": _iso_now()},
        "relations": relations,
    }
    _json_dump(catalog_path, data)
    return catalog_path


# ---------- READ DURATIONS ----------


def load_last_run_durations(project_dir: Path) -> dict[str, float]:
    """
    Best-effort reader for the last run_results.json.

    Returns: { model_name: duration_in_seconds }.
    On any error or missing file: {}.
    """
    path = _target_dir(project_dir)
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    # tolerate a few possible shapes
    items: list[dict[str, Any]] = (
        raw.get("results") or raw.get("node_results") or raw.get("nodes") or []
    )

    out: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        dur_ms = item.get("duration_ms")
        if isinstance(name, str) and isinstance(dur_ms, (int, float)):
            out[name] = float(dur_ms) / 1000.0
    return out


# ---------- TEST RESULTS ----------


@dataclass
class TestResult:
    kind: str
    table: str  # display label (may include arrows for relationships)
    relation: str | None  # machine-join key (best-effort; usually the tested table)
    column: str | None
    ok: bool
    severity: str  # "error" | "warn"
    duration_ms: int
    msg: str | None = None
    param_str: str = ""
    example_sql: str | None = None


def write_test_results(
    project_dir: Path,
    *,
    started_at: str,
    finished_at: str,
    results: list[TestResult],
) -> Path:
    """
    Write test_results.json containing a run envelope + individual test outcomes.
    """
    project_dir = Path(project_dir)
    out_dir = _target_dir(project_dir)
    path = out_dir / "test_results.json"

    data = {
        "metadata": {"tool": "fastflowtransform", "generated_at": _iso_now()},
        "test_started_at": started_at,
        "test_finished_at": finished_at,
        "results": [asdict(r) for r in results],
    }
    _json_dump(path, data)
    return path


# ---------- UNIT TEST RESULTS ----------


@dataclass
class UTestResult:
    model: str
    case: str
    status: str  # "pass" | "fail" | "error" | "skip"
    duration_ms: int
    cache_hit: bool = False

    message: str | None = None
    target_relation: str | None = None
    spec_path: str = ""


def write_utest_results(
    project_dir: Path,
    *,
    started_at: str,
    finished_at: str,
    failures: int,
    results: list[UTestResult],
    engine: str | None = None,
) -> Path:
    """
    Write utest_results.json containing a run envelope + per-case results.
    """
    project_dir = Path(project_dir)
    out_dir = _target_dir(project_dir)
    path = out_dir / "utest_results.json"

    data = {
        "metadata": {"tool": "fastflowtransform", "generated_at": _iso_now()},
        "utest_started_at": started_at,
        "utest_finished_at": finished_at,
        "engine": engine or "",
        "failures": int(failures or 0),
        "results": [asdict(r) for r in results],
    }
    _json_dump(path, data)
    return path
