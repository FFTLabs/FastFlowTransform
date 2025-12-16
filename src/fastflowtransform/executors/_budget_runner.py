from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from time import perf_counter
from typing import Any

from fastflowtransform.executors._query_stats_adapter import QueryStatsAdapter, RowcountStatsAdapter
from fastflowtransform.executors.budget.core import BudgetGuard
from fastflowtransform.executors.query_stats.core import QueryStats


def run_sql_with_budget(
    executor: Any,
    sql: str,
    *,
    guard: BudgetGuard,
    exec_fn: Callable[[], Any],
    rowcount_extractor: Callable[[Any], int | None] | None = None,
    extra_stats: Callable[[Any], QueryStats | None] | None = None,
    estimate_fn: Callable[[str], int | None] | None = None,
    post_estimate_fn: Callable[[str, Any], int | None] | None = None,
    record_stats: bool = True,
    stats_adapter: QueryStatsAdapter | None = None,
) -> Any:
    """
    Shared helper for guarded SQL execution with timing + stats recording.

    executor      object exposing _apply_budget_guard, _is_budget_guard_active, _record_query_stats
    sql           statement (used for guard + optional estimator)
    exec_fn       callable that executes the statement and returns a result/job handle
    rowcount_extractor(result) -> int|None    best-effort row count (non-negative only)
    extra_stats(result) -> QueryStats|None    allows engines to override/extend stats post-exec
    estimate_fn(sql) -> int|None              optional best-effort bytes estimate when guard
                                              inactive
    post_estimate_fn(sql, result) -> int|None optional post-exec fallback when bytes are still None
    record_stats  set False to skip immediate stats (e.g., when a job handle records on .result())
    """
    estimated_bytes = executor._apply_budget_guard(guard, sql)
    if (
        estimated_bytes is None
        and not executor._is_budget_guard_active()
        and estimate_fn is not None
    ):
        with suppress(Exception):
            estimated_bytes = estimate_fn(sql)

    # If stats should be deferred (BigQuery job handles), just run and return.
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
    if adapter is None:
        stats = QueryStats(bytes_processed=estimated_bytes, rows=None, duration_ms=duration_ms)
    else:
        stats = adapter.collect(result, duration_ms=duration_ms, estimated_bytes=estimated_bytes)

    executor._record_query_stats(stats)
    return result
