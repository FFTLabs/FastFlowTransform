from __future__ import annotations

from typing import Any, Protocol

from fastflowtransform.executors.budget.core import BudgetGuard
from fastflowtransform.executors.budget.runtime.base import BaseBudgetRuntime, BudgetExecutor
from fastflowtransform.typing import bigquery


class BigQueryBudgetExecutor(BudgetExecutor, Protocol):
    project: str
    dataset: str
    location: str | None
    client: Any


class BigQueryBudgetRuntime(BaseBudgetRuntime[BigQueryBudgetExecutor]):
    """BigQuery budget runtime using dry-run estimation."""

    DEFAULT_GUARD = BudgetGuard(
        env_var="FF_BQ_MAX_BYTES",
        estimator_attr="runtime_budget_estimate_query_bytes",
        engine_label="BigQuery",
        what="query",
    )

    def estimate_query_bytes(self, sql: str) -> int | None:
        """
        Estimate bytes for a BigQuery SQL statement using a dry-run.

        Returns the estimated bytes, or None if estimation is not possible.
        """
        cfg = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
        )
        if self.executor.dataset:
            cfg.default_dataset = bigquery.DatasetReference(
                self.executor.project, self.executor.dataset
            )

        try:
            job = self.executor.client.query(
                sql,
                job_config=cfg,
                location=self.executor.location,
            )
            job.result()
        except Exception:
            return None

        try:
            return int(getattr(job, "total_bytes_processed", 0) or 0)
        except Exception:
            return None
