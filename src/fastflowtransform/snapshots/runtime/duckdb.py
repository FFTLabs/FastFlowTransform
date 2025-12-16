from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from fastflowtransform.snapshots.runtime.base import BaseSnapshotRuntime, SnapshotExecutor


class DuckSnapshotExecutor(SnapshotExecutor, Protocol):
    def _qualified(self, relation: str, *, quoted: bool = True) -> str: ...


class DuckSnapshotRuntime(BaseSnapshotRuntime[DuckSnapshotExecutor]):
    """
    Snapshot runtime for DuckDB, extracted from the old SnapshotSqlMixin.
    """

    # ---- Engine hooks -----------------------------------------------------
    def _snapshot_target_identifier(self, rel_name: str) -> str:
        return self.executor._qualified(rel_name)

    def _snapshot_current_timestamp(self) -> str:
        return "current_timestamp"

    def _snapshot_null_timestamp(self) -> str:
        return "cast(null as timestamp)"

    def _snapshot_null_hash(self) -> str:
        return "cast(null as varchar)"

    def _snapshot_hash_expr(self, check_cols: list[str], src_alias: str) -> str:
        concat_expr = self._snapshot_concat_expr(check_cols, src_alias)
        return f"cast(md5({concat_expr}) as varchar)"

    def _snapshot_cast_as_string(self, expr: str) -> str:
        return f"cast({expr} as varchar)"

    def _snapshot_source_ref(
        self, rel_name: str, select_body: str
    ) -> tuple[str, Callable[[], None]]:
        src_view_name = f"__ff_snapshot_src_{rel_name}".replace(".", "_")
        src_quoted = self.executor._quote_identifier(src_view_name)
        self.executor._execute_sql(f"create or replace temp view {src_quoted} as {select_body}")

        def _cleanup() -> None:
            self.executor._execute_sql(f"drop view if exists {src_quoted}")

        return src_quoted, _cleanup
