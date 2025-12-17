from __future__ import annotations

from typing import Any

from fastflowtransform.executors._query_stats_adapter import SparkDataFrameStatsAdapter
from fastflowtransform.executors.query_stats.core import QueryStats
from fastflowtransform.executors.query_stats.runtime.base import (
    BaseQueryStatsRuntime,
    QueryStatsExecutor,
)


class DatabricksSparkQueryStatsRuntime(BaseQueryStatsRuntime[QueryStatsExecutor]):
    """Spark-specific stats helpers."""

    def rowcount_from_result(self, result: Any) -> int | None:
        # Avoid triggering extra Spark actions; rely on estimates instead.
        rc = getattr(result, "count", None)
        if isinstance(rc, int) and rc >= 0:
            return rc
        return None

    def record_dataframe(self, df: Any, duration_ms: int) -> QueryStats:
        budget_runtime = getattr(self.executor, "runtime_budget", None)
        adapter = (
            budget_runtime.spark_stats_adapter("<dataframe>")
            if budget_runtime is not None
            else SparkDataFrameStatsAdapter(lambda _: None)
        )
        stats = adapter.collect(df, duration_ms=duration_ms, estimated_bytes=None)
        self.executor._record_query_stats(stats)
        return stats
