from __future__ import annotations

from typing import Protocol

from fastflowtransform.snapshots.runtime.base import BaseSnapshotRuntime, SnapshotExecutor


class BigQuerySnapshotExecutor(SnapshotExecutor, Protocol):
    project: str
    dataset: str
    location: str | None

    def _ensure_dataset(self) -> None: ...

    def _qualified_identifier(
        self, relation: str, project: str | None = None, dataset: str | None = None
    ) -> str: ...


class BigQuerySnapshotRuntime(BaseSnapshotRuntime[BigQuerySnapshotExecutor]):
    """
    Snapshot runtime for BigQuery, matching the legacy mixin hooks.
    """

    # ---- Engine hooks -----------------------------------------------------
    def _snapshot_prepare_target(self) -> None:
        self.executor._ensure_dataset()

    def _snapshot_target_identifier(self, rel_name: str) -> str:
        return self.executor._qualified_identifier(
            rel_name,
            project=getattr(self.executor, "project", None),
            dataset=getattr(self.executor, "dataset", None),
        )

    def _snapshot_current_timestamp(self) -> str:
        return "CURRENT_TIMESTAMP()"

    def _snapshot_null_timestamp(self) -> str:
        return "CAST(NULL AS TIMESTAMP)"

    def _snapshot_null_hash(self) -> str:
        return "CAST(NULL AS STRING)"

    def _snapshot_hash_expr(self, check_cols: list[str], src_alias: str) -> str:
        concat_expr = self._snapshot_concat_expr(check_cols, src_alias)
        return f"TO_HEX(MD5({concat_expr}))"

    def _snapshot_cast_as_string(self, expr: str) -> str:
        return f"CAST({expr} AS STRING)"

    # BigQuery uses inline source (default), so no override for _snapshot_source_ref
