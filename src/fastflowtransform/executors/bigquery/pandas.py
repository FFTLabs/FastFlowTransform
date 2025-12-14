# fastflowtransform/executors/bigquery/pandas.py
from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from time import perf_counter

import pandas as pd

from fastflowtransform.contracts.runtime.bigquery import BigQueryRuntimeContracts
from fastflowtransform.core import Node
from fastflowtransform.executors.bigquery.base import BigQueryBaseExecutor
from fastflowtransform.executors.query_stats import QueryStats
from fastflowtransform.typing import BadRequest, Client, LoadJobConfig, NotFound, bigquery


class BigQueryExecutor(BigQueryBaseExecutor[pd.DataFrame]):
    ENGINE_NAME: str = "bigquery"
    runtime_contracts: BigQueryRuntimeContracts
    """
    BigQuery executor (pandas DataFrames).
    ENV/Profiles typically use:
      - FF_BQ_PROJECT
      - FF_BQ_DATASET
      - FF_BQ_LOCATION (optional)
    """

    def __init__(
        self,
        project: str,
        dataset: str,
        location: str | None = None,
        client: Client | None = None,
        allow_create_dataset: bool = False,
    ):
        super().__init__(
            project=project,
            dataset=dataset,
            location=location,
            client=client,
            allow_create_dataset=allow_create_dataset,
        )
        self.runtime_contracts = BigQueryRuntimeContracts(self)

    # ---------- Python (Frames) ----------
    def _read_relation(self, relation: str, node: Node, deps: Iterable[str]) -> pd.DataFrame:
        q = f"SELECT * FROM {self._qualified_identifier(relation)}"
        try:
            job = self.client.query(q, location=self.location)
            return job.result().to_dataframe(create_bqstorage_client=True)
        except NotFound as e:
            # list existing tables to aid debugging
            tables = list(self.client.list_tables(f"{self.project}.{self.dataset}"))
            existing = [t.table_id for t in tables]
            raise RuntimeError(
                f"Dependency table not found: {self.project}.{self.dataset}.{relation}\n"
                f"Deps: {list(deps)}\nExisting in dataset: {existing}\n"
                "Hinweis: Seeds/Upstream-Modelle erzeugt? DATASET korrekt?"
            ) from e

    def _materialize_relation(self, relation: str, df: pd.DataFrame, node: Node) -> None:
        self._ensure_dataset()
        table_id = f"{self.project}.{self.dataset}.{relation}"
        job_config = LoadJobConfig(write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
        # Optionally extend dtype mapping here (NUMERIC/STRING etc.)
        start = perf_counter()
        try:
            job = self.client.load_table_from_dataframe(
                df,
                table_id,
                job_config=job_config,
                location=self.location,
            )
            job.result()
        except BadRequest as e:
            raise RuntimeError(f"BigQuery write failed: {table_id}\n{e}") from e
        else:
            duration_ms = int((perf_counter() - start) * 1000)
            self._record_dataframe_stats(df, duration_ms)

    def _create_view_over_table(self, view_name: str, backing_table: str, node: Node) -> None:
        """
        Convenience helper for a simple view on top of a backing table.
        """
        # Delegate to the shared base implementation
        self._create_or_replace_view_from_table(view_name, backing_table, node)

    def _frame_name(self) -> str:
        return "pandas"

    def _record_dataframe_stats(self, df: pd.DataFrame, duration_ms: int) -> None:
        rows = len(df)
        bytes_val = int(df.memory_usage(deep=True).sum()) if rows > 0 else 0
        self._record_query_stats(
            QueryStats(
                bytes_processed=bytes_val if bytes_val > 0 else None,
                rows=rows if rows > 0 else None,
                duration_ms=duration_ms,
            )
        )

        # ---- Unit-test helpers (pandas) ---------------------------------------

    def utest_read_relation(self, relation: str) -> pd.DataFrame:
        """
        Read a relation into a pandas DataFrame for unit-test assertions.
        """
        q = f"SELECT * FROM {self._qualified_identifier(relation)}"
        job = self.client.query(q, location=self.location)
        # Same convention as _read_relation: use BigQuery Storage if available
        return job.result().to_dataframe(create_bqstorage_client=True)

    def utest_load_relation_from_rows(self, relation: str, rows: list[dict]) -> None:
        """
        Load rows into a BigQuery table for unit tests (replace if exists).
        """
        self._ensure_dataset()
        table_id = f"{self.project}.{self.dataset}.{relation}"
        df = pd.DataFrame(rows)

        job_config = LoadJobConfig(write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)

        try:
            job = self.client.load_table_from_dataframe(
                df,
                table_id,
                job_config=job_config,
                location=self.location,
            )
            job.result()
        except BadRequest as e:
            raise RuntimeError(f"BigQuery utest write failed: {table_id}\n{e}") from e

    def utest_clean_target(self, relation: str) -> None:
        """
        For unit tests: drop any table/view with this name in the configured dataset.
        """
        table_id = f"{self.project}.{self.dataset}.{relation}"
        # BigQuery treats views & tables both as "tables" for deletion.
        try:
            # not_found_ok=True is available on the real client; our typing alias
            # should be compatible - if not, just ignore NotFound below.
            self.client.delete_table(table_id, not_found_ok=True)
        except NotFound:
            pass
        except TypeError:
            # For older client versions without not_found_ok, fall back:
            with suppress(NotFound):
                self.client.delete_table(table_id)
        except Exception:
            # Cleanup is best-effort in utests.
            pass
