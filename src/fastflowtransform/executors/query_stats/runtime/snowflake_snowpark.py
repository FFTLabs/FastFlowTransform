from __future__ import annotations

from typing import Any

from fastflowtransform.executors.query_stats.core import QueryStats
from fastflowtransform.executors.query_stats.runtime.base import (
    BaseQueryStatsRuntime,
    QueryStatsExecutor,
)


class SnowflakeSnowparkQueryStatsRuntime(BaseQueryStatsRuntime[QueryStatsExecutor]):
    """Snowflake Snowpark stats helpers."""

    def rowcount_from_result(self, result: Any) -> int | None:
        rc = getattr(result, "rowcount", None)
        if isinstance(rc, int) and rc >= 0:
            return rc
        return None

    def record_dataframe(self, df: Any, duration_ms: int) -> QueryStats:
        budget_runtime = getattr(self.executor, "runtime_budget", None)

        bytes_estimate: int | None = None
        if budget_runtime is not None:
            try:
                bytes_estimate = budget_runtime.dataframe_bytes(df)
            except Exception:
                bytes_estimate = None

        if bytes_estimate is not None and bytes_estimate <= 0:
            bytes_estimate = None

        stats = QueryStats(
            bytes_processed=bytes_estimate,
            rows=None,
            duration_ms=duration_ms,
        )
        self.executor._record_query_stats(stats)
        return stats
