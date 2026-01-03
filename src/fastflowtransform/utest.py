# src/fastflowtransform/utest.py
import datetime
import difflib
import hashlib
import json
import os
from collections.abc import Generator, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, cast

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fastflowtransform.cache import FingerprintCache, can_skip_node
from fastflowtransform.fingerprint import (
    EnvCtx,
    build_env_ctx,
    fingerprint_py,
    fingerprint_sql,
    get_function_source,
)
from fastflowtransform.meta import delete_meta_for_node

from .core import REGISTRY, Node, relation_for

# ---------- Specifications ----------


class UnitInput(BaseModel):
    """Single relation input: either inline rows or a CSV file."""

    model_config = ConfigDict(extra="forbid")

    rows: list[dict[str, Any]] | None = None
    csv: str | None = None


class UnitExpect(BaseModel):
    """
    Expected result configuration for a unit-test case.

    Extra keys are forbidden so YAML specs are tightly validated.
    """

    model_config = ConfigDict(extra="forbid")

    relation: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    order_by: list[str] | None = None
    any_order: bool = False
    approx: dict[str, float] | None = None
    ignore_columns: list[str] | None = None
    subset: bool = False


class UnitDefaults(BaseModel):
    """Defaults that apply to all cases in a spec unless overridden."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, UnitInput] = Field(default_factory=dict)
    expect: UnitExpect = Field(default_factory=UnitExpect)


class UnitCase(BaseModel):
    """A single unit-test case within a spec."""

    model_config = ConfigDict(extra="forbid")

    name: str
    inputs: dict[str, UnitInput] = Field(default_factory=dict)
    expect: UnitExpect = Field(default_factory=UnitExpect)


class UnitSpec(BaseModel):
    """
    Top-level unit-test specification loaded from YAML.

    `path` and `project_dir` are runtime-only and are not populated from YAML
    (we set them in discovery).
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    engine: str | None = None
    defaults: UnitDefaults = Field(default_factory=UnitDefaults)
    cases: list[UnitCase] = Field(default_factory=list)

    path: Path | None = Field(default=None, exclude=True)
    project_dir: Path | None = Field(default=None, exclude=True)

    # ---- defaults merging helpers -------------------------------------
    def _merge_expect(self, case_expect: UnitExpect) -> UnitExpect:
        """
        Merge spec-level default.expect with case.expect.

        Only fields explicitly set on the case override the defaults.
        """
        base = self.defaults.expect.model_dump()
        override = case_expect

        for field_name in override.model_fields_set:
            base[field_name] = getattr(override, field_name)

        return UnitExpect(**base)

    def _merge_inputs(self, case_inputs: dict[str, UnitInput]) -> dict[str, UnitInput]:
        """
        Merge spec-level default.inputs with case.inputs (case wins per relation).
        """
        merged: dict[str, UnitInput] = dict(self.defaults.inputs)
        merged.update(case_inputs or {})
        return merged

    def merged_case(self, case: UnitCase) -> UnitCase:
        """
        Return a new UnitCase where defaults have been applied (inputs + expect).
        """
        return UnitCase(
            name=case.name,
            inputs=self._merge_inputs(case.inputs),
            expect=self._merge_expect(case.expect),
        )


def discover_unit_specs(
    project_dir: Path, path: str | None = None, only_model: str | None = None
) -> list[UnitSpec]:
    files = [Path(path)] if path else list((project_dir / "tests" / "unit").glob("*.yml"))
    specs: list[UnitSpec] = []

    for f in files:
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if not raw:
            continue

        try:
            spec = UnitSpec.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid unit-test spec {f}: {exc}") from exc

        if only_model and spec.model != only_model:
            continue

        # Attach runtime fields
        spec.path = f
        spec.project_dir = project_dir.resolve()
        specs.append(spec)

    return specs


# ---------- Input loaders ----------


def _load_relation_from_rows(executor: Any, rel: str, rows: list[dict]) -> None:
    """
    Delegate loading test-input rows to the executor's utest helper.
    """
    if not hasattr(executor, "utest_load_relation_from_rows"):
        raise RuntimeError(
            f"Unit tests: executor of type {type(executor).__name__} "
            "does not implement utest_load_relation_from_rows()."
        )
    executor.utest_load_relation_from_rows(rel, rows)


def _load_relation_from_csv(executor: Any, rel: str, csv_path: Path) -> None:
    df = pd.read_csv(csv_path)
    _load_relation_from_rows(executor, rel, df.to_dict(orient="records"))


def _read_result(executor: Any, rel: str) -> pd.DataFrame:
    """
    Delegate reading result relation to the executor's utest helper.
    """
    if not hasattr(executor, "utest_read_relation"):
        raise RuntimeError(
            f"Unit tests: executor of type {type(executor).__name__} "
            "does not implement utest_read_relation()."
        )
    return executor.utest_read_relation(rel)


def _project_root_for_spec(spec: UnitSpec) -> Path:
    if getattr(REGISTRY, "project_dir", None):
        return Path(REGISTRY.get_project_dir()).resolve()
    if spec.path is None:
        proj = spec.project_dir
        return proj.resolve() if isinstance(proj, Path) else Path.cwd()
    p = spec.path.resolve()
    for parent in [p.parent, *list(p.parents)]:
        if (parent / "models").is_dir():
            return parent
    return spec.path.parent


# ---------- Cache and Fingerprint Helpers ----------


def _normalize_for_hash(obj: Any) -> Any:
    """Deterministic, JSON-serializable shape: dicts sorted, tuples/sets normalized."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, (list, tuple, set)):
        # Preserve list order; sort sets/tuples into a list for determinism
        if isinstance(obj, list):
            return [_normalize_for_hash(x) for x in obj]
        return sorted(
            [_normalize_for_hash(x) for x in obj], key=lambda x: json.dumps(x, sort_keys=True)
        )
    if isinstance(obj, dict):
        return {k: _normalize_for_hash(obj[k]) for k in sorted(obj.keys())}
    # Fallback to string representation (stable enough for primitives we don't expect here)
    return str(obj)


def _sha256_hex(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _digest_file(path: Path) -> str | None:
    """Return SHA-256 hex of file contents if readable; else None."""
    try:
        data = path.read_bytes()
    except Exception:
        return None
    return _sha256_hex(data)


def _resolve_csv_path(spec: Any, csv_val: str) -> Path:
    """
    Resolve CSV path for utests with robust fallbacks:
      1) absolute path → return as-is
      2) spec.path's parent (YAML dir)
      3) spec.project_dir (project root)
      4) derived project root via _project_root_for_spec(spec)
      5) current working directory

    We return the first candidate that exists; if none exist, return the first candidate anyway.
    """
    p = Path(csv_val)
    if p.is_absolute():
        return p

    candidates: list[Path] = []

    # 1) YAML file directory
    sp = getattr(spec, "path", None)
    if sp:
        with suppress(Exception):
            candidates.append(Path(sp).parent / p)

    # 2) Explicit project_dir
    proj_dir = getattr(spec, "project_dir", None)
    if proj_dir:
        candidates.append(Path(proj_dir) / p)

    # 3) Heuristic project root
    with suppress(Exception):
        candidates.append(_project_root_for_spec(spec) / p)

    # 4) CWD
    candidates.append(Path.cwd() / p)

    for c in candidates:
        if c.exists():
            return c.resolve()

    # If nothing exists, return best guess (first candidate) to surface a clear error upstream
    return (candidates[0] if candidates else (Path.cwd() / p)).resolve()


def _fingerprint_case_inputs(spec: UnitSpec, case: UnitCase) -> str:
    """
    Compute a deterministic fingerprint of the EFFECTIVE inputs for a case.
    Merges spec.defaults.inputs and case.inputs (case overrides), then:
      - For rows: include normalized rows.
      - For csv: include the resolved path AND its file content digest if available.
    """
    norm: dict[str, Any] = {}
    for rel, cfg in (case.inputs or {}).items():
        item: dict[str, Any] = {}

        # Pydantic model from spec/case
        if isinstance(cfg, UnitInput):
            if cfg.rows is not None:
                item["rows"] = _normalize_for_hash(cfg.rows)
            if cfg.csv:
                csv_path = _resolve_csv_path(spec, cfg.csv)
                item["csv_path"] = csv_path.as_posix()
                file_hash = _digest_file(csv_path)
                if file_hash:
                    item["csv_sha256"] = file_hash
                else:
                    item.setdefault("csv_unreadable", True)
        # Defensive fallback: mapping-like config
        elif isinstance(cfg, Mapping):
            if "rows" in cfg:
                item["rows"] = _normalize_for_hash(cfg["rows"])
            if "csv" in cfg and isinstance(cfg["csv"], str):
                csv_path = _resolve_csv_path(spec, cfg["csv"])
                item["csv_path"] = csv_path.as_posix()
                file_hash = _digest_file(csv_path)
                if file_hash:
                    item["csv_sha256"] = file_hash
                else:
                    item.setdefault("csv_unreadable", True)
        else:
            # Unknown shape: include normalized raw value
            item["value"] = _normalize_for_hash(cfg)
        norm[rel] = item

    payload = {"inputs": _normalize_for_hash(norm)}
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return _sha256_hex(data)


# ---------- Assertions ----------


class UnitAssertionFailure(Exception):
    pass


def assert_rows_equal(
    actual_df: pd.DataFrame,
    expect_rows: list[dict],
    *,
    order_by: list[str] | None = None,
    any_order: bool = False,
    approx: dict[str, float] | None = None,
    ignore_columns: list[str] | None = None,
    subset: bool = False,
) -> None:
    exp = pd.DataFrame(expect_rows)

    actual_df, exp = _drop_ignored_columns(actual_df, exp, ignore_columns)
    _assert_columns_present(actual_df, exp, subset)

    actual_df, exp = _apply_ordering(actual_df, exp, order_by, any_order)
    # Apply numeric approximations (align actual to expected within tolerances)
    if approx:
        _apply_approx_equalization(actual_df, exp, approx)

    if subset:
        _assert_subset_present(actual_df, exp)
        return

    _assert_exact_equal(actual_df, exp)


# ----------------- Helfer -----------------


def _drop_ignored_columns(
    actual_df: pd.DataFrame,
    exp: pd.DataFrame,
    ignore_columns: list[str] | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not ignore_columns:
        return actual_df, exp

    cols_actual = [c for c in ignore_columns if c in actual_df.columns]
    cols_exp = [c for c in ignore_columns if c in exp.columns]

    if cols_actual:
        actual_df = actual_df.drop(columns=cols_actual, errors="ignore")
    if cols_exp:
        exp = exp.drop(columns=cols_exp, errors="ignore")
    return actual_df, exp


def _assert_columns_present(actual_df: pd.DataFrame, exp: pd.DataFrame, subset: bool) -> None:
    if subset:
        return
    missing = set(exp.columns) - set(actual_df.columns)
    if missing:
        raise UnitAssertionFailure(f"Missing columns in actual: {sorted(missing)}")


def _apply_ordering(
    actual_df: pd.DataFrame,
    exp: pd.DataFrame,
    order_by: list[str] | None,
    any_order: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if order_by:
        actual_df = actual_df.sort_values(order_by).reset_index(drop=True)
        exp = exp.sort_values(order_by).reset_index(drop=True)
        return actual_df, exp

    if any_order:
        common = sorted(set(exp.columns) & set(actual_df.columns))
        if common:
            actual_df = actual_df.sort_values(common).reset_index(drop=True)
            exp = exp.sort_values(common).reset_index(drop=True)

    return actual_df, exp


def _apply_approx_equalization(
    actual_df: pd.DataFrame,
    exp: pd.DataFrame,
    approx: dict[str, float],
) -> list[str]:
    """Compares numeric columns with tolerance."""
    checked: list[str] = []
    for col, tol in approx.items():
        if col not in exp.columns or col not in actual_df.columns:
            continue

        try:
            tol_f = float(tol)
        except (TypeError, ValueError) as err:
            raise UnitAssertionFailure(
                f"Invalid approx tolerance for column '{col}': {tol!r} (must be a number)"
            ) from err

        a_num = pd.to_numeric(actual_df[col], errors="coerce")
        e_num = pd.to_numeric(exp[col], errors="coerce")

        diff = (a_num - e_num).abs().fillna(0)
        bad = diff > tol_f
        if bad.any():
            raise UnitAssertionFailure(
                f"Approx mismatch in '{col}' (tol={tol_f}). "
                f"expected={e_num[bad].tolist()} vs actual={a_num[bad].tolist()}"
            )

        actual_df[col] = exp[col]
        checked.append(col)

    return checked


def _assert_subset_present(actual_df: pd.DataFrame, exp: pd.DataFrame) -> None:
    if exp.empty:
        return

    key_cols = list(exp.columns)
    exp_rows = _rows_as_tuples(exp, key_cols)
    act_rows = _rows_as_tuples(actual_df, key_cols)

    missing: list[tuple] = [r for r in exp_rows if r not in act_rows]
    if missing:
        raise UnitAssertionFailure(f"Expected row {missing[0]} not found in actual")


def _rows_as_tuples(df: pd.DataFrame, key_cols: Iterable[str]) -> list[tuple]:
    idx_range = range(len(df))
    return [tuple(df[c].iloc[i] if c in df.columns else None for c in key_cols) for i in idx_range]


def _normalize_cell_for_compare(v: Any) -> Any:
    """Normalize individual cell values so that semantically equal values compare equal."""
    # Treat NaNs / None uniformly
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "__NA__"

    # pandas.Timestamp
    if isinstance(v, pd.Timestamp):
        # to_pydatetime() → datetime, then .date() → date
        return v.to_pydatetime().date().isoformat()

    # datetime.datetime
    if isinstance(v, datetime.datetime):
        return v.date().isoformat()

    # datetime.date (but not datetime.datetime, already handled above)
    if isinstance(v, datetime.date):
        return v.isoformat()

    return v


def _normalize_df_for_compare(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert a DataFrame to a comparison-friendly shape:
      - normalize each cell
      - resulting dtypes will usually be 'object', so int32 vs int64 etc. no longer matter
    """
    # Avoid DataFrame.applymap() to keep Pylance happy:
    # for each column (Series), map every value through _normalize_cell_for_compare
    return df.apply(lambda col: col.map(_normalize_cell_for_compare))


def _assert_exact_equal(actual_df: pd.DataFrame, exp: pd.DataFrame) -> None:
    # Align columns first
    A = actual_df[exp.columns]
    E = exp

    # ---- Make comparison *row-order insensitive* by default ----
    sort_cols = list(E.columns)
    A = A.sort_values(sort_cols).reset_index(drop=True)
    E = E.sort_values(sort_cols).reset_index(drop=True)

    # Normalize both sides
    A_norm = _normalize_df_for_compare(A)
    E_norm = _normalize_df_for_compare(E)

    if A_norm.equals(E_norm):
        return

    # Helpful debug: show dtypes *after* normalization and indices
    debug = [
        "Rows differ but CSV output is identical or deceptively similar.",
        f"Actual index:   {list(A_norm.index)}",
        f"Expected index: {list(E_norm.index)}",
        "",
        "Actual dtypes:",
        str(A_norm.dtypes),
        "",
        "Expected dtypes:",
        str(E_norm.dtypes),
    ]
    debug_msg = "\n".join(debug)

    a_csv = A_norm.to_csv(index=False)
    e_csv = E_norm.to_csv(index=False)
    diff = "\n".join(
        difflib.unified_diff(
            e_csv.splitlines(),
            a_csv.splitlines(),
            fromfile="expected",
            tofile="actual",
            lineterm="",
        )
    )
    raise UnitAssertionFailure(f"{debug_msg}\n\nDiff:\n{diff}")


# ---------- Runner ----------


def validate_inputs_cover_deps(node: Node, inputs: dict[str, dict]) -> tuple[list[str], list[str]]:
    # Inform the user if expected deps are missing (heuristic only).
    # Map logical dependency -> physical relation
    expected = [relation_for(d) for d in (node.deps or [])]
    missing = [r for r in expected if r not in inputs]
    return expected, missing


@dataclass
class UtestCtx:
    executor: Any
    jenv: Any
    engine_name: str
    env_ctx: EnvCtx
    cache: FingerprintCache | None
    cache_mode: str
    computed_fps: dict[str, str] = field(default_factory=dict)
    failures: int = 0


def _normalize_cache_mode(cache_mode: str | Any) -> str:
    if not isinstance(cache_mode, str):
        cache_mode = getattr(cache_mode, "value", str(cache_mode))
    v = cache_mode.lower()
    if v not in {"off", "ro", "rw"}:
        raise ValueError(f"unknown cache_mode: {cache_mode}")
    return v


def _detect_engine_name(executor: Any) -> str:
    # Prefer explicit engine_name on BaseExecutor subclasses
    name = getattr(executor, "engine_name", None)
    if isinstance(name, str) and name:
        return name

    # Fallback heuristics for non-BaseExecutor usage
    if hasattr(executor, "con"):
        return "duckdb"
    if hasattr(executor, "engine"):
        return "postgres"
    if hasattr(executor, "client"):  # pragma: no cover
        return "bigquery"
    return "unknown"


def _make_env_ctx(engine_name: str) -> EnvCtx:
    return build_env_ctx(
        engine=engine_name,
        profile_name="utest",
        relevant_env_keys=[k for k in os.environ if k.startswith("FF_")],
        sources=getattr(REGISTRY, "sources", {}),
    )


def _make_cache(project_dir: Path | None, engine_name: str) -> FingerprintCache | None:
    if project_dir is None:
        return None
    cache = FingerprintCache(project_dir, profile="utest", engine=engine_name)
    cache.load()
    return cache


def _get_project_dir_safe() -> Path | None:
    try:
        return REGISTRY.get_project_dir()
    except Exception:
        return None


def _fingerprint_case(node: Any, spec: Any, case: Any, ctx: UtestCtx) -> str | None:
    # 1) casespezifische Dep-FPs
    dep_fps = {
        "__case__": f"{getattr(spec, 'path', 'spec')}::{getattr(case, 'name', 'case')}",
        "__inputs__": _fingerprint_case_inputs(spec, case),
    }
    try:
        if node.kind == "sql":  # pragma: no cover
            sql = ctx.executor.render_sql(
                node,
                ctx.jenv,
                ref_resolver=lambda nm: ctx.executor._resolve_ref(nm, ctx.jenv),
                source_resolver=ctx.executor._resolve_source,
            )
            return fingerprint_sql(
                node=node, rendered_sql=sql, env_ctx=ctx.env_ctx, dep_fps=dep_fps
            )
        # python
        func = REGISTRY.py_funcs[node.name]
        src = get_function_source(func)
        return fingerprint_py(node=node, func_src=src, env_ctx=ctx.env_ctx, dep_fps=dep_fps)
    except Exception:
        return None  # fingerprint optional


def _maybe_skip_by_cache(node: Any, cand_fp: str | None, ctx: UtestCtx) -> bool:
    if not (cand_fp and ctx.cache and ctx.cache_mode in {"ro", "rw"}):
        return False
    materialized = (getattr(node, "meta", {}) or {}).get("materialized", "table")
    if can_skip_node(
        node_name=node.name,
        new_fp=cand_fp,
        cache=ctx.cache,
        executor=ctx.executor,
        materialized=materialized,
    ):
        print("   ↻ skipped (utest cache hit)")
        if ctx.cache_mode == "rw":
            ctx.computed_fps.setdefault(node.name, cand_fp)
        return True
    return False


def _execute_and_update_cache(
    node: Any, cand_fp: str | None, ctx: UtestCtx
) -> tuple[bool, str | None]:
    """
    Execute the model node and update cache fingerprint if enabled.

    Returns:
        (ok, message) where message is only set on failure.
    """
    ok, err = _execute_node(ctx.executor, node, ctx.jenv)
    if not ok:
        msg = f"execution failed: {err}"
        print(f"   ❌ {msg}")
        ctx.failures += 1
        return False, msg

    if cand_fp and ctx.cache and ctx.cache_mode == "rw":
        ctx.computed_fps[node.name] = cand_fp

    return True, None


def _read_and_assert(spec: Any, case: Any, ctx: UtestCtx) -> tuple[bool, str | None]:
    """
    Read the target relation and assert expected rows.

    Returns:
        (ok, message) where message is set when read/assert fails.
    """
    ok, df_or_exc, target_rel = _read_target_df(ctx.executor, spec, case)

    if not ok:
        msg: str | None = f"cannot read result '{target_rel}': {df_or_exc}"
        print(f"   ❌ {msg}")
        ctx.failures += 1
        return False, msg

    ok2, msg = _assert_expected_rows(df_or_exc, case)
    if ok2:
        print("   ✅ ok")
        return True, None

    # msg is already a human-readable diff/summary from your assert helper
    print(f"   ❌ {msg}")
    ctx.failures += 1
    return False, msg


def _build_utest_ctx(executor: Any, jenv: Any, cache_mode: str) -> UtestCtx:
    project_dir = _get_project_dir_safe()
    engine_name = _detect_engine_name(executor)
    env_ctx = _make_env_ctx(engine_name)
    cache = _make_cache(project_dir, engine_name)

    return UtestCtx(
        executor=executor,
        jenv=jenv,
        engine_name=engine_name,
        env_ctx=env_ctx,
        cache=cache,
        cache_mode=cache_mode,
    )


def _append_result(
    results_out: list[dict[str, Any]] | None,
    *,
    spec: UnitSpec,
    case_name: str,
    status: str,
    message: str | None,
    duration_ms: int,
    cache_hit: bool,
    target_relation: str,
) -> None:
    if results_out is None:
        return

    results_out.append(
        {
            "model": spec.model,
            "case": case_name,
            "status": status,
            "message": (message or ""),
            "duration_ms": duration_ms,
            "cache_hit": cache_hit,
            "target_relation": target_relation,
            "spec_path": str(spec.path),
        }
    )


def _record_model_not_found(
    ctx: UtestCtx,
    *,
    spec: UnitSpec,
    results_out: list[dict[str, Any]] | None,
) -> None:
    print(f"⚠️  Model '{spec.model}' not found (in {spec.path})")
    ctx.failures += 1
    _append_result(
        results_out,
        spec=spec,
        case_name="",
        status="error",
        message="model not found",
        duration_ms=0,
        cache_hit=False,
        target_relation="",
    )


def _compute_target_relation(spec_model: str, case: Any) -> str:
    target_rel_cfg = getattr(case, "expect", None)
    if isinstance(target_rel_cfg, UnitExpect):
        return target_rel_cfg.relation or relation_for(spec_model)
    if isinstance(target_rel_cfg, Mapping):
        return target_rel_cfg.get("relation") or relation_for(spec_model)
    return relation_for(spec_model)


def _iter_cases(spec: UnitSpec, only_case: str | None) -> Generator[UnitCase]:
    for raw_case in spec.cases:
        case = spec.merged_case(raw_case)
        if only_case and case.name != only_case:
            continue
        yield case


def _duration_ms(t0: float, t1: float) -> int:
    return int((t1 - t0) * 1000)


def _skip_due_to_input_failure(
    ctx: UtestCtx,
    *,
    spec: UnitSpec,
    case: Any,
    t0: float,
    results_out: list[dict[str, Any]] | None,
) -> None:
    print("   ⚠️ skipping execution due to input load failure")
    t1 = perf_counter()
    _append_result(
        results_out,
        spec=spec,
        case_name=case.name,
        status="error",
        message="input load failure",
        duration_ms=_duration_ms(t0, t1),
        cache_hit=False,
        target_relation="",
    )


def _run_one_case(
    ctx: UtestCtx,
    *,
    spec: UnitSpec,
    case: Any,
    node: Any,
    reuse_meta: bool,
    results_out: list[dict[str, Any]] | None,
) -> None:
    print(f"→ {spec.model} :: {case.name}")
    t0 = perf_counter()

    status = "pass"
    message: str | None = None

    if not reuse_meta:
        with suppress(Exception):
            delete_meta_for_node(ctx.executor, node.name)

    cand_fp = _fingerprint_case(node, spec, case, ctx)

    before_failures = ctx.failures
    ctx.failures += _load_inputs_for_case(ctx.executor, spec, case, node)
    if ctx.failures > before_failures:
        _skip_due_to_input_failure(ctx, spec=spec, case=case, t0=t0, results_out=results_out)
        return

    target_rel = _compute_target_relation(spec.model, case)

    if _maybe_skip_by_cache(node, cand_fp, ctx):
        ok, msg = _read_and_assert(spec, case, ctx)
        if not ok:
            status = "fail"
            message = msg

        _cleanup_inputs_for_case(ctx.executor, case)
        t1 = perf_counter()
        _append_result(
            results_out,
            spec=spec,
            case_name=case.name,
            status=status,
            message=message,
            duration_ms=_duration_ms(t0, t1),
            cache_hit=True,
            target_relation=target_rel,
        )
        return

    _reset_utest_relation(ctx.executor, target_rel)

    ok_exec, exec_msg = _execute_and_update_cache(node, cand_fp, ctx)
    if not ok_exec:
        _cleanup_inputs_for_case(ctx.executor, case)
        t1 = perf_counter()
        _append_result(
            results_out,
            spec=spec,
            case_name=case.name,
            status="error",
            message=exec_msg,
            duration_ms=_duration_ms(t0, t1),
            cache_hit=False,
            target_relation=target_rel,
        )
        return

    ok, msg = _read_and_assert(spec, case, ctx)
    if not ok:
        status = "fail"
        message = msg

    _cleanup_inputs_for_case(ctx.executor, case)
    t1 = perf_counter()
    _append_result(
        results_out,
        spec=spec,
        case_name=case.name,
        status=status,
        message=message,
        duration_ms=_duration_ms(t0, t1),
        cache_hit=False,
        target_relation=target_rel,
    )


def _finalize_cache(ctx: UtestCtx) -> None:
    if ctx.cache and ctx.computed_fps and ctx.cache_mode == "rw":  # pragma: no cover
        ctx.cache.update_many(ctx.computed_fps)
        ctx.cache.save()


def run_unit_specs(
    specs: list[UnitSpec],
    executor: Any,
    jenv: Any,
    only_case: str | None = None,
    *,
    cache_mode: str = "off",
    reuse_meta: bool = False,
    results_out: list[dict[str, Any]] | None = None,
) -> int:
    """
    Execute discovered unit-test specs. Returns the number of failed cases.

    Args:
        cache_mode: 'off' | 'ro' | 'rw'. Default 'off' for deterministic runs.
        reuse_meta: reserved (no-op).
    """
    cache_mode = _normalize_cache_mode(cache_mode)
    ctx = _build_utest_ctx(executor, jenv, cache_mode)

    for spec in specs:
        if spec.engine and spec.engine != ctx.engine_name:
            continue

        node = REGISTRY.nodes.get(spec.model)
        if not node:
            _record_model_not_found(ctx, spec=spec, results_out=results_out)
            continue

        for case in _iter_cases(spec, only_case):
            _run_one_case(
                ctx,
                spec=spec,
                case=case,
                node=node,
                reuse_meta=reuse_meta,
                results_out=results_out,
            )

    _finalize_cache(ctx)
    return ctx.failures


# ----------------- Helper -----------------


def _reset_utest_relation(executor: Any, relation: str) -> None:
    """
    Best-effort: ask the executor to drop any view/table for this relation
    before we (re)create it in a unit test.
    """
    reset = getattr(executor, "utest_clean_target", None)
    if callable(reset):
        with suppress(Exception):
            reset(relation)


def _cleanup_inputs_for_case(executor: Any, case: Any) -> None:
    """
    Best-effort: drop all input relations after a unit-test case finishes.

    This prevents tables created as test fixtures (like 'users_clean' in mart tests)
    from leaking into other specs (like the 'users_clean' model tests).
    """
    inputs = getattr(case, "inputs", None) or {}
    for rel in inputs:
        _reset_utest_relation(executor, rel)


def _load_inputs_for_case(executor: Any, spec: Any, case: Any, node: Any) -> int:
    """
    Loads all declared relations in 'case.inputs'.
    Returns the count of failed inputs.
    """
    failures = 0

    expected_deps, missing = validate_inputs_cover_deps(node, case.inputs)
    if missing:
        print(
            f"   ⚠️ inputs do not cover all deps: missing {missing}" + f" (expected {expected_deps})"
        )

    for rel, cfg in (case.inputs or {}).items():
        try:
            _reset_utest_relation(executor, rel)

            rows: list[dict] | None = None
            csv_val: str | None = None

            if isinstance(cfg, UnitInput):
                rows = cfg.rows
                csv_val = cfg.csv
            elif isinstance(cfg, Mapping):
                rows = cast(Mapping[str, Any], cfg).get("rows")
                csv_val = cast(Mapping[str, Any], cfg).get("csv")

            if rows is not None:
                _load_relation_from_rows(executor, rel, rows)
            elif csv_val:
                csv_path = _resolve_csv_path(spec, csv_val)
                _load_relation_from_csv(executor, rel, csv_path)
            else:
                print(f"   ❌ invalid input for relation '{rel}'")
                failures += 1
        except Exception as e:
            print(f"   ❌ failed loading input for '{rel}': {e}")
            failures += 1

    return failures


def _execute_node(executor: Any, node: Any, jenv: Any) -> tuple[bool, str | None]:
    # Best-effort cleanup so view<->table flips don't fail in DuckDB/Postgres.
    try:
        rel = relation_for(node.name)
        _reset_utest_relation(executor, rel)
    except Exception:
        # Cleanup is best-effort; don't fail the test run on cleanup errors.
        pass

    try:
        if getattr(node, "kind", None) == "sql":
            executor.run_sql(node, jenv)
        else:
            executor.run_python(node)
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _read_target_df(executor: Any, spec: Any, case: Any) -> tuple[bool, Any, str]:
    exp_cfg = getattr(case, "expect", None) or {}
    if isinstance(exp_cfg, UnitExpect):
        target_rel = exp_cfg.relation or relation_for(spec.model)
    elif isinstance(exp_cfg, Mapping):
        target_rel = exp_cfg.get("relation") or relation_for(spec.model)
    else:
        target_rel = relation_for(spec.model)
    try:
        df = _read_result(executor, target_rel)
        return True, df, target_rel
    except Exception as e:
        return False, e, target_rel


def _assert_expected_rows(df: Any, case: Any) -> tuple[bool, str | None]:
    try:
        exp_cfg = getattr(case, "expect", None) or {}

        if isinstance(exp_cfg, UnitExpect):
            rows_cfg = exp_cfg.rows or []
            order_by = exp_cfg.order_by
            any_order = exp_cfg.any_order
            approx = exp_cfg.approx
            ignore_columns = exp_cfg.ignore_columns
            subset = exp_cfg.subset
        elif isinstance(exp_cfg, Mapping):
            rows_cfg = exp_cfg.get("rows", [])
            order_by = exp_cfg.get("order_by")
            any_order = exp_cfg.get("any_order", False)
            approx = exp_cfg.get("approx")
            ignore_columns = exp_cfg.get("ignore_columns")
            subset = exp_cfg.get("subset", False)
        else:
            rows_cfg = []
            order_by = None
            any_order = False
            approx = None
            ignore_columns = None
            subset = False

        assert_rows_equal(
            df,
            rows_cfg,
            order_by=order_by,
            any_order=any_order,
            approx=approx,
            ignore_columns=ignore_columns,
            subset=subset,
        )
        return True, None
    except UnitAssertionFailure as e:
        return False, str(e)
    except AssertionError as e:
        return False, str(e)
