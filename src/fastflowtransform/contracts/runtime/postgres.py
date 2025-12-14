# fastflowtransform/contracts/runtime/postgres.py
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from contextlib import suppress
from typing import Any

import pandas as pd

from fastflowtransform.contracts.runtime.base import (
    BaseRuntimeContracts,
    ContractExecutor,
    RuntimeContractConfig,
    RuntimeContractContext,
    expected_physical_schema,
)


def _q_ident(ident: str) -> str:
    return '"' + (ident or "").replace('"', '""') + '"'


def _safe_tmp_name(prefix: str, relation: str) -> str:
    # keep it simple + deterministic
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", relation or "rel").strip("_")
    if not base:
        base = "rel"
    return f"{prefix}{base}"


def _lit(s: str) -> str:
    # SQL string literal (no params in our simple executor path)
    return "'" + (s or "").replace("'", "''") + "'"


def _base_type(s: str | None) -> str:
    """
    Compare only base types (ignore modifiers like varchar(255), numeric(18,0)).
    """
    if not s:
        return ""
    t = re.sub(r"\s+", " ", str(s).strip().lower())
    # strip trailing "(...)"
    t = re.sub(r"\s*\(.*\)\s*$", "", t)
    return t


def _split_schema_table(executor: Any, table: str) -> tuple[str, str]:
    # table may be '"schema"."table"' or 'schema.table' or 'table'
    cleaned = table.replace('"', "").strip()
    parts = [p for p in cleaned.split(".") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    # fallback schema from executor if available
    schema = getattr(executor, "schema", None) or "public"
    return str(schema), parts[-1] if parts else cleaned


def _fetchall(executor: Any, sql: str) -> list[Any]:
    res = executor.execute_test_sql(sql)
    fetchall = getattr(res, "fetchall", None)
    if callable(fetchall):
        data = fetchall()
        if isinstance(data, Iterable):
            return list(data)
        return []
    # fallback: try iterating
    try:
        return list(res)
    except Exception:
        return []


def _pg_expected_canon(executor: Any, expected_type: str) -> str:
    """
    Let Postgres validate & canonicalize the expected type.
    - to_regtype() returns NULL if type is invalid
    - format_type() returns canonical SQL name
    """
    q = f"select format_type(to_regtype({_lit(expected_type)})::oid, NULL)"
    rows = _fetchall(executor, q)
    if not rows or rows[0][0] is None:
        raise ValueError(f"invalid Postgres type {expected_type!r}")
    return _base_type(str(rows[0][0]))


def _pg_actual_schema(executor: Any, table: str) -> dict[str, str]:
    """
    Read actual column types from pg_catalog via format_type(atttypid, atttypmod).
    This is more robust than information_schema.data_type (and includes canonical naming).
    """
    schema, rel = _split_schema_table(executor, table)
    q = f"""
    select a.attname as col, format_type(a.atttypid, a.atttypmod) as typ
    from pg_attribute a
    join pg_class c on c.oid = a.attrelid
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = {_lit(schema)}
      and c.relname  = {_lit(rel)}
      and a.attnum > 0
      and not a.attisdropped
    """
    rows = _fetchall(executor, q)
    out: dict[str, str] = {}
    for r in rows:
        col = str(r[0]).lower()
        out[col] = _base_type(str(r[1]))
    return out


class PostgresRuntimeContracts(BaseRuntimeContracts):
    """
    Runtime schema contracts for Postgres.

    Like DuckRuntimeContracts, this composes an executor but can use
    Postgres-specific attributes (engine/schema) when doing Python DF staging.
    """

    def __init__(self, executor: ContractExecutor):
        super().__init__(executor)

    # --- helpers ---------------------------------------------------------

    def _verify(
        self,
        *,
        table: str,
        expected: Mapping[str, str],
        cfg: RuntimeContractConfig,
    ) -> None:
        if not expected:
            return

        actual = _pg_actual_schema(self.executor, table)
        exp_lower = {k.lower(): v for k, v in expected.items()}

        problems: list[str] = []

        for expected_col, expected_type in expected.items():
            key = expected_col.lower()
            if key not in actual:
                problems.append(f"- missing column {expected_col!r}")
                continue

            try:
                exp_canon = _pg_expected_canon(self.executor, expected_type)
            except ValueError as e:
                problems.append(f"- column {expected_col!r}: {e}")
                continue

            got_canon = actual[key]
            if got_canon != exp_canon:
                problems.append(
                    f"- column {expected_col!r}: expected type {expected_type!r} "
                    f"(canon={exp_canon!r}), got (canon={got_canon!r})"
                )

        if not cfg.allow_extra_columns:
            extras = [c for c in actual if c not in exp_lower]
            if extras:
                problems.append(f"- extra columns present: {sorted(extras)}")

        if problems:
            raise RuntimeError(
                f"[contracts] Postgres schema enforcement failed for {table}:\n"
                + "\n".join(problems)
            )

    def _drop_create_ctas(self, target: str, select_body: str) -> None:
        # Postgres: no CREATE OR REPLACE TABLE
        self.executor._execute_sql(f"drop table if exists {target} cascade")
        self.executor._execute_sql(f"create table {target} as {select_body}")
        # best-effort stats
        with suppress(Exception):
            self.executor._execute_sql(f"analyze {target}")

    def _ctas_cast_via_subquery(
        self,
        *,
        ctx: RuntimeContractContext,
        select_body: str,
        expected: Mapping[str, str],
    ) -> None:
        if not expected:
            self._drop_create_ctas(ctx.physical_table, select_body)
            return

        exp_lower = {k.lower(): v for k, v in expected.items()}

        # Always quote columns in projections
        projections: list[str] = [
            f"cast({_q_ident(col)} as {typ}) as {_q_ident(col)}" for col, typ in expected.items()
        ]

        if ctx.config.allow_extra_columns:
            # stage into a real table (not TEMP), because PostgresExecutor may use
            # different pooled connections per statement.
            tmp = _safe_tmp_name("__ff_contract_tmp_", ctx.relation)
            qtmp = _q_ident(tmp)

            self.executor._execute_sql(f"drop table if exists {qtmp} cascade")
            self.executor._execute_sql(f"create table {qtmp} as {select_body}")

            try:
                actual = self.executor.introspect_table_physical_schema(tmp)
                for c in actual:
                    if c not in exp_lower:
                        # actual keys are lowercase; we need the real column name as stored.
                        # safest: select by identifier using the lowercase key as-is.
                        projections.append(_q_ident(c))

                proj_sql = ", ".join(projections)
                self._drop_create_ctas(
                    ctx.physical_table,
                    f"select {proj_sql} from {qtmp}",
                )
            finally:
                with suppress(Exception):
                    self.executor._execute_sql(f"drop table if exists {qtmp} cascade")
        else:
            proj_sql = ", ".join(projections)
            wrapped = f"select {proj_sql} from ({select_body}) as src"
            self._drop_create_ctas(ctx.physical_table, wrapped)

    # --- BaseRuntimeContracts hooks -------------------------------------

    def apply_sql_contracts(
        self,
        *,
        ctx: RuntimeContractContext,
        select_body: str,
    ) -> None:
        expected = expected_physical_schema(executor=self.executor, contract=ctx.contract)
        mode = ctx.config.mode

        if mode == "off" or not expected:
            self._drop_create_ctas(ctx.physical_table, select_body)
            return

        if mode == "cast":
            self._ctas_cast_via_subquery(ctx=ctx, select_body=select_body, expected=expected)
            self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return

        if mode == "verify":
            self._drop_create_ctas(ctx.physical_table, select_body)
            self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return

        # unknown mode -> behave like off
        self._drop_create_ctas(ctx.physical_table, select_body)

    def verify_after_materialization(self, *, ctx: RuntimeContractContext) -> None:
        expected = expected_physical_schema(executor=self.executor, contract=ctx.contract)
        if not expected:
            return
        if ctx.config.mode not in {"verify", "cast"}:
            return
        self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)

    def materialize_python(
        self,
        *,
        ctx: RuntimeContractContext,
        df: Any,
    ) -> bool:
        """
        Enforce contracts for pandas DataFrames by staging via to_sql, then
        creating the target via Postgres CASTs (cast mode) or verifying (verify mode).
        """
        mode = ctx.config.mode
        if mode == "off":
            return False
        if not isinstance(df, pd.DataFrame):
            return False

        expected = expected_physical_schema(executor=self.executor, contract=ctx.contract)

        if mode == "cast" and not expected:
            raise RuntimeError(
                f"[contracts] cast mode enabled for {ctx.relation!r} "
                "but no physical schema could be resolved."
            )

        # We rely on PostgresExecutor providing `.engine` and `.schema` (like your executor does).
        engine = getattr(self.executor, "engine", None)
        schema = getattr(self.executor, "schema", None)
        if engine is None:
            return False

        tmp = _safe_tmp_name("__ff_py_src_", ctx.relation)
        qtmp = _q_ident(tmp)

        # 1) stage df -> table
        with suppress(Exception):
            self.executor._execute_sql(f"drop table if exists {qtmp} cascade")

        df.to_sql(
            tmp,
            engine,
            if_exists="replace",
            index=False,
            schema=schema,
            method="multi",
        )

        try:
            select_body = f"select * from {qtmp}"

            if mode == "cast":
                self._ctas_cast_via_subquery(
                    ctx=ctx,
                    select_body=select_body,
                    expected=expected,
                )
                self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)

            elif mode == "verify":
                self._drop_create_ctas(ctx.physical_table, select_body)
                if expected:
                    self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            else:
                return False

            return True
        finally:
            with suppress(Exception):
                self.executor._execute_sql(f"drop table if exists {qtmp} cascade")
