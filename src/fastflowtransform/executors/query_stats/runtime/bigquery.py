from __future__ import annotations

from typing import Any

import pandas as pd

from fastflowtransform.executors.query_stats.core import QueryStats, _TrackedQueryJob
from fastflowtransform.executors.query_stats.runtime.base import (
    BaseQueryStatsRuntime,
    QueryStatsExecutor,
)


class BigQueryQueryStatsRuntime(BaseQueryStatsRuntime[QueryStatsExecutor]):
    """BigQuery-specific stats helpers."""

    def wrap_job(self, job: Any) -> _TrackedQueryJob:
        return _TrackedQueryJob(job, on_complete=self.record_job)

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
