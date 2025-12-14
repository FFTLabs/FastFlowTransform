# fastflowtransform/contracts/runtime/databricks_spark.py
from __future__ import annotations

from collections.abc import Mapping
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
    s = str(t).strip().lower()

    # collapse whitespace
    s = " ".join(s.split())

    # split base + params, e.g. decimal(10,2)
    base = s
    params = None
    if "(" in s and s.endswith(")"):
        base, params = s.split("(", 1)
        base = base.strip()
        params = params[:-1].strip().replace(" ", "")  # strip trailing ')'

    # normalize common synonyms between Spark + human-written contracts
    base_map = {
        "integer": "int",
        "int4": "int",
        "long": "bigint",
        "int8": "bigint",
        "bool": "boolean",
        "varchar": "string",
        "char": "string",
        "character": "string",
        "text": "string",
        "str": "string",
        "double precision": "double",
        "numeric": "decimal",
    }
    base = base_map.get(base, base)

    if params:
        return f"{base}({params})"
    return base


def _types_match(expected: str, got: str) -> bool:
    exp = _norm_type(expected)
    g = _norm_type(got)
    if not exp or not g:
        return False

    # If expected includes params (decimal(10,2)), require exact match.
    if "(" in exp:
        return exp == g

    # Otherwise compare base types only (allow actual to carry params).
    exp_base = exp.split("(", 1)[0]
    got_base = g.split("(", 1)[0]
    return exp_base == got_base


class DatabricksSparkRuntimeContracts(BaseRuntimeContracts):
    """
    Runtime schema contracts for Spark / DatabricksSparkExecutor.

    - verify: write output table, then compare contract vs actual Spark schema
    - cast: apply Spark casts before writing, then verify
    """

    def __init__(self, executor: ContractExecutor):
        super().__init__(executor)

    # --- helpers ---------------------------------------------------------

    def _save_df_as_table(self, *, ctx: RuntimeContractContext, df: Any) -> None:
        """
        Delegate to DatabricksSparkExecutor._save_df_as_table (format handler aware).
        """
        save = getattr(self.executor, "_save_df_as_table", None)
        if not callable(save):
            raise RuntimeError(
                "[contracts] Spark runtime contracts require executor._save_df_as_table(...)"
            )

        # Preserve existing storage behavior if available
        storage_meta = {}
        storage_fn = getattr(self.executor, "_storage_meta", None)
        if callable(storage_fn):
            try:
                storage_meta = storage_fn(ctx.node, ctx.physical_table)
            except Exception:
                storage_meta = {}

        save(ctx.physical_table, df, storage=storage_meta)

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
                f"[contracts] Spark schema enforcement failed for {table}:\n" + "\n".join(problems)
            )

    def _cast_df(
        self,
        *,
        df: Any,
        expected: Mapping[str, str],
        allow_extra: bool,
    ) -> Any:
        """
        Return a projected DataFrame:
          - expected cols casted to expected Spark SQL types
          - optionally keep extra cols
        """
        # Use your lazy import helper to avoid hard pyspark deps at import time
        from fastflowtransform.executors._spark_imports import get_spark_functions  # noqa: PLC0415

        F = get_spark_functions()

        if not expected:
            return df

        cols = list(getattr(df, "columns", []) or [])
        col_map = {c.lower(): c for c in cols}  # actual name by lower key

        exp_lower = {k.lower(): v for k, v in expected.items()}

        # Ensure expected columns exist
        missing = [c for c in exp_lower if c not in col_map]
        if missing:
            raise RuntimeError(f"[contracts] missing expected columns: {sorted(missing)}")

        projections: list[Any] = []
        for low_name, typ in exp_lower.items():
            real = col_map[low_name]
            projections.append(F.col(real).cast(str(typ)).alias(real))

        if allow_extra:
            for c in cols:
                if c.lower() not in exp_lower:
                    projections.append(F.col(c))

        return df.select(*projections)

    # --- BaseRuntimeContracts hooks -------------------------------------

    def apply_sql_contracts(
        self,
        *,
        ctx: RuntimeContractContext,
        select_body: str,
    ) -> None:
        expected = expected_physical_schema(executor=self.executor, contract=ctx.contract)
        mode = ctx.config.mode

        # Spark executor doesn't do CTAS SQL; it materializes via DF + _save_df_as_table
        df = self.executor._execute_sql(select_body)

        if mode == "off" or not expected:
            self._save_df_as_table(ctx=ctx, df=df)
            return

        if mode == "cast":
            if not expected:
                raise RuntimeError(
                    f"[contracts] cast mode enabled for {ctx.relation!r} "
                    "but no physical schema could be resolved."
                )
            df2 = self._cast_df(
                df=df, expected=expected, allow_extra=ctx.config.allow_extra_columns
            )
            self._save_df_as_table(ctx=ctx, df=df2)
            self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return

        if mode == "verify":
            self._save_df_as_table(ctx=ctx, df=df)
            self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return

        # unknown mode -> behave like off
        self._save_df_as_table(ctx=ctx, df=df)

    def materialize_python(
        self,
        *,
        ctx: RuntimeContractContext,
        df: Any,
    ) -> bool:
        """
        Spark Python models return a Spark DataFrame. Enforce contracts here
        so we can CAST before writing.
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

        # basic Spark DF shape check (avoid importing pyspark types)
        if not hasattr(df, "schema") or not hasattr(df, "columns") or not hasattr(df, "select"):
            return False

        if mode == "cast":
            df2 = self._cast_df(
                df=df, expected=expected, allow_extra=ctx.config.allow_extra_columns
            )
            self._save_df_as_table(ctx=ctx, df=df2)
            self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return True

        if mode == "verify":
            self._save_df_as_table(ctx=ctx, df=df)
            if expected:
                self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
            return True

        return False

    def verify_after_materialization(self, *, ctx: RuntimeContractContext) -> None:
        expected = expected_physical_schema(executor=self.executor, contract=ctx.contract)
        if not expected:
            return
        if ctx.config.mode not in {"verify", "cast"}:
            return
        self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
