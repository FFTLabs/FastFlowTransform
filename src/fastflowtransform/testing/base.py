# src/fastflowtransform/testing/base.py
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy.sql.elements import ClauseElement

from fastflowtransform.config.contracts import PhysicalTypeConfig
from fastflowtransform.executors.base import BaseExecutor, _scalar
from fastflowtransform.logging import dprint
from fastflowtransform.utils.timefmt import format_duration_minutes

# ===== Execution helpers ==================


def _fail(check: str, table: str, column: str | None, sql: str, detail: str) -> None:
    raise TestFailure(
        f"[{check}] {table}{('.' + column) if column else ''}: {detail}\nSQL:\n{sql.strip()}"
    )


def _pretty_sql(sql: Any) -> str:
    """Compact, human-readable rendering for debugging."""
    sql_tuple_len = 2

    if isinstance(sql, str):
        return sql.strip()
    if isinstance(sql, tuple) and len(sql) == sql_tuple_len and isinstance(sql[0], str):
        return f"{sql[0].strip()}  -- params={sql[1]}"
    if isinstance(sql, ClauseElement):
        return "<SQLAlchemy ClauseElement>"
    if isinstance(sql, Sequence) and not isinstance(sql, (bytes, bytearray, str)):
        parts = []
        for s in sql:
            parts.append(_pretty_sql(s))
        return "[\n  " + ",\n  ".join(parts) + "\n]"
    return repr(sql)


def sql_list(values: list[Any] | None) -> str:
    """Render a simple SQL literal list, portable enough for DuckDB/Postgres/BigQuery."""

    def lit(v: Any) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v).replace("'", "''")
        return f"'{s}'"

    return ", ".join(lit(v) for v in (values or []))


def accepted_values(
    executor: BaseExecutor, table: str, column: str, *, values: list[Any], where: str | None = None
) -> None:
    """
    Fail if any non-NULL value of table.column is outside the set 'values'.
    """
    # If no values are provided, we consider the check vacuously true.
    if not values:
        return

    in_list = sql_list(values)
    sql = f"select count(*) from {table} where {column} is not null"

    sql = f"select count(*) from {table} where {column} is not null and {column} not in ({in_list})"

    n = _scalar(executor, sql)
    if int(n or 0) > 0:
        sample_sql = f"select distinct {column} from {table} where {column} is not null"
        if in_list:
            sample_sql += f" and {column} not in ({in_list})"
        if where:
            sql += f" and ({where})"
            sample_sql += f" and ({where})"
        sample_sql += " limit 5"
        rows = [r[0] for r in executor.execute_test_sql(sample_sql).fetchall()]
        raise TestFailure(f"{table}.{column} has {n} value(s) outside accepted set; e.g. {rows}")


# ===== Tests ==============================================================


class TestFailure(Exception):
    """Raised when a data-quality check fails."""

    # Prevent pytest from collecting this as a test when imported into a test module.
    __test__ = False
    pass


def _wrap_db_error(
    check: str, table: str, column: str | None, sql: str, err: Exception
) -> TestFailure:
    msg = [f"[{check}] Error in {table}{('.' + column) if column else ''}"]
    msg.append(f"DB-Error: {type(err).__name__}: {err}")
    # Common Postgres/SQLAlchemy hints
    txt = str(err).lower()
    if "undefinedcolumn" in txt and "having" in sql.lower():
        msg.append("Note: Postgres does not permit alias usage in HAVING statement.")
    if "f405" in txt or "textual sql expression" in txt:
        msg.append("Note: SQLAlchemy 2.0 requires text('...') for raw SQL stings.")
    msg.append("SQL:\n" + sql.strip())
    return TestFailure("\n".join(msg))


def not_null(executor: BaseExecutor, table: str, column: str, where: str | None = None) -> None:
    """Fails if any non-filtered row has NULL in `column`."""
    sql = f"select count(*) from {table} where {column} is null"
    if where:
        sql += f" and ({where})"
    try:
        c = _scalar(executor, sql)
    except Exception as e:
        raise _wrap_db_error("not_null", table, column, sql, e) from e
    dprint("not_null:", sql, "=>", c)
    if c and c != 0:
        _fail("not_null", table, column, sql, f"has {c} NULL-values")


def unique(executor: BaseExecutor, table: str, column: str, where: str | None = None) -> None:
    """Fails if any duplicate appears in `column` within the (optionally) filtered set."""
    sql = (
        "select count(*) from (select {col} as v, "
        "count(*) as c from {tbl}{w} group by 1 having count(*) > 1) as q"
    )
    w = f" where ({where})" if where else ""
    sql = sql.format(col=column, tbl=table, w=w)
    try:
        c = _scalar(executor, sql)
    except Exception as e:
        raise _wrap_db_error("unique", table, column, sql, e) from e
    dprint("unique:", sql, "=>", c)
    if c and c != 0:
        _fail("unique", table, column, sql, f"contains {c} duplicates")


def greater_equal(executor: BaseExecutor, table: str, column: str, threshold: float = 0.0) -> None:
    sql = f"select count(*) from {table} where {column} < {threshold}"
    c = _scalar(executor, sql)
    dprint("greater_equal:", sql, "=>", c)
    if c and c != 0:
        raise TestFailure(f"{table}.{column} has {c} values < {threshold}")


def between(
    executor: BaseExecutor,
    table: str,
    column: str,
    *,
    min_value: float | int | None = None,
    max_value: float | int | None = None,
) -> None:
    """
    Fail if any non-NULL value of table.column is outside the inclusive
    range [min_value, max_value]. If one bound is None, only the other
    is enforced.
    """
    if min_value is None and max_value is None:
        return

    conds: list[str] = []
    if min_value is not None:
        conds.append(f"{column} < {min_value}")
    if max_value is not None:
        conds.append(f"{column} > {max_value}")

    where_expr = " or ".join(conds)
    sql = f"select count(*) from {table} where {column} is not null and ({where_expr})"
    c = _scalar(executor, sql)
    dprint("between:", sql, "=>", c)

    if c and c != 0:
        if min_value is not None and max_value is not None:
            raise TestFailure(
                f"{table}.{column} has {c} value(s) outside inclusive range "
                f"[{min_value}, {max_value}]"
            )
        elif min_value is not None:
            raise TestFailure(f"{table}.{column} has {c} value(s) < {min_value}")
        else:
            raise TestFailure(f"{table}.{column} has {c} value(s) > {max_value}")


def regex_match(
    executor: BaseExecutor,
    table: str,
    column: str,
    pattern: str,
    where: str | None = None,
) -> None:
    """
    Fail if any non-NULL value in table.column does not match the given
    Python regex pattern. This is implemented client-side for engine
    independence:

        SELECT column FROM table [WHERE ...]
        -> evaluate in Python -> fail on first few mismatches.
    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise TestFailure(f"Invalid regex pattern {pattern!r} for {table}.{column}: {exc}") from exc

    sql = f"select {column} from {table}"
    if where:
        sql += f" where ({where})"

    res = executor.execute_test_sql(sql)
    rows: list = getattr(res, "fetchall", lambda: [])()

    bad_values: list[Any] = []
    for row in rows:
        val = row[0]
        if val is None:
            continue
        if not regex.match(str(val)):
            bad_values.append(val)
            if len(bad_values) >= 5:
                break

    dprint("regex_match:", sql, "=> bad_values:", bad_values)

    if bad_values:
        raise TestFailure(
            f"{table}.{column} has values not matching regex {pattern!r}; examples: {bad_values}"
        )


def non_negative_sum(executor: BaseExecutor, table: str, column: str) -> None:
    sql = f"select coalesce(sum({column}),0) from {table}"
    s = _scalar(executor, sql)
    dprint("non_negative_sum:", sql, "=>", s)
    if s is not None and s < 0:
        raise TestFailure(f"sum({table}.{column}) is negative: {s}")


def row_count_between(
    executor: BaseExecutor, table: str, min_rows: int = 1, max_rows: int | None = None
) -> None:
    sql = f"select count(*) from {table}"
    c = _scalar(executor, sql)
    dprint("row_count_between:", sql, "=>", c)
    if c is None or c < min_rows:
        raise TestFailure(f"{table} has too few rows: {c} < {min_rows}")
    if max_rows is not None and c > max_rows:
        raise TestFailure(f"{table} has too many rows: {c} > {max_rows}")


def _freshness_probe(executor: BaseExecutor, table: str, ts_col: str) -> Any:
    """Read max(ts_col) and wrap engine errors with context."""
    probe_sql = f"select max({ts_col}) from {table}"
    try:
        return _scalar(executor, probe_sql)
    except Exception as e:
        # Column missing or other metadata-related DB error
        raise _wrap_db_error("freshness", table, ts_col, probe_sql, e) from e


def _resolve_expected_physical(
    physical_cfg: PhysicalTypeConfig | None,
    engine_key: str,
) -> str | None:
    """
    Given the PhysicalTypeConfig and an engine key, return the expected
    physical type string for that engine, or None if nothing is declared.

    Precedence:
      1) physical.<engine_key>
      2) physical.default
    """
    if physical_cfg is None:
        return None

    # Engine-specific override
    eng_val = getattr(physical_cfg, engine_key, None)
    if isinstance(eng_val, str) and eng_val.strip():
        return eng_val.strip()

    # Fallback to default
    if isinstance(physical_cfg.default, str) and physical_cfg.default.strip():
        return physical_cfg.default.strip()

    return None


def column_physical_type(
    executor: BaseExecutor,
    table: str,
    column: str,
    physical_cfg: PhysicalTypeConfig | None,
) -> None:
    """
    Assert that the physical DB type of table.column matches the contract's
    PhysicalTypeConfig for the current engine.
    """
    engine_key = executor.engine_name
    expected = _resolve_expected_physical(physical_cfg, engine_key)
    if not expected:
        # No expectation configured for this engine → nothing to enforce.
        return

    actual = executor.introspect_column_physical_type(table, column)
    if actual is None:
        raise TestFailure(
            f"[column_physical_type] Could not determine physical type for {table}.{column} "
            f"(engine={engine_key}). Ensure the table exists and the column name is correct."
        )

    exp_norm = executor.normalize_physical_type(expected)
    act_norm = executor.normalize_physical_type(actual)

    if exp_norm != act_norm:
        raise TestFailure(
            f"{table}.{column} has physical type {actual!r}, expected {expected!r} "
            f"for engine {engine_key}"
        )


def freshness(executor: BaseExecutor, table: str, ts_col: str, max_delay_minutes: int) -> None:
    """
    Fail if the latest timestamp in `ts_col` is older than `max_delay_minutes`.

    Behaviour:
    - First, run a lightweight probe on max(ts_col) to detect clearly wrong types
      (e.g. VARCHAR) and emit an actionable error instead of an engine-specific
      type/binder exception.
    - Then compute the delay in minutes using an engine-friendly expression:
      * Postgres / DuckDB: date_part('epoch', now() - max(ts_col)) / 60.0
      * Spark / Databricks:
        (unix_timestamp(current_timestamp()) - unix_timestamp(max(ts_col))) / 60.0

    For Spark-like connections we go straight to the unix_timestamp variant so
    we do not trigger noisy INVALID_EXTRACT_FIELD logs from the planner.
    """
    # 1) Probe type: read max(ts_col) and inspect the Python value that comes back.
    probe = _freshness_probe(executor, table, ts_col)

    # If max(...) comes back as a string, this is almost certainly a typed-as-VARCHAR
    # timestamp column. Fail with a clear hint instead of letting the engine throw.
    if probe is not None and isinstance(probe, str):
        raise TestFailure(
            f"[freshness] {table}.{ts_col} must be a TIMESTAMP-like column, but "
            f"max({ts_col}) returned a value of type {type(probe).__name__}.\n"
            "Hint: cast the column in your model, for example:\n"
            f"  select ..., CAST({ts_col} AS TIMESTAMP) as {ts_col}, ...\n"
            "and then reference that column in the freshness test."
        )

    # 2) Compute delay based on executor (engine-specific hook).
    delay, sql_used = executor.compute_freshness_delay_minutes(table, ts_col)

    dprint("freshness:", sql_used, "=>", delay)

    if delay is None or delay > max_delay_minutes:
        raise TestFailure(
            f"freshness of {table}.{ts_col} too old: "
            f"{format_duration_minutes(delay)} > "
            f"{format_duration_minutes(max_delay_minutes)}"
        )


# ===== Cross-table reconciliations (FF-310) ======================================


def _scalar_where(executor: BaseExecutor, table: str, expr: str, where: str | None = None) -> Any:
    """Return the first scalar from `SELECT {expr} FROM {table} [WHERE ...]`."""
    sql = f"select {expr} from {table}" + (f" where {where}" if where else "")
    dprint("reconcile:", sql)
    return _scalar(executor, sql)


def reconcile_equal(
    executor: BaseExecutor,
    left: dict,
    right: dict,
    abs_tolerance: float | None = None,
    rel_tolerance_pct: float | None = None,
) -> None:
    """Assert left == right within absolute and/or relative tolerances.

    Both sides are dictionaries: {"table": str, "expr": str, "where": Optional[str]}.
    If both tolerances are omitted, exact equality is enforced.
    """
    L = _scalar_where(executor, left["table"], left["expr"], left.get("where"))
    R = _scalar_where(executor, right["table"], right["expr"], right.get("where"))
    if L is None or R is None:
        raise TestFailure(f"One side is NULL (left={L}, right={R})")
    diff = abs(float(L) - float(R))

    # Absolute tolerance check
    if abs_tolerance is not None and diff <= float(abs_tolerance):
        return

    # Relative tolerance check (percentage)
    if rel_tolerance_pct is not None:
        denom = max(abs(float(R)), 1e-12)
        rel = diff / denom
        if (rel * 100.0) <= float(rel_tolerance_pct):
            return

    # If neither tolerance was provided, enforce strict equality via diff==0.
    if abs_tolerance is None and rel_tolerance_pct is None and diff == 0.0:
        return
    raise TestFailure(
        f"Reconcile equal failed: left={L}, right={R}, diff={diff}, "
        f"rel%={(diff / max(abs(float(R)), 1e-12)) * 100:.6f}"
    )


def reconcile_ratio_within(
    executor: BaseExecutor, left: dict, right: dict, min_ratio: float, max_ratio: float
) -> None:
    """Assert min_ratio <= (left/right) <= max_ratio."""
    L = _scalar_where(executor, left["table"], left["expr"], left.get("where"))
    R = _scalar_where(executor, right["table"], right["expr"], right.get("where"))
    if L is None or R is None:
        raise TestFailure(f"One side is NULL (left={L}, right={R})")
    eps = 1e-12
    denom = float(R) if abs(float(R)) > eps else eps
    ratio = float(L) / denom
    if not (float(min_ratio) <= ratio <= float(max_ratio)):
        raise TestFailure(
            f"Ratio {ratio:.6f} out of bounds [{min_ratio}, {max_ratio}] (L={L}, R={R})"
        )


def reconcile_diff_within(
    executor: BaseExecutor, left: dict, right: dict, max_abs_diff: float
) -> None:
    """Assert |left - right| <= max_abs_diff."""
    L = _scalar_where(executor, left["table"], left["expr"], left.get("where"))
    R = _scalar_where(executor, right["table"], right["expr"], right.get("where"))
    if L is None or R is None:
        raise TestFailure(f"One side is NULL (left={L}, right={R})")
    diff = abs(float(L) - float(R))
    if diff > float(max_abs_diff):
        raise TestFailure(f"Abs diff {diff} > max_abs_diff {max_abs_diff} (L={L}, R={R})")


def reconcile_coverage(
    executor: BaseExecutor,
    source: dict,
    target: dict,
    source_where: str | None = None,
    target_where: str | None = None,
) -> None:
    """Assert that every key from `source` exists in `target` (anti-join count == 0)."""
    s_tbl, s_key = source["table"], source["key"]
    t_tbl, t_key = target["table"], target["key"]
    s_w = f" where {source_where}" if source_where else ""
    t_w = f" where {target_where}" if target_where else ""
    sql = f"""
      with src as (select {s_key} as k from {s_tbl}{s_w}),
           tgt as (select {t_key} as k from {t_tbl}{t_w})
      select count(*) from src s
      left join tgt t on s.k = t.k
      where t.k is null
    """
    missing = _scalar(executor, sql)
    dprint("reconcile_coverage:", sql, "=>", missing)
    if missing and missing != 0:
        raise TestFailure(f"Coverage failed: {missing} source keys missing in target")


def relationships(
    executor: BaseExecutor,
    table: str,
    field: str,
    to_table: str,
    to_field: str,
    *,
    where: str | None = None,
    to_where: str | None = None,
) -> None:
    """
    Assert that every value from child `table.field` exists in parent `to_table.to_field`.
    Implemented as an anti-join count; failures report the number of missing keys.
    """
    child_where = f" where {where}" if where else ""
    parent_where = f" where {to_where}" if to_where else ""
    sql = f"""
      with child as (select {field} as k from {table}{child_where}),
           parent as (select {to_field} as k from {to_table}{parent_where})
      select count(*) from child c
      left join parent p on c.k = p.k
      where p.k is null
    """
    try:
        missing = _scalar(executor, sql)
    except Exception as e:
        raise _wrap_db_error("relationships", table, field, sql, e) from e
    dprint("relationships:", sql, "=>", missing)
    if missing and missing != 0:
        raise TestFailure(
            f"[relationships] {table}.{field} has {missing} orphan key(s) "
            f"missing in {to_table}.{to_field}"
        )
