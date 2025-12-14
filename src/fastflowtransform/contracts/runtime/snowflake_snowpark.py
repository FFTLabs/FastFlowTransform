# fastflowtransform/contracts/runtime/snowflake_snowpark.py
from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from fastflowtransform.contracts.runtime.base import (
    BaseRuntimeContracts,
    ContractExecutor,
    RuntimeContractConfig,
    RuntimeContractContext,
    expected_physical_schema,
)


def _norm_type(t: str | None) -> str | None:
    if not t:
        return None
    s = str(t).strip().upper()
    s = " ".join(s.split())

    # normalize synonyms users might write in contracts
    base_map = {
        "INT": "NUMBER",
        "INTEGER": "NUMBER",
        "BIGINT": "NUMBER",
        "SMALLINT": "NUMBER",
        "TINYINT": "NUMBER",
        "DECIMAL": "NUMBER",
        "NUMERIC": "NUMBER",
        "STRING": "VARCHAR",
        "TEXT": "VARCHAR",
        "CHAR": "VARCHAR",
        "CHARACTER": "VARCHAR",
        "DOUBLE PRECISION": "DOUBLE",
    }
    # preserve params if present
    if "(" in s and s.endswith(")"):
        base, params = s.split("(", 1)
        base = base.strip()
        params = params[:-1].strip().replace(" ", "")
        base = base_map.get(base, base)
        return f"{base}({params})"

    return base_map.get(s, s)


def _types_match(expected: str, got: str) -> bool:
    exp = _norm_type(expected)
    g = _norm_type(got)
    if not exp or not g:
        return False

    # if expected includes params, require exact match
    if "(" in exp:
        return exp == g

    # otherwise compare base only (allow actual to carry params)
    exp_base = exp.split("(", 1)[0]
    got_base = g.split("(", 1)[0]
    return exp_base == got_base


class SnowflakeSnowparkRuntimeContracts(BaseRuntimeContracts):
    """
    Runtime schema contracts for Snowflake (Snowpark).

    SQL models:
      - off:   CREATE OR REPLACE TABLE AS <select_body>
      - verify: CTAS then compare information_schema.columns
      - cast:  CTAS via subquery with CAST(...) projections, then verify

    Python models (Snowpark DataFrame):
      - verify: write.save_as_table(... overwrite) then verify
      - cast:   create temp view from DF, do CTAS with casts, then verify
    """

    def __init__(self, executor: ContractExecutor):
        super().__init__(executor)

    # --- tiny helpers -----------------------------------------------------

    def _exec(self, sql: str) -> None:
        """
        Snowpark executes on action; collect() is the simplest action.
        """
        res = self.executor._execute_sql(sql)
        collect = getattr(res, "collect", None)
        if callable(collect):
            collect()
        else:
            # best-effort fallback (shouldn't happen for real Snowpark)
            _ = res

    def _safe_tmp_name(self, base: str) -> str:
        txt = re.sub(r"[^A-Za-z0-9_]+", "_", base or "tmp").strip("_")
        if not txt:
            txt = "tmp"
        return f"__FF_CONTRACT_{txt}".upper()

    def _verify(
        self,
        *,
        table: str,
        expected: Mapping[str, str],
        cfg: RuntimeContractConfig,
    ) -> None:
        if not expected:
            return

        actual = self.executor.introspect_table_physical_schema(table)
        exp_lower = {k.lower(): v for k, v in expected.items()}

        problems: list[str] = []

        for col, expected_type in expected.items():
            key = col.lower()
            if key not in actual:
                problems.append(f"- missing column {col!r}")
                continue
            got = actual[key]
            if not _types_match(expected_type, got):
                problems.append(f"- column {col!r}: expected {expected_type!r}, got {got!r}")

        if not cfg.allow_extra_columns:
            extras = [c for c in actual if c not in exp_lower]
            if extras:
                problems.append(f"- extra columns present: {sorted(extras)}")

        if problems:
            raise RuntimeError(
                f"[contracts] Snowflake schema enforcement failed for {table}:\n"
                + "\n".join(problems)
            )

    def _ctas_raw(self, target: str, select_body: str) -> None:
        self._exec(f"CREATE OR REPLACE TABLE {target} AS {select_body}")

    def _ctas_cast_via_subquery(
        self,
        *,
        ctx: RuntimeContractContext,
        select_body: str,
        expected: Mapping[str, str],
    ) -> None:
        if not expected:
            self._ctas_raw(ctx.physical_table, select_body)
            return

        exp_lower = {k.lower(): v for k, v in expected.items()}
        projections: list[str] = [f"CAST({col} AS {typ}) AS {col}" for col, typ in expected.items()]

        if ctx.config.allow_extra_columns:
            tmp = self._safe_tmp_name(f"{ctx.relation}_tmp")
            # temp table holds the raw output so we can discover extras
            self._exec(f"CREATE OR REPLACE TEMPORARY TABLE {tmp} AS {select_body}")

            try:
                actual = self.executor.introspect_table_physical_schema(tmp)
                for c in actual:
                    if c not in exp_lower:
                        # c is already lowercased key; emit identifier as-is
                        projections.append(c)

                proj_sql = ", ".join(projections)
                self._exec(
                    f"CREATE OR REPLACE TABLE {ctx.physical_table} AS SELECT {proj_sql} FROM {tmp}"
                )
            finally:
                with suppress(Exception):
                    self._exec(f"DROP TABLE IF EXISTS {tmp}")
        else:
            proj_sql = ", ".join(projections)
            wrapped = f"SELECT {proj_sql} FROM ({select_body}) AS SRC"
            self._ctas_raw(ctx.physical_table, wrapped)

    # --- BaseRuntimeContracts hooks ---------------------------------------

    def apply_sql_contracts(
        self,
        *,
        ctx: RuntimeContractContext,
        select_body: str,
    ) -> None:
        expected = expected_physical_schema(executor=self.executor, contract=ctx.contract)
        mode = ctx.config.mode

        if mode == "off" or not expected:
            self._ctas_raw(ctx.physical_table, select_body)
            return

        if mode == "cast":
            if not expected:
                raise RuntimeError(
                    f"[contracts] cast mode enabled for {ctx.relation!r} "
                    "but no physical schema could be resolved."
                )
            self._ctas_cast_via_subquery(ctx=ctx, select_body=select_body, expected=expected)
            self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return

        if mode == "verify":
            self._ctas_raw(ctx.physical_table, select_body)
            self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return

        self._ctas_raw(ctx.physical_table, select_body)

    def materialize_python(
        self,
        *,
        ctx: RuntimeContractContext,
        df: Any,
    ) -> bool:
        """
        Snowpark Python models return a Snowpark DataFrame.

        - verify: write to table via save_as_table then verify
        - cast:   create temp view from df, then CTAS with CASTs, then verify

        Return True because we fully materialized the target table here.
        """
        mode = ctx.config.mode
        if mode == "off":
            return False

        expected = expected_physical_schema(executor=self.executor, contract=ctx.contract)

        if mode == "cast" and not expected:
            raise RuntimeError(
                f"[contracts] cast mode enabled for {ctx.relation!r} "
                "but no physical schema could be resolved."
            )

        # Basic Snowpark DF surface check
        if not (
            hasattr(df, "write")
            and hasattr(df, "schema")
            and callable(getattr(df, "collect", None))
        ):
            return False

        if mode == "verify":
            # Let Snowpark write in its native path, then verify
            df.write.save_as_table(ctx.physical_table, mode="overwrite")
            if expected:
                self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return True

        if mode == "cast":
            tmp_view = self._safe_tmp_name(f"{ctx.relation}_src")
            create_view = getattr(df, "create_or_replace_temp_view", None)
            if not callable(create_view):
                # fallback: if temp views aren't available, don't take over
                return False

            create_view(tmp_view)
            try:
                select_body = f"SELECT * FROM {tmp_view}"
                self._ctas_cast_via_subquery(ctx=ctx, select_body=select_body, expected=expected)
                self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
                return True
            finally:
                with suppress(Exception):
                    self._exec(f"DROP VIEW IF EXISTS {tmp_view}")

        return False

    def verify_after_materialization(self, *, ctx: RuntimeContractContext) -> None:
        expected = expected_physical_schema(executor=self.executor, contract=ctx.contract)
        if not expected:
            return
        if ctx.config.mode not in {"verify", "cast"}:
            return
        self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
