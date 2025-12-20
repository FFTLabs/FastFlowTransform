from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from time import perf_counter
from typing import Any, Protocol, TypeVar

from fastflowtransform.executors._query_stats_adapter import QueryStatsAdapter, RowcountStatsAdapter
from fastflowtransform.executors.budget.core import BudgetGuard
from fastflowtransform.executors.query_stats.runtime.base import BaseQueryStatsRuntime


class BudgetExecutor(Protocol):
    """Minimal executor surface used by budget runtimes."""

    def _apply_budget_guard(self, guard: BudgetGuard | None, sql: str) -> int | None: ...
    def _is_budget_guard_active(self) -> bool: ...


E = TypeVar("E", bound=BudgetExecutor)


class BaseBudgetRuntime[E: BudgetExecutor]:
    """
    Base runtime for per-query budget enforcement.

    Executors compose this (like runtime contracts) and delegate guarded
    execution through it.
    """

    executor: E
    guard: BudgetGuard | None

    def __init__(self, executor: E, guard: BudgetGuard | None = None):
        self.executor = executor
        self.guard = guard or getattr(type(self), "DEFAULT_GUARD", None)

    def apply_guard(self, sql: str) -> int | None:
        return self.executor._apply_budget_guard(self.guard, sql)

    def run_sql(
        self,
        sql: str,
        *,
        exec_fn: Callable[[], Any],
        stats_runtime: BaseQueryStatsRuntime,
        rowcount_extractor: Callable[[Any], int | None] | None = None,
        extra_stats: Callable[[Any], Any] | None = None,
        estimate_fn: Callable[[str], int | None] | None = None,
        post_estimate_fn: Callable[[str, Any], int | None] | None = None,
        record_stats: bool = True,
        stats_adapter: QueryStatsAdapter | None = None,
    ) -> Any:
        estimated_bytes = self.apply_guard(sql)
        estimator = estimate_fn or getattr(self, "estimate_query_bytes", None)
        if (
            estimated_bytes is None
            and not self.executor._is_budget_guard_active()
            and callable(estimator)
        ):
            with suppress(Exception):
                estimated_bytes = estimator(sql)

        if not record_stats:
            return exec_fn()

        started = perf_counter()
        result = exec_fn()
        duration_ms = int((perf_counter() - started) * 1000)

        adapter = stats_adapter
        if adapter is None and (rowcount_extractor or post_estimate_fn or extra_stats):
            adapter = RowcountStatsAdapter(
                rowcount_extractor=rowcount_extractor,
                post_estimate_fn=post_estimate_fn,
                extra_stats=extra_stats,
                sql=sql,
            )

        stats_runtime.record_result(
            result,
            duration_ms=duration_ms,
            estimated_bytes=estimated_bytes,
            adapter=adapter,
            sql=sql,
        )

        return result
