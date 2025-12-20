from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol, TypeVar

from fastflowtransform.executors._query_stats_adapter import (
    JobStatsAdapter,
    QueryStatsAdapter,
    RowcountStatsAdapter,
)
from fastflowtransform.executors.query_stats.core import QueryStats


class QueryStatsExecutor(Protocol):
    """Minimal executor surface used by query-stats runtimes."""

    def _record_query_stats(self, stats: QueryStats) -> None: ...


E = TypeVar("E", bound=QueryStatsExecutor)


@dataclass
class QueryTimer:
    started_at: float


class BaseQueryStatsRuntime[E: QueryStatsExecutor]:
    """
    Base runtime for collecting per-query stats.

    Executors compose this (like runtime contracts) and delegate stat recording
    so the run engine can aggregate per-node metrics.
    """

    executor: E

    def __init__(self, executor: E):
        self.executor = executor

    def start_timer(self) -> QueryTimer:
        return QueryTimer(started_at=perf_counter())

    def record_result(
        self,
        result: Any,
        *,
        timer: QueryTimer | None = None,
        duration_ms: int | None = None,
        estimated_bytes: int | None = None,
        adapter: QueryStatsAdapter | None = None,
        sql: str | None = None,
        rowcount_extractor: Callable[[Any], int | None] | None = None,
        extra_stats: Callable[[Any], QueryStats | None] | None = None,
        post_estimate_fn: Callable[[str, Any], int | None] | None = None,
    ) -> QueryStats:
        """
        Collect stats from a result object and record them on the executor.

        Either pass a timer (from start_timer) or an explicit duration_ms.
        If no adapter is given, a simple QueryStats with bytes/duration is recorded.
        """
        if duration_ms is None and timer is not None:
            duration_ms = int((perf_counter() - timer.started_at) * 1000)

        stats_adapter = adapter
        if stats_adapter is None and (rowcount_extractor or extra_stats or post_estimate_fn):
            stats_adapter = RowcountStatsAdapter(
                rowcount_extractor=rowcount_extractor,
                extra_stats=extra_stats,
                post_estimate_fn=post_estimate_fn,
                sql=sql,
            )

        if stats_adapter is None:
            stats = QueryStats(bytes_processed=estimated_bytes, rows=None, duration_ms=duration_ms)
        else:
            stats = stats_adapter.collect(
                result, duration_ms=duration_ms, estimated_bytes=estimated_bytes
            )

        self.executor._record_query_stats(stats)
        return stats

    def record_job(self, job: Any) -> QueryStats:
        adapter = JobStatsAdapter()
        stats = adapter.collect(job)
        self.executor._record_query_stats(stats)
        return stats
