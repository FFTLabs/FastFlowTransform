from __future__ import annotations

from typing import Any

import pandas as pd

from fastflowtransform.executors.query_stats.core import QueryStats
from fastflowtransform.executors.query_stats.runtime.base import (
    BaseQueryStatsRuntime,
    QueryStatsExecutor,
)


class DuckQueryStatsRuntime(BaseQueryStatsRuntime[QueryStatsExecutor]):
    """DuckDB-specific runtime logic for stats extraction."""

    def rowcount_from_result(self, result: Any) -> int | None:
        rc = getattr(result, "rowcount", None)
        if isinstance(rc, int) and rc >= 0:
            return rc
        return None

    def record_dataframe(self, df: pd.DataFrame, duration_ms: int) -> QueryStats:
        rows = len(df)
        bytes_estimate = int(df.memory_usage(deep=True).sum()) if rows > 0 else 0
        bytes_val = bytes_estimate if bytes_estimate > 0 else None
        stats = QueryStats(
            bytes_processed=bytes_val,
            rows=rows if rows > 0 else None,
            duration_ms=duration_ms,
        )
        self.executor._record_query_stats(stats)
        return stats
