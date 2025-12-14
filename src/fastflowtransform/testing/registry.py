# fastflowtransform/testing/registry.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from fastflowtransform.core import REGISTRY
from fastflowtransform.executors.base import BaseExecutor
from fastflowtransform.logging import get_logger
from fastflowtransform.testing import base as testing
from fastflowtransform.testing.base import _scalar

logger = get_logger("dq_registry")


class Runner(Protocol):
    """Callable signature for a generic test runner.

    Returns:
        ok (bool): Whether the test passed.
        message (str | None): Optional human-friendly message (usually set on failure).
        example_sql (str | None): Optional example SQL (shown in summary on failure).
    """

    __name__: str

    def __call__(
        self, executor: BaseExecutor, table: str, column: str | None, params: dict[str, Any]
    ) -> tuple[bool, str | None, str | None]: ...


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _format_param_validation_error(
    kind: str,
    origin: str | None,
    exc: ValidationError,
) -> str:
    """Build a human-friendly error message when params don't match the schema."""
    lines: list[str] = []
    header = f"[{kind}] Invalid test configuration"
    if origin:
        header += f" for {origin}"
    lines.append(header + ":")
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "invalid value")
        if loc:
            lines.append(f"  • {loc}: {msg}")
        else:
            lines.append(f"  • {msg}")
    lines.append(
        "Hint: Update project.yml → tests: entry for this test so that its parameters "
        "match the expected schema."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Basic column-level tests
# ---------------------------------------------------------------------------


def run_not_null(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    where = params.get("where")
    example = f"select count(*) from {table} where {column} is null" + (
        f" and ({where})" if where else ""
    )
    if column is None:
        # Column is required for not_null
        return False, "missing required parameter: column", example
    col = column
    try:
        testing.not_null(executor, table, col, where=where)
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_unique(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    where = params.get("where")
    example = (
        f"select {column} as key, count(*) c from {table}"
        + (f" where ({where})" if where else "")
        + " group by 1 having count(*) > 1 limit 5"
    )
    if column is None:
        return False, "missing required parameter: column", example
    col = column
    try:
        testing.unique(executor, table, col, where=where)
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_accepted_values(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.accepted_values."""
    values = params.get("values") or []
    where = params.get("where")

    if column is None:
        example = "-- accepted_values: column parameter is required"
        return False, "missing required parameter: column", example

    if not values:
        # No values configured -> we treat this as a no-op check.
        example = f"-- accepted_values: no values provided; check is skipped for {table}.{column}"
        return True, None, example

    in_list = testing.sql_list(values)
    example = (
        f"select distinct {column} from {table} "
        + f"where {column} is not null and {column} not in ({in_list})"
        + (f" and ({where})" if where else "")
        + " limit 5"
    )

    col = column
    try:
        testing.accepted_values(executor, table, col, values=values, where=where)
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_greater_equal(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.greater_equal (column >= threshold)."""
    threshold = float(params.get("threshold", 0.0))
    if column is None:
        example = f"select count(*) from {table} where <column> < {threshold}"
        return False, "missing required parameter: column", example

    example = f"select count(*) from {table} where {column} < {threshold}"
    col = column
    try:
        testing.greater_equal(executor, table, col, threshold=threshold)
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_between(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.between (inclusive numeric range)."""
    if column is None:
        example = f"select count(*) from {table} where <column> < <min> or <column> > <max>"
        return False, "missing required parameter: column", example

    min_val = params.get("min")
    max_val = params.get("max")

    if min_val is None and max_val is None:
        example = f"-- between: no min/max provided for {table}.{column}"
        return (
            False,
            "between test requires at least one of 'min' or 'max'",
            example,
        )

    conds: list[str] = []
    if min_val is not None:
        conds.append(f"{column} < {min_val}")
    if max_val is not None:
        conds.append(f"{column} > {max_val}")
    where_expr = " or ".join(conds)
    example = f"select count(*) from {table} where {column} is not null and ({where_expr})"

    col = column
    try:
        testing.between(
            executor,
            table,
            col,
            min_value=min_val,
            max_value=max_val,
        )
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_regex_match(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.regex_match (Python-side regex evaluation)."""
    pattern = params.get("pattern") or params.get("regex")
    where = params.get("where")

    if column is None:
        example = f"select {column or '<column>'} from {table}"
        return False, "missing required parameter: column", example

    if not pattern:
        example = f"select {column} from {table}  -- pattern missing"
        return False, "missing required parameter: pattern", example

    example = f"select {column} from {table}"
    if where:
        example += f" where ({where})"

    col = column
    try:
        testing.regex_match(
            executor,
            table,
            col,
            pattern=str(pattern),
            where=where,
        )
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_column_physical_type(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """
    Runner for testing.column_physical_type (schema/DDL assertion).

    Args:
        executor: Backend executor for querying information_schema.
        table: Target table name.
        column: Target column name.
        params: Config mapping; supports `physical` as either a string type
            or a mapping of {engine_key: type, default: type}.
    """
    physical_cfg = params.get("physical")

    if column is None:
        example = "-- column_physical_type: column parameter is required"
        return False, "missing required parameter: column", example

    if physical_cfg is None:
        # Nothing to enforce; treat as noop (passes).
        example = f"-- column_physical_type: no 'physical' configured for {table}.{column}"
        return True, None, example

    example = f"-- physical type check for {table}.{column} via information_schema.columns"

    try:
        testing.column_physical_type(executor, table, column, physical_cfg)
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_non_negative_sum(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.non_negative_sum."""
    if column is None:
        example = f"select coalesce(sum(<column>), 0) from {table}"
        return False, "missing required parameter: column", example

    example = f"select coalesce(sum({column}), 0) from {table}"
    col = column
    try:
        testing.non_negative_sum(executor, table, col)
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


# ---------------------------------------------------------------------------
# Table-level tests
# ---------------------------------------------------------------------------


def run_row_count_between(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.row_count_between."""
    min_rows = int(params.get("min_rows", 1))
    max_rows_param = params.get("max_rows")
    max_rows = int(max_rows_param) if max_rows_param is not None else None

    example = f"select count(*) from {table}"
    try:
        testing.row_count_between(executor, table, min_rows=min_rows, max_rows=max_rows)
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_freshness(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.freshness (max timestamp delay in minutes)."""
    if column is None:
        example = (
            f"select date_part('epoch', now() - max(<ts_column>)) / 60.0 as delay_min from {table}"
        )
        return False, "missing required parameter: column (ts_col)", example

    max_delay_raw = params.get("max_delay_minutes")
    example = f"select date_part('epoch', now() - max({column})) / 60.0 as delay_min from {table}"

    if max_delay_raw is None:
        return False, "missing required parameter: max_delay_minutes", example

    try:
        max_delay_int = int(max_delay_raw)
    except (TypeError, ValueError):
        return (
            False,
            f"invalid max_delay_minutes (expected integer minutes, got {max_delay_raw!r})",
            example,
        )

    col = column
    try:
        testing.freshness(executor, table, col, max_delay_minutes=max_delay_int)
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


# ---------------------------------------------------------------------------
# Helpers for reconcile tests
# ---------------------------------------------------------------------------


def _example_scalar_side(side: dict[str, Any]) -> str:
    """Render an example SELECT for a reconcile side."""
    tbl = side.get("table", "<table>")
    expr = side.get("expr", "<expr>")
    where = side.get("where")
    return f"select {expr} from {tbl}" + (f" where {where}" if where else "")


def _example_coverage_sql(
    source: dict[str, Any],
    target: dict[str, Any],
    source_where: str | None,
    target_where: str | None,
) -> str:
    """Render an example SQL for reconcile_coverage."""
    s_tbl, s_key = source.get("table", "<source_table>"), source.get("key", "<source_key>")
    t_tbl, t_key = target.get("table", "<target_table>"), target.get("key", "<target_key>")
    s_w = f" where {source_where}" if source_where else ""
    t_w = f" where {target_where}" if target_where else ""
    return f"""
with src as (select {s_key} as k from {s_tbl}{s_w}),
     tgt as (select {t_key} as k from {t_tbl}{t_w})
select count(*) from src s
left join tgt t on s.k = t.k
where t.k is null
""".strip()


def _example_relationship_sql(
    child_table: str,
    child_field: str,
    parent_table: str | None,
    parent_field: str,
    child_where: str | None,
    parent_where: str | None,
) -> str:
    """Render an example SQL snippet for relationships (foreign key) checks."""
    ct = child_table or "<child_table>"
    cf = child_field or "<child_field>"
    pt = parent_table or "<parent_table>"
    pf = parent_field or "<parent_field>"
    cw = f" where {child_where}" if child_where else ""
    pw = f" where {parent_where}" if parent_where else ""
    return f"""
with child as (select {cf} as k from {ct}{cw}),
     parent as (select {pf} as k from {pt}{pw})
select count(*) from child c
left join parent p on c.k = p.k
where p.k is null
""".strip()


# ---------------------------------------------------------------------------
# Reconcile tests
# ---------------------------------------------------------------------------


def run_reconcile_equal(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.reconcile_equal (left == right within tolerances)."""
    left = params.get("left")
    right = params.get("right")
    abs_tol = params.get("abs_tolerance")
    rel_tol = params.get("rel_tolerance_pct")

    if not isinstance(left, dict) or not isinstance(right, dict):
        example = "-- reconcile_equal requires 'left' and 'right' dict parameters"
        return False, "missing or invalid 'left'/'right' parameters", example

    example = _example_scalar_side(left) + ";\n" + _example_scalar_side(right)

    try:
        testing.reconcile_equal(
            executor,
            left=left,
            right=right,
            abs_tolerance=abs_tol,
            rel_tolerance_pct=rel_tol,
        )
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_reconcile_ratio_within(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.reconcile_ratio_within (min_ratio <= L/R <= max_ratio)."""
    left = params.get("left")
    right = params.get("right")
    min_ratio = params.get("min_ratio")
    max_ratio = params.get("max_ratio")

    if not isinstance(left, dict) or not isinstance(right, dict):
        example = "-- reconcile_ratio_within requires 'left' and 'right' dict parameters"
        return False, "missing or invalid 'left'/'right' parameters", example

    if min_ratio is None or max_ratio is None:
        example = _example_scalar_side(left) + ";\n" + _example_scalar_side(right)
        return False, "missing required parameters: min_ratio / max_ratio", example

    example = _example_scalar_side(left) + ";\n" + _example_scalar_side(right)

    try:
        testing.reconcile_ratio_within(
            executor,
            left=left,
            right=right,
            min_ratio=float(min_ratio),
            max_ratio=float(max_ratio),
        )
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_reconcile_diff_within(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.reconcile_diff_within (|L - R| <= max_abs_diff)."""
    left = params.get("left")
    right = params.get("right")
    max_abs_diff = params.get("max_abs_diff")

    if not isinstance(left, dict) or not isinstance(right, dict):
        example = "-- reconcile_diff_within requires 'left' and 'right' dict parameters"
        return False, "missing or invalid 'left'/'right' parameters", example

    if max_abs_diff is None:
        example = _example_scalar_side(left) + ";\n" + _example_scalar_side(right)
        return False, "missing required parameter: max_abs_diff", example

    example = _example_scalar_side(left) + ";\n" + _example_scalar_side(right)

    try:
        testing.reconcile_diff_within(
            executor,
            left=left,
            right=right,
            max_abs_diff=float(max_abs_diff),
        )
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_reconcile_coverage(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.reconcile_coverage (anti-join count == 0)."""
    source = params.get("source")
    target = params.get("target")
    source_where = params.get("source_where")
    target_where = params.get("target_where")

    if not isinstance(source, dict) or not isinstance(target, dict):
        example = "-- reconcile_coverage requires 'source' and 'target' dict parameters"
        return False, "missing or invalid 'source'/'target' parameters", example

    example = _example_coverage_sql(source, target, source_where, target_where)

    try:
        testing.reconcile_coverage(
            executor,
            source=source,
            target=target,
            source_where=source_where,
            target_where=target_where,
        )
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


def run_relationships(
    executor: Any, table: str, column: str | None, params: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Runner for testing.relationships (FK-style anti join)."""
    field = params.get("field") or column
    to_table = params.get("_to_relation") or params.get("to")
    to_field = params.get("to_field") or "id"
    where = params.get("where")
    to_where = params.get("to_where")

    example = _example_relationship_sql(
        table, field or "<field>", to_table, to_field, where, to_where
    )

    if not field:
        return False, "missing required parameter: field (or column)", example
    if not to_table:
        return False, "missing required parameter: to", example

    try:
        testing.relationships(
            executor,
            table=table,
            field=field,
            to_table=to_table,
            to_field=to_field,
            where=where,
            to_where=to_where,
        )
        return True, None, example
    except testing.TestFailure as e:
        return False, str(e), example


# ---------------------------------------------------------------------------
# Optional param-schema registry for custom tests
# ---------------------------------------------------------------------------

# kind -> Pydantic model used to validate params
TEST_PARAM_MODELS: dict[str, type[BaseModel]] = {}
# kind -> origin info (for nicer messages)
TEST_ORIGINS: dict[str, str] = {}  # e.g. "tests/dq/no_future_orders.ff.sql"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Public registry (extensible).
TESTS: dict[str, Runner] = {
    "not_null": run_not_null,
    "unique": run_unique,
    "accepted_values": run_accepted_values,
    "greater_equal": run_greater_equal,
    "non_negative_sum": run_non_negative_sum,
    "row_count_between": run_row_count_between,
    "freshness": run_freshness,
    "relationships": run_relationships,
    # Reconcile tests
    "reconcile_equal": run_reconcile_equal,
    "reconcile_ratio_within": run_reconcile_ratio_within,
    "reconcile_diff_within": run_reconcile_diff_within,
    "reconcile_coverage": run_reconcile_coverage,
    # Contracts helpers
    "between": run_between,
    "regex_match": run_regex_match,
    "column_physical_type": run_column_physical_type,
}


# ---------------------------------------------------------------------------
# Public registration API
# ---------------------------------------------------------------------------


class DQParamsBase(BaseModel):
    """
    Base for all dynamically created DQ params models.
    Forbids unknown keys by default.
    """

    model_config = ConfigDict(extra="forbid")


def register_python_test(
    kind: str,
    runner: Runner,
    *,
    params_model: type[BaseModel] | None = None,
    origin: str | None = None,
    overwrite: bool = False,
) -> None:
    """
    Register a custom Python test.

    Args:
        kind: logical test type (e.g. "no_future_orders").
        runner: callable implementing Runner.
        params_model: optional Pydantic model for the params dict.
        origin: string used in error messages (e.g. module path).
        overwrite: if True, replace an existing test with the same kind.
    """
    if kind in TESTS and not overwrite:
        raise ValueError(
            f"Test type {kind!r} is already registered "
            f"(origin={TEST_ORIGINS.get(kind, '<builtin>')!r})"
        )

    if kind in TESTS and overwrite:
        logger.warning(
            "Overwriting DQ test %r (previous origin=%r, new origin=%r)",
            kind,
            TEST_ORIGINS.get(kind),
            origin,
        )

    TESTS[kind] = runner

    if params_model is not None:
        TEST_PARAM_MODELS[kind] = params_model
    # If overwriting and no new params_model is given, keep the old one if present.
    elif overwrite and kind in TEST_PARAM_MODELS:
        pass
    else:
        # Default to a generic params model if you have one, or leave it unset
        TEST_PARAM_MODELS.pop(kind, None)

    if origin is not None:
        TEST_ORIGINS[kind] = origin
    elif overwrite:
        # if overwriting without explicit origin, don't change existing origin
        pass


def register_sql_test(
    kind: str,
    path: Path,
    *,
    params_model: type[BaseModel] | None = None,
    overwrite: bool = False,
) -> None:
    """
    Register a custom SQL-based test from a *.ff.sql file.

    kind: logical test type (e.g. "no_future_orders").
    path: filesystem path to the template.
    params_model: optional Pydantic model for params.
    overwrite: if True, allow overriding an existing test of the same kind.
    """
    origin = str(path)
    META_KEYS = {"type", "table", "column", "severity", "tags", "name"}

    def _runner(
        executor: Any, table: str, column: str | None, params: dict[str, Any]
    ) -> tuple[bool, str | None, str | None]:
        # 1) Strip generic test metadata and validate params if a schema is provided
        raw_params: dict[str, Any] = dict(params or {})
        core_params: dict[str, Any] = {k: v for k, v in raw_params.items() if k not in META_KEYS}

        if params_model is not None:
            try:
                cfg = params_model.model_validate(core_params)
            except ValidationError as exc:
                err_msg = _format_param_validation_error(kind, origin, exc)
                raise testing.TestFailure(err_msg) from exc
            # Use normalized params (e.g. converted types, defaults)
            params_validated = cfg.model_dump(exclude_none=True)
        else:
            params_validated = core_params

        # 2) Render the SQL template with a stable context
        env = REGISTRY.get_env()
        if "config" not in env.globals:
            # DQ SQL templates include a leading {{ config(...) }} metadata block; it
            # should be a no-op at render time, so provide a stub when absent.
            env.globals["config"] = lambda **kwargs: ""
        raw = path.read_text(encoding="utf-8")
        tmpl = env.from_string(raw)

        ctx: dict[str, Any] = {
            "kind": kind,
            "table": table,
            "column": column,
            "params": params_validated,
            # always present, so templates can safely do `{% if where %}`
            "where": params_validated.get("where"),
        }

        try:
            sql = tmpl.render(**ctx)
        except Exception as exc:
            raise testing.TestFailure(
                f"[{kind}] Failed to render SQL template for {origin}: {exc}"
            ) from exc

        # 3) Execute the SQL: convention here is "fail if count(*) > 0"
        n = _scalar(executor, sql)
        ok = int(n or 0) == 0
        msg: str | None = None if ok else f"{kind} failed: {n} offending row(s)"
        example_sql = sql
        return ok, msg, example_sql

    register_python_test(
        kind,
        _runner,
        params_model=params_model,
        origin=origin,
        overwrite=overwrite,
    )
