from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from fastflowtransform.executors.common import _q_ident
from fastflowtransform.snapshots.runtime.base import BaseSnapshotRuntime, SnapshotExecutor


class PostgresSnapshotExecutor(SnapshotExecutor, Protocol):
    def _qualified(self, relation: str, *, quoted: bool = True) -> str: ...


class PostgresSnapshotRuntime(BaseSnapshotRuntime[PostgresSnapshotExecutor]):
    """
    Snapshot runtime for Postgres, extracted from the legacy mixin hooks.
    """

    # ---- Engine hooks -----------------------------------------------------
    def _snapshot_target_identifier(self, rel_name: str) -> str:
        return self.executor._qualified(rel_name)

    def _snapshot_current_timestamp(self) -> str:
        return "current_timestamp"

    def _snapshot_null_timestamp(self) -> str:
        return "cast(null as timestamp)"

    def _snapshot_null_hash(self) -> str:
        return "cast(null as text)"

    def _snapshot_hash_expr(self, check_cols: list[str], src_alias: str) -> str:
        concat_expr = self._snapshot_concat_expr(check_cols, src_alias)
        return f"md5({concat_expr})"

    def _snapshot_cast_as_string(self, expr: str) -> str:
        return f"cast({expr} as text)"

    def _snapshot_source_ref(
        self, rel_name: str, select_body: str
    ) -> tuple[str, Callable[[], None]]:
        src_name = f"__ff_snapshot_src_{rel_name}".replace(".", "_")
        src_q = _q_ident(src_name)
        self.executor._execute_sql(f"drop table if exists {src_q}")
        self.executor._execute_sql(f"create temporary table {src_q} as {select_body}")

        def _cleanup() -> None:
            self.executor._execute_sql(f"drop table if exists {src_q}")

        return src_q, _cleanup
