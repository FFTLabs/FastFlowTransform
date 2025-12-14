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


class BigQueryRuntimeContracts(BaseRuntimeContracts):
    """
    Runtime schema contracts for BigQuery.

    Notes:
    - executor._execute_sql returns a job-like object; we force execution via .result()
      when present.
    - CAST mode uses BigQuery's `src.* EXCEPT(col1, col2, ...)` to retain extra columns.
    """

    def __init__(self, executor: ContractExecutor):
        super().__init__(executor)

    # --- helpers ---------------------------------------------------------

    def _exec(self, sql: str) -> Any:
        res = self.executor._execute_sql(sql)
        # BigQuery QueryJob / our _TrackedQueryJob: execute via .result()
        result_fn = getattr(res, "result", None)
        if callable(result_fn):
            return result_fn()
        # Spark-like fallbacks (harmless here, but keeps this helper generic)
        collect_fn = getattr(res, "collect", None)
        if callable(collect_fn):
            return collect_fn()
        return res

    def _verify(
        self,
        *,
        table: str,
        expected: Mapping[str, str],
        cfg: RuntimeContractConfig,
    ) -> None:
        if not expected:
            return

        actual = self.executor.introspect_table_physical_schema(table)  # {lower_name: TYPE}
        exp_lower = {k.lower(): v for k, v in expected.items()}

        problems: list[str] = []

        for col, expected_type in expected.items():
            key = col.lower()
            if key not in actual:
                problems.append(f"- missing column {col!r}")
                continue
            got = actual[key]
            if str(got).lower() != str(expected_type).lower():
                problems.append(f"- column {col!r}: expected type {expected_type!r}, got {got!r}")

        if not cfg.allow_extra_columns:
            extras = [c for c in actual if c not in exp_lower]
            if extras:
                problems.append(f"- extra columns present: {sorted(extras)}")

        if problems:
            raise RuntimeError(
                f"[contracts] BigQuery schema enforcement failed for {table}:\n"
                + "\n".join(problems)
            )

    def _ctas_raw(self, target: str, select_body: str) -> None:
        # BigQuery supports CREATE OR REPLACE TABLE ... AS <select>
        self._exec(f"create or replace table {target} as {select_body}")

    def _ctas_cast_via_subquery(
        self,
        *,
        ctx: RuntimeContractContext,
        select_body: str,
        expected: Mapping[str, str],
    ) -> None:
        """
        Cast mode for SQL models:

          CREATE OR REPLACE TABLE target AS
          SELECT
            CAST(src.col AS TYPE) AS col,
            ...
            [, src.* EXCEPT(col, ...)]   -- if allow_extra_columns
          FROM ( <user select_body> ) AS src
        """
        if not expected:
            self._ctas_raw(ctx.physical_table, select_body)
            return

        casts = [f"cast(src.{col} as {typ}) as {col}" for col, typ in expected.items()]

        if ctx.config.allow_extra_columns:
            # Keep extras without duplicating expected columns
            except_list = ", ".join(expected.keys())
            casts.append(f"src.* except ({except_list})")

        proj_sql = ", ".join(casts)
        wrapped = f"select {proj_sql} from ({select_body}) as src"
        self._ctas_raw(ctx.physical_table, wrapped)

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

        # unknown mode -> behave like off
        self._ctas_raw(ctx.physical_table, select_body)

    def verify_after_materialization(self, *, ctx: RuntimeContractContext) -> None:
        expected = expected_physical_schema(executor=self.executor, contract=ctx.contract)
        if not expected:
            return
        if ctx.config.mode not in {"verify", "cast"}:
            return
        self._verify(table=ctx.physical_table, expected=expected, cfg=ctx.config)
