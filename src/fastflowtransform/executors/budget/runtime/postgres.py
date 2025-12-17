from __future__ import annotations

import json
from typing import Any, Protocol

from fastflowtransform.executors.budget.core import BudgetGuard
from fastflowtransform.executors.budget.runtime.base import BaseBudgetRuntime, BudgetExecutor


class PostgresBudgetExecutor(BudgetExecutor, Protocol):
    schema: str | None

    def _execute_sql_maintenance(
        self,
        sql: str,
        *args: Any,
        conn: Any | None = None,
        set_search_path: bool = True,
        **kwargs: Any,
    ) -> Any: ...

    def _set_search_path(self, conn: Any) -> None: ...
    def _extract_select_like(self, sql_or_body: str) -> str: ...


class PostgresBudgetRuntime(BaseBudgetRuntime[PostgresBudgetExecutor]):
    """Postgres-specific budget runtime with EXPLAIN-based estimation."""

    DEFAULT_GUARD = BudgetGuard(
        env_var="FF_PG_MAX_BYTES",
        estimator_attr="_estimate_query_bytes",
        engine_label="Postgres",
        what="query",
    )

    _DEFAULT_PG_ROW_WIDTH = 128

    def __init__(self, executor: PostgresBudgetExecutor, guard: BudgetGuard | None = None):
        super().__init__(executor, guard)

    def estimate_query_bytes(self, sql: str) -> int | None:
        body = self.executor._extract_select_like(sql)
        lower = body.lstrip().lower()
        if not lower.startswith(("select", "with")):
            return None

        explain_sql = f"EXPLAIN (FORMAT JSON) {body}"

        try:
            raw = self.executor._execute_sql_maintenance(explain_sql, set_search_path=False)
        except Exception:
            return None

        if raw is None:
            return None

        try:
            data = json.loads(raw)
        except Exception:
            data = raw

        # Postgres JSON format: list with a single object
        if isinstance(data, list) and data:
            root = data[0]
        elif isinstance(data, dict):
            root = data
        else:
            return None

        plan = root.get("Plan")
        if not isinstance(plan, dict):
            if isinstance(root, dict) and "Node Type" in root:
                plan = root
            else:
                return None

        return self._estimate_bytes_from_plan(plan)

    def _estimate_bytes_from_plan(self, plan: dict[str, Any]) -> int | None:
        def _to_int(node: dict[str, Any], keys: tuple[str, ...]) -> int | None:
            for key in keys:
                val = node.get(key)
                if val is None:
                    continue
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
            return None

        rows = _to_int(plan, ("Plan Rows", "Plan_Rows", "Rows"))
        width = _to_int(plan, ("Plan Width", "Plan_Width", "Width"))

        if rows is None and width is None:
            return None

        candidate: int | None

        if rows is not None and width is not None:
            candidate = rows * width
        elif rows is not None:
            candidate = rows * self._DEFAULT_PG_ROW_WIDTH
        else:
            candidate = width

        if candidate is None or candidate <= 0:
            return None

        return int(candidate)
