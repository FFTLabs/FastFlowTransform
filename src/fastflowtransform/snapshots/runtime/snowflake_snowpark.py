from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from fastflowtransform.executors.common import _q_ident
from fastflowtransform.snapshots.runtime.base import BaseSnapshotRuntime, SnapshotExecutor


class SnowflakeSnapshotExecutor(SnapshotExecutor, Protocol):
    def _qualified(self, relation: str, *, quoted: bool = True) -> str: ...


class SnowflakeSnowparkSnapshotRuntime(BaseSnapshotRuntime[SnowflakeSnapshotExecutor]):
    """
    Snapshot runtime for Snowflake Snowpark, matching legacy mixin hooks.
    """

    # ---- Engine hooks -----------------------------------------------------
    def _snapshot_target_identifier(self, rel_name: str) -> str:
        return self.executor._qualified(rel_name)

    def _snapshot_current_timestamp(self) -> str:
        return "CURRENT_TIMESTAMP()"

    def _snapshot_create_keyword(self) -> str:
        return "CREATE OR REPLACE TABLE"

    def _snapshot_null_timestamp(self) -> str:
        return "CAST(NULL AS TIMESTAMP)"

    def _snapshot_null_hash(self) -> str:
        return "CAST(NULL AS VARCHAR)"

    def _snapshot_hash_expr(self, check_cols: list[str], src_alias: str) -> str:
        concat_expr = self._snapshot_concat_expr(check_cols, src_alias)
        return f"CAST(MD5({concat_expr}) AS VARCHAR)"

    def _snapshot_cast_as_string(self, expr: str) -> str:
        return f"CAST({expr} AS VARCHAR)"

    def _snapshot_source_ref(
        self, rel_name: str, select_body: str
    ) -> tuple[str, Callable[[], None]]:
        src_name = f"__ff_snapshot_src_{rel_name}".replace(".", "_")
        src_quoted = _q_ident(src_name)
        self.executor._execute_sql(
            f"CREATE OR REPLACE TEMPORARY VIEW {src_quoted} AS {select_body}"
        )

        def _cleanup() -> None:
            self.executor._execute_sql(f"DROP VIEW IF EXISTS {src_quoted}")

        return src_quoted, _cleanup
