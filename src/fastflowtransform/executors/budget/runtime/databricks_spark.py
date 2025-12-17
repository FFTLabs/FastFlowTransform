from __future__ import annotations

from typing import Any, Protocol

from fastflowtransform.executors._query_stats_adapter import SparkDataFrameStatsAdapter
from fastflowtransform.executors.budget.core import BudgetGuard
from fastflowtransform.executors.budget.runtime.base import BaseBudgetRuntime, BudgetExecutor


class DatabricksSparkBudgetExecutor(BudgetExecutor, Protocol):
    spark: Any

    def _selectable_body(self, sql: str) -> str: ...


class DatabricksSparkBudgetRuntime(BaseBudgetRuntime[DatabricksSparkBudgetExecutor]):
    """Databricks/Spark budget runtime using logical-plan stats for estimation."""

    DEFAULT_GUARD = BudgetGuard(
        env_var="FF_SPK_MAX_BYTES",
        estimator_attr="runtime_budget_estimate_query_bytes",
        engine_label="Databricks/Spark",
        what="query",
    )

    def __init__(self, executor: DatabricksSparkBudgetExecutor, guard: BudgetGuard | None = None):
        super().__init__(executor, guard)
        self._default_size: int | None = self.detect_default_size()

    def estimate_query_bytes(self, sql: str) -> int | None:
        return self._spark_plan_bytes(sql)

    def detect_default_size(self) -> int:
        """
        Detect Spark's defaultSizeInBytes sentinel.

        - Prefer spark.sql.defaultSizeInBytes if available.
        - Fall back to Long.MaxValue (2^63 - 1) otherwise.
        """
        try:
            conf_val = self.executor.spark.conf.get("spark.sql.defaultSizeInBytes")
            if conf_val is not None:
                return int(conf_val)
        except Exception:
            # config not set / older Spark / weird environment
            pass

        # Fallback: Spark uses Long.MaxValue by default
        return 2**63 - 1  # 9223372036854775807

    def spark_stats_adapter(self, sql: str) -> SparkDataFrameStatsAdapter:
        """
        Build a SparkDataFrameStatsAdapter tied to this runtime's estimation logic.
        """

        def _bytes(df: Any) -> int | None:
            estimate = self.dataframe_bytes(df)
            if estimate is not None:
                return estimate
            return self.estimate_query_bytes(sql)

        return SparkDataFrameStatsAdapter(_bytes)

    # ---- Shared helpers for Spark stats ----
    def dataframe_bytes(self, df: Any) -> int | None:
        try:
            jdf = getattr(df, "_jdf", None)
            if jdf is None:
                return None

            qe = jdf.queryExecution()
            jplan = qe.optimizedPlan()

            if self._jplan_uses_default_size(jplan):
                return None

            stats = jplan.stats()
            size_attr = getattr(stats, "sizeInBytes", None)
            size_val = size_attr() if callable(size_attr) else size_attr
            return self._parse_spark_stats_size(size_val)
        except Exception:
            return None

    def _spark_plan_bytes(self, sql: str) -> int | None:
        """
        Inspect the optimized logical plan via the JVM and return sizeInBytes
        as an integer, or None if not available. This does not execute the query.
        """
        try:
            normalized = self.executor._selectable_body(sql).rstrip(";\n\t ")
            if not normalized:
                normalized = sql
        except Exception:
            normalized = sql

        stmt = normalized.lstrip().lower()
        if not stmt.startswith(("select", "with")):
            # DDL/DML statements should not be executed twice.
            return None

        try:
            df = self.executor.spark.sql(normalized)

            jdf = getattr(df, "_jdf", None)
            if jdf is None:
                return None

            qe = jdf.queryExecution()
            jplan = qe.optimizedPlan()

            if self._jplan_uses_default_size(jplan):
                return None

            stats = jplan.stats()
            size_attr = getattr(stats, "sizeInBytes", None)
            size_val = size_attr() if callable(size_attr) else size_attr

            return self._parse_spark_stats_size(size_val)
        except Exception:
            return None

    def _jplan_uses_default_size(self, jplan: Any) -> bool:
        """
        Recursively walk a JVM LogicalPlan and return True if any node's
        stats.sizeInBytes equals spark.sql.defaultSizeInBytes.
        """
        spark_default_size = self._default_size
        if spark_default_size is None:
            return False

        try:
            stats = jplan.stats()
            size_val = stats.sizeInBytes()
            size_int = int(str(size_val))
            if size_int == spark_default_size:
                return True
        except Exception:
            # ignore stats errors and keep walking
            pass

        # children() is a Scala Seq[LogicalPlan]; iterate via .size() / .apply(i)
        try:
            children = jplan.children()
            n = children.size()
            for idx in range(n):
                child = children.apply(idx)
                if self._jplan_uses_default_size(child):
                    return True
        except Exception:
            # if we can't inspect children, stop here
            pass

        return False

    def _parse_spark_stats_size(self, size_val: Any) -> int | None:
        if size_val is None:
            return None
        try:
            size_int = int(str(size_val))
        except Exception:
            return None
        return size_int if size_int > 0 else None
