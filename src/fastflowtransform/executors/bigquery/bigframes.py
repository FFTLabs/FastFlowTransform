# fastflowtransform/executors/bigquery/bigframes.py
from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from typing import Any

import pandas as pd

from fastflowtransform.contracts.runtime.bigquery import BigQueryRuntimeContracts
from fastflowtransform.core import Node
from fastflowtransform.executors.bigquery.base import BigQueryBaseExecutor
from fastflowtransform.typing import (
    BadRequest,
    BFDataFrame,
    BigQueryOptions,
    LoadJobConfig,
    NotFound,
    bf_global_session,
    bigframes,
    bigquery,
)


class BigQueryBFExecutor(BigQueryBaseExecutor[BFDataFrame]):
    ENGINE_NAME: str = "bigquery"
    runtime_contracts: BigQueryRuntimeContracts

    def __init__(
        self,
        project: str,
        dataset: str,
        location: str | None = None,
        allow_create_dataset: bool = False,
    ):
        if not project:
            raise RuntimeError("BigFrames executor requires FF_BQ_PROJECT to be set.")
        if not location:
            raise RuntimeError(
                "BigFrames executor requires FF_BQ_LOCATION to be set. "
                "Use the dataset's region (e.g., EU or US)."
            )
        super().__init__(
            project=project,
            dataset=dataset,
            location=location,
            allow_create_dataset=allow_create_dataset,
        )
        self.runtime_contracts = BigQueryRuntimeContracts(self)

        try:
            ctx = BigQueryOptions(
                project=project,
                location=location,
            )
            self.session = bigframes.Session(context=ctx)
        except Exception as exc:
            raise RuntimeError(
                "Failed to initialize BigFrames session. Verify FF_BQ_PROJECT, "
                "FF_BQ_DATASET, and FF_BQ_LOCATION are set for the active profile. "
                f"{exc}"
            ) from exc

    def run_python(self, node: Node) -> None:
        """
        Execute Python models with a session scoped to this executor.

        We avoid mutating the process-wide default session; instead we
        temporarily set the executor session as the active global session so
        model code using bpd.DataFrame(...) picks up the configured location,
        then restore afterward.
        """
        ctx = bf_global_session._GlobalSessionContext(self.session)
        with ctx:
            super().run_python(node)

    # ---------- Python (Frames) ----------
    def _read_relation(self, relation: str, node: Node, deps: Iterable[str]) -> BFDataFrame:
        table_id = f"{self.project}.{self.dataset}.{relation}"
        try:
            return self.session.read_gbq(table_id)
        except NotFound as e:
            existing = [
                t.table_id for t in self.client.list_tables(f"{self.project}.{self.dataset}")
            ]
            raise RuntimeError(
                f"Dependency table not found: {table_id}\n"
                f"Deps: {list(deps)}\nExisting in dataset: {existing}\n"
                "Hinweis: Seeds/Upstream-Modelle erzeugt? DATASET korrekt?"
            ) from e

    def _materialize_relation(self, relation: str, df: BFDataFrame, node: Node) -> None:
        self._ensure_dataset()
        table_id = f"{self.project}.{self.dataset}.{relation}"

        to_gbq = getattr(df, "to_gbq", None)
        if callable(to_gbq):
            to_gbq(table_id, if_exists="replace")
            return

        # Fallback only when it is truly a method (not a column name!)
        mat = getattr(df, "materialize", None)
        if callable(mat):
            mat(table=table_id, mode="overwrite")
            return

        raise RuntimeError(
            "BigQuery DataFrames: Ergebnis nicht materialisierbar. "
            "Erwarte df.to_gbq(...) oder df.materialize(...)."
        )

    # ---- Required-columns validation tuned for BigFrames ----
    def _validate_required(
        self,
        node_name: str,
        inputs: Any,
        requires: dict[str, set[str]],
    ) -> None:
        if not requires:
            return

        def cols(bf_df: BFDataFrame) -> set[str]:
            if hasattr(bf_df, "columns"):
                return set(map(str, list(bf_df.columns)))
            if hasattr(bf_df, "schema") and hasattr(bf_df.schema, "names"):
                return set(bf_df.schema.names)
            return set()

        errs: list[str] = []
        if self._is_frame(inputs):
            # Single input frame case
            need = next(iter(requires.values()), set())
            miss = need - cols(inputs)
            if miss:
                errs.append(f"- missing columns: {sorted(miss)}")
        else:
            # Mapping {rel -> frame}
            for rel, need in requires.items():
                if rel not in inputs:
                    errs.append(f"- missing dependency key '{rel}'")
                    continue
                miss = need - cols(inputs[rel])
                if miss:
                    errs.append(f"- [{rel}] missing: {sorted(miss)}")

        if errs:
            raise ValueError(
                f"Required columns check failed for BigQuery DataFrames model '{node_name}'.\n"
                + "\n".join(errs)
            )

    def _columns_of(self, frame: BFDataFrame) -> list[str]:
        if hasattr(frame, "columns"):
            return [str(c) for c in list(frame.columns)]
        if hasattr(frame, "schema") and hasattr(frame.schema, "names"):
            return list(frame.schema.names)
        return []

    def _is_frame(self, obj: Any) -> bool:
        if obj is None:
            return False
        return (
            callable(getattr(obj, "to_gbq", None))
            or callable(getattr(obj, "materialize", None))
            or hasattr(obj, "columns")
        )

    def _frame_name(self) -> str:
        return "BigQuery DataFrame (BigFrames)"

        # ---- Unit-test helpers (pandas-facing) --------------------------------

    def utest_read_relation(self, relation: str) -> pd.DataFrame:
        """
        Read a relation into a pandas DataFrame for unit-test assertions.

        Even though this executor uses BigFrames for normal execution,
        utests compare pandas DataFrames, so we convert.
        """
        q = f"SELECT * FROM {self._qualified_identifier(relation)}"
        job = self.client.query(q, location=self.location)
        return job.result().to_dataframe(create_bqstorage_client=True)

    def utest_load_relation_from_rows(self, relation: str, rows: list[dict]) -> None:
        """
        Load rows into a BigQuery table for unit tests (replace if exists).

        Implementation uses the raw BigQuery client with pandas, which is
        perfectly fine for test input setup.
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

        try:
            self.client.delete_table(table_id, not_found_ok=True)
        except NotFound:
            pass
        except TypeError:
            with suppress(NotFound):
                self.client.delete_table(table_id)
        except Exception:
            # Best-effort; don't make the whole test run fail because cleanup hiccupped.
            pass
