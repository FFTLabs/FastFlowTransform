# fastflowtransform/executors/_query_stats_adapter.py
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from fastflowtransform.executors.query_stats.core import QueryStats


class QueryStatsAdapter(Protocol):
    """Adapter interface to normalize stats extraction across engines."""

    def collect(
        self, result: Any, *, duration_ms: int | None, estimated_bytes: int | None
    ) -> QueryStats: ...


class RowcountStatsAdapter:
    """
    Default stats adapter for DB-API style executors that expose rowcount.
    Preserves existing post_estimate/extra_stats hook behavior.
    """

    def __init__(
        self,
        *,
        rowcount_extractor: Callable[[Any], int | None] | None = None,
        post_estimate_fn: Callable[[str, Any], int | None] | None = None,
        extra_stats: Callable[[Any], QueryStats | None] | None = None,
        sql: str | None = None,
    ) -> None:
        self.rowcount_extractor = rowcount_extractor
        self.post_estimate_fn = post_estimate_fn
        self.extra_stats = extra_stats
        self.sql = sql

    def collect(
        self, result: Any, *, duration_ms: int | None, estimated_bytes: int | None
    ) -> QueryStats:
        rows: int | None = None
        if self.rowcount_extractor is not None:
            try:
                rows = self.rowcount_extractor(result)
            except Exception:
                rows = None

        stats = QueryStats(bytes_processed=estimated_bytes, rows=rows, duration_ms=duration_ms)

        if stats.bytes_processed is None and self.post_estimate_fn is not None:
            try:
                post_estimate = self.post_estimate_fn(self.sql or "", result)
            except Exception:
                post_estimate = None
            if post_estimate is not None:
                stats = QueryStats(
                    bytes_processed=post_estimate,
                    rows=stats.rows,
                    duration_ms=stats.duration_ms,
                )

        if self.extra_stats is not None:
            try:
                extra = self.extra_stats(result)
            except Exception:
                extra = None
            if extra:
                stats = QueryStats(
                    bytes_processed=extra.bytes_processed
                    if extra.bytes_processed is not None
                    else stats.bytes_processed,
                    rows=extra.rows if extra.rows is not None else stats.rows,
                    duration_ms=(
                        extra.duration_ms if extra.duration_ms is not None else stats.duration_ms
                    ),
                )

        return stats


class JobStatsAdapter:
    """
    Generic job-handle stats extractor (BigQuery/Snowflake/Spark-like objects).
    Mirrors the previous `_record_query_job_stats` heuristics.
    """

    def collect(self, job: Any) -> QueryStats:
        def _safe_int(val: Any) -> int | None:
            try:
                if val is None:
                    return None
                return int(val)
            except Exception:
                return None

        bytes_processed = _safe_int(
            getattr(job, "total_bytes_processed", None) or getattr(job, "bytes_processed", None)
        )

        rows = _safe_int(
            getattr(job, "num_dml_affected_rows", None)
            or getattr(job, "total_rows", None)
            or getattr(job, "rowcount", None)
        )

        duration_ms: int | None = None
        try:
            started = getattr(job, "started", None)
            ended = getattr(job, "ended", None)
            if started is not None and ended is not None:
                duration_ms = int((ended - started).total_seconds() * 1000)
        except Exception:
            duration_ms = None

        return QueryStats(bytes_processed=bytes_processed, rows=rows, duration_ms=duration_ms)


class SparkDataFrameStatsAdapter:
    """
    Adapter for Spark DataFrames that mirrors existing Databricks behaviour:
    - bytes via provided bytes_fn (plan-based best effort)
    - rows left as None
    - duration passed through
    """

    def __init__(self, bytes_fn: Callable[[Any], int | None]) -> None:
        self.bytes_fn = bytes_fn

    def collect(
        self, result: Any, *, duration_ms: int | None, estimated_bytes: int | None = None
    ) -> QueryStats:
        df = result
        bytes_val = estimated_bytes
        if bytes_val is None:
            try:
                bytes_val = self.bytes_fn(df)
            except Exception:
                bytes_val = None

        return QueryStats(
            bytes_processed=bytes_val if bytes_val is not None and bytes_val > 0 else None,
            rows=None,
            duration_ms=duration_ms,
        )
