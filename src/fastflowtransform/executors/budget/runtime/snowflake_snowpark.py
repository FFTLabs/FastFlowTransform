from __future__ import annotations

import json
from contextlib import suppress
from typing import Any, Protocol

from fastflowtransform.executors.budget.core import BudgetGuard
from fastflowtransform.executors.budget.runtime.base import BaseBudgetRuntime, BudgetExecutor


class SnowflakeSnowparkBudgetExecutor(BudgetExecutor, Protocol):
    session: Any

    def _selectable_body(self, sql: str) -> str: ...


class SnowflakeSnowparkBudgetRuntime(BaseBudgetRuntime[SnowflakeSnowparkBudgetExecutor]):
    """Snowflake Snowpark budget runtime using EXPLAIN for estimation."""

    DEFAULT_GUARD = BudgetGuard(
        env_var="FF_SF_MAX_BYTES",
        estimator_attr="runtime_budget_estimate_query_bytes",
        engine_label="Snowflake",
        what="query",
    )

    def estimate_query_bytes(self, sql: str) -> int | None:
        """
        Best-effort Snowflake bytes estimation using EXPLAIN USING JSON.
        Mirrors the previous executor-side logic.
        """
        try:
            body = self.executor._selectable_body(sql)
        except Exception:
            body = sql

        try:
            rows = self.executor.session.sql(f"EXPLAIN USING JSON {body}").collect()
            if not rows:
                return None

            parts: list[str] = []
            for r in rows:
                try:
                    parts.append(str(r[0]))
                except Exception:
                    as_dict: dict[str, Any] = getattr(r, "asDict", lambda: {})()
                    if as_dict:
                        parts.extend(str(v) for v in as_dict.values())

            plan_text = "\n".join(parts).strip()
            if not plan_text:
                return None

            try:
                plan_data = json.loads(plan_text)
            except Exception:
                return None

            bytes_val = self._extract_bytes_from_plan(plan_data)
            if bytes_val is None or bytes_val <= 0:
                return None
            return bytes_val
        except Exception:
            # Any parsing / EXPLAIN issues → no estimate, guard skipped
            return None

    def dataframe_bytes(self, df: Any) -> int | None:
        """
        Best-effort bytes estimate for a Snowpark DataFrame.
        """
        try:
            sql_text = self._snowpark_df_sql(df)
            if not isinstance(sql_text, str) or not sql_text.strip():
                return None
            return self.estimate_query_bytes(sql_text)
        except Exception:
            return None

    def _extract_bytes_from_plan(self, plan_data: Any) -> int | None:
        def _to_int(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except Exception:
                return None

        if isinstance(plan_data, dict):
            global_stats = plan_data.get("GlobalStats") or plan_data.get("globalStats")
            if isinstance(global_stats, dict):
                candidate = _to_int(
                    global_stats.get("bytesAssigned") or global_stats.get("bytes_assigned")
                )
                if candidate:
                    return candidate
            for val in plan_data.values():
                bytes_val = self._extract_bytes_from_plan(val)
                if bytes_val:
                    return bytes_val
        elif isinstance(plan_data, list):
            for item in plan_data:
                bytes_val = self._extract_bytes_from_plan(item)
                if bytes_val:
                    return bytes_val
        return None

    def _snowpark_df_sql(self, df: Any) -> str | None:
        """
        Extract the main SQL statement for a Snowpark DataFrame.

        Uses the documented public APIs:
        - DataFrame.queries -> {"queries": [sql1, sql2, ...], "post_actions": [...]}
        - Optionally falls back to df._plan.sql() if needed.
        """
        queries_dict = getattr(df, "queries", None)

        if isinstance(queries_dict, dict):
            queries = queries_dict.get("queries")
            if isinstance(queries, list) and queries:
                candidates = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
                if candidates:
                    return max(candidates, key=len)

        plan = getattr(df, "_plan", None)
        if plan is not None:
            with suppress(Exception):
                simplify = getattr(plan, "simplify", None)
                if callable(simplify):
                    simplified = simplify()
                    to_sql = getattr(simplified, "sql", None)
                    if callable(to_sql):
                        sql = to_sql()
                        if isinstance(sql, str) and sql.strip():
                            return sql.strip()

            with suppress(Exception):
                to_sql = getattr(plan, "sql", None)
                if callable(to_sql):
                    sql = to_sql()
                    if isinstance(sql, str) and sql.strip():
                        return sql.strip()

        return None
