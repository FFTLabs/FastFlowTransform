# fastflowtransform/contracts/runtime/duckdb.py
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from fastflowtransform.contracts.runtime.base import (
    BaseRuntimeContracts,
    ContractExecutor,
    RuntimeContractConfig,
    RuntimeContractContext,
    expected_physical_schema,
)


class DuckRuntimeContracts(BaseRuntimeContracts):
    """
    Runtime schema contracts for DuckDB.

    Uses the shared ContractExecutor protocol only; all Duck-specific
    behavior lives here, not in the executor.
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

        actual = self.executor.introspect_table_physical_schema(table)
        exp_lower = {k.lower(): v for k, v in expected.items()}

        problems: list[str] = []

        for col, expected_type in expected.items():
            key = col.lower()
            if key not in actual:
                problems.append(f"- missing column {col!r}")
                continue
            got = actual[key]
            if got.lower() != expected_type.lower():
                problems.append(f"- column {col!r}: expected type {expected_type!r}, got {got!r}")

        if not cfg.allow_extra_columns:
            extras = [c for c in actual if c not in exp_lower]
            if extras:
                problems.append(f"- extra columns present: {sorted(extras)}")

        if problems:
            raise RuntimeError(
                f"[contracts] DuckDB schema enforcement failed for {table}:\n" + "\n".join(problems)
            )

    def _ctas_raw(self, target: str, select_body: str) -> None:
        self.executor._execute_sql(f"create or replace table {target} as {select_body}")

    def _ctas_cast_via_subquery(
        self,
        *,
        ctx: RuntimeContractContext,
        select_body: str,
        expected: Mapping[str, str],
    ) -> None:
        """
        Cast mode for SQL models:

            create or replace table target as
            select cast(col as TYPE) as col, ...
            from ( <user select_body> ) as src
        """
        if not expected:
            self._ctas_raw(ctx.physical_table, select_body)
            return

        exp_lower = {k.lower(): v for k, v in expected.items()}

        projections: list[str] = [f"cast({col} as {typ}) as {col}" for col, typ in expected.items()]

        if ctx.config.allow_extra_columns:
            # stage in a temp table in the same "namespace" as physical_table
            tmp_name = f"{ctx.physical_table}__ff_contract_tmp".replace('"', "")
            self._ctas_raw(tmp_name, select_body)
            actual = self.executor.introspect_table_physical_schema(tmp_name)
            for c in actual:
                if c not in exp_lower:
                    projections.append(c)
            proj_sql = ", ".join(projections)
            self.executor._execute_sql(
                f"create or replace table {ctx.physical_table} as select {proj_sql} from {tmp_name}"
            )
            self.executor._execute_sql(f"drop table if exists {tmp_name}")
        else:
            proj_sql = ", ".join(projections)
            wrapped = f"select {proj_sql} from ({select_body}) as src"
            self._ctas_raw(ctx.physical_table, wrapped)

    def coerce_frame_schema(self, df: Any, ctx: RuntimeContractContext) -> Any:
        """
        Coerce a pandas.DataFrame to match DuckDB physical types in 'cast' mode.

        - Only runs when ctx.config.mode == "cast"
        - Only for pandas.DataFrame
        - Uses expected_physical_schema (per-table contracts)
        """
        if ctx.config.mode != "cast":
            return df

        if not isinstance(df, pd.DataFrame):
            return df

        expected = expected_physical_schema(
            executor=self.executor,
            contract=ctx.contract,
        )
        if not expected:
            return df

        coerced = df.copy()

        for col, phys_type in expected.items():
            if col not in coerced.columns:
                # Missing columns are handled by verification later.
                continue
            coerced[col] = self._coerce_series_to_type(coerced[col], phys_type)

        return coerced

    def _coerce_series_to_type(self, s: pd.Series, phys_type: str) -> pd.Series:
        """
        Best-effort coercion of a pandas Series to a DuckDB physical type.

        This is intentionally simple and will raise on invalid casts so that
        contract violations surface clearly.
        """
        t = (phys_type or "").strip().lower()
        if "(" in t:
            t = t.split("(", 1)[0].strip()

        # Nullable boolean
        if t in {"boolean", "bool"}:
            return s.astype("boolean")

        # Integers
        if t in {"tinyint", "smallint", "int", "integer", "bigint"}:
            return pd.to_numeric(s, errors="raise").astype("Int64")

        # Floats / decimals
        if t in {"float", "real", "double", "double precision", "decimal", "numeric"}:
            return pd.to_numeric(s, errors="raise").astype("float64")

        # Text / strings
        if t in {"char", "character", "varchar", "text", "string"}:
            # Use pandas' nullable string dtype
            return s.astype("string")

        # Date / timestamp
        if t in {"date"}:
            return pd.to_datetime(s, errors="raise").dt.date

        if t in {"timestamp", "timestamptz", "timestamp_ntz", "timestamp_ltz"}:
            return pd.to_datetime(s, errors="raise")

        # For types like json, uuid, varbinary, etc., we leave as-is and rely
        # on DuckDB's inference.
        return s

    # --- BaseRuntimeContracts hooks -------------------------------------

    def apply_sql_contracts(
        self,
        *,
        ctx: RuntimeContractContext,
        select_body: str,
    ) -> None:
        """
        Apply DuckDB runtime contracts for SQL models.
        """
        expected = expected_physical_schema(
            executor=self.executor,
            contract=ctx.contract,
        )

        mode = ctx.config.mode

        if mode == "off" or not expected:
            self._ctas_raw(ctx.physical_table, select_body)
            return

        if mode == "cast":
            self._ctas_cast_via_subquery(ctx=ctx, select_body=select_body, expected=expected)
            self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return

        if mode == "verify":
            self._ctas_raw(ctx.physical_table, select_body)
            self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return

        # unknown mode -> behave like off
        self._ctas_raw(ctx.physical_table, select_body)

    def verify_after_materialization(self, *, ctx: RuntimeContractContext) -> None:
        """
        If you want a second verification step (e.g. after incremental insert/merge),
        you can call this from the run-engine. For now it's a thin wrapper.
        """
        expected = expected_physical_schema(
            executor=self.executor,
            contract=ctx.contract,
        )
        if not expected:
            return
        if ctx.config.mode not in {"verify", "cast"}:
            return
        self._verify(
            table=ctx.physical_table,
            expected=expected,
            cfg=ctx.config,
        )
