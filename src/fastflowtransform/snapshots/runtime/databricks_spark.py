from __future__ import annotations

from contextlib import suppress
from functools import reduce
from typing import Any, Protocol

from fastflowtransform.core import Node, relation_for
from fastflowtransform.executors._spark_imports import get_spark_functions, get_spark_window
from fastflowtransform.logging import echo, echo_debug
from fastflowtransform.snapshots.core import resolve_snapshot_config
from fastflowtransform.snapshots.runtime.base import SnapshotExecutor
from fastflowtransform.typing import SDF, SparkSession


class DatabricksSnapshotExecutor(SnapshotExecutor, Protocol):
    spark: SparkSession

    def _physical_identifier(self, identifier: str, *, database: str | None = None) -> str: ...

    def _storage_meta(self, node: Node | None, relation: str) -> dict[str, Any]: ...

    def _save_df_as_table(
        self, identifier: str, df: SDF, *, storage: dict[str, Any] | None = None
    ) -> None: ...


class DatabricksSparkSnapshotRuntime:
    """
    Snapshot runtime for Databricks/Spark (Delta/Parquet/Iceberg), extracted
    from the executor. Uses Spark DataFrame operations instead of SQL strings.
    """

    SNAPSHOT_VALID_FROM_COL = "_ff_valid_from"
    SNAPSHOT_VALID_TO_COL = "_ff_valid_to"
    SNAPSHOT_IS_CURRENT_COL = "_ff_is_current"
    SNAPSHOT_HASH_COL = "_ff_snapshot_hash"
    SNAPSHOT_UPDATED_AT_COL = "_ff_updated_at"

    executor: DatabricksSnapshotExecutor

    def __init__(self, executor: DatabricksSnapshotExecutor):
        self.executor = executor

    def run_snapshot_sql(self, node: Node, env: Any) -> None:
        ex = self.executor
        F = get_spark_functions()

        meta = self._validate_snapshot_node(node)
        cfg = resolve_snapshot_config(node, meta)

        strategy = cfg.strategy
        unique_key = cfg.unique_key
        updated_at = cfg.updated_at
        check_cols = cfg.check_cols

        body, rel_name, physical = self._snapshot_sql_body(node, env)

        vf = self.SNAPSHOT_VALID_FROM_COL
        vt = self.SNAPSHOT_VALID_TO_COL
        is_cur = self.SNAPSHOT_IS_CURRENT_COL
        hash_col = self.SNAPSHOT_HASH_COL
        upd_meta = self.SNAPSHOT_UPDATED_AT_COL

        if not ex.exists_relation(rel_name):
            self._snapshot_first_run(
                node=node,
                rel_name=rel_name,
                body=body,
                strategy=strategy,
                updated_at=updated_at,
                check_cols=check_cols,
                F=F,
                vf=vf,
                vt=vt,
                is_cur=is_cur,
                hash_col=hash_col,
                upd_meta=upd_meta,
            )
            return

        self._snapshot_incremental_run(
            node=node,
            body=body,
            rel_name=rel_name,
            physical=physical,
            strategy=strategy,
            unique_key=unique_key,
            updated_at=updated_at,
            check_cols=check_cols,
            F=F,
            vf=vf,
            vt=vt,
            is_cur=is_cur,
            hash_col=hash_col,
            upd_meta=upd_meta,
        )

    def snapshot_prune(
        self,
        relation: str,
        unique_key: list[str],
        keep_last: int,
        *,
        dry_run: bool = False,
    ) -> None:
        """
        Delete older snapshot versions while keeping the most recent `keep_last`
        rows per business key (including the current row), implemented as a
        DataFrame overwrite (no in-place DELETE).
        """
        if keep_last <= 0:
            return

        Window = get_spark_window()
        F = get_spark_functions()
        ex = self.executor

        if not unique_key:
            return

        vf = self.SNAPSHOT_VALID_FROM_COL

        try:
            physical = ex._physical_identifier(relation)
            df = ex.spark.table(physical)
        except Exception:
            return

        w = Window.partitionBy(*[F.col(k) for k in unique_key]).orderBy(F.col(vf).desc())
        ranked = df.withColumn("__ff_rn", F.row_number().over(w))

        if dry_run:
            cnt = ranked.filter(F.col("__ff_rn") > int(keep_last)).count()

            echo(
                f"[DRY-RUN] snapshot_prune({relation}): would delete {cnt} row(s) "
                f"(keep_last={keep_last})"
            )
            return

        pruned = ranked.filter(F.col("__ff_rn") <= int(keep_last)).drop("__ff_rn")

        # Materialize before overwrite to avoid Spark's self-read/overwrite issues.
        materialized: list[Any] = []

        def _materialize(df_any: Any) -> Any:
            try:
                cp = df_any.localCheckpoint(eager=True)
                materialized.append(cp)
                return cp
            except Exception:
                cached = df_any.cache()
                cached.count()
                materialized.append(cached)
                return cached

        try:
            out = _materialize(pruned)
            ex._save_df_as_table(relation, out)
        finally:
            for handle in materialized:
                with suppress(Exception):
                    handle.unpersist()

    # ---- Helpers ---------------------------------------------------------
    def _validate_snapshot_node(self, node: Node) -> dict[str, Any]:
        ex = self.executor
        if node.kind != "sql":
            raise TypeError(
                f"Snapshot materialization is only supported for SQL models, "
                f"got kind={node.kind!r} for {node.name}."
            )

        meta = getattr(node, "meta", {}) or {}
        if not ex._meta_is_snapshot(meta):
            raise ValueError(f"Node {node.name} is not configured with materialized='snapshot'.")
        return meta

    def _snapshot_sql_body(
        self,
        node: Node,
        env: Any,
    ) -> tuple[str, str, str]:
        ex = self.executor
        sql_rendered = ex.render_sql(
            node,
            env,
            ref_resolver=lambda name: ex._resolve_ref(name, env),
            source_resolver=ex._resolve_source,
        )
        sql_clean = ex._strip_leading_config(sql_rendered).strip()
        body = ex._selectable_body(sql_clean).rstrip(" ;\n\t")

        rel_name = relation_for(node.name)
        physical = ex._physical_identifier(rel_name)
        return body, rel_name, physical

    def _snapshot_first_run(
        self,
        *,
        node: Node,
        rel_name: str,
        body: str,
        strategy: str,
        updated_at: str | None,
        check_cols: list[str],
        F: Any,
        vf: str,
        vt: str,
        is_cur: str,
        hash_col: str,
        upd_meta: str,
    ) -> None:
        ex = self.executor
        src_df = ex._execute_sql(body)

        echo_debug(f"[snapshot] first run for {rel_name} (strategy={strategy})")

        if strategy == "timestamp":
            assert updated_at is not None, (
                "timestamp snapshots require a non-null updated_at column"
            )
            df_snap = (
                src_df.withColumn(upd_meta, F.col(updated_at))
                .withColumn(vf, F.col(updated_at))
                .withColumn(vt, F.lit(None).cast("timestamp"))
                .withColumn(is_cur, F.lit(True))
                .withColumn(hash_col, F.lit(None).cast("string"))
            )
        else:
            cols_expr = [F.coalesce(F.col(c).cast("string"), F.lit("")) for c in check_cols]
            concat_expr = F.concat_ws("||", *cols_expr)
            hash_expr = F.md5(concat_expr).cast("string")
            upd_expr = F.col(updated_at) if updated_at else F.current_timestamp()

            df_snap = (
                src_df.withColumn(upd_meta, upd_expr)
                .withColumn(vf, F.current_timestamp())
                .withColumn(vt, F.lit(None).cast("timestamp"))
                .withColumn(is_cur, F.lit(True))
                .withColumn(hash_col, hash_expr)
            )

        storage_meta = ex._storage_meta(node, rel_name)
        ex._save_df_as_table(rel_name, df_snap, storage=storage_meta)

    def _snapshot_incremental_run(
        self,
        *,
        node: Node,
        body: str,
        rel_name: str,
        physical: str,
        strategy: str,
        unique_key: list[str],
        updated_at: str | None,
        check_cols: list[str],
        F: Any,
        vf: str,
        vt: str,
        is_cur: str,
        hash_col: str,
        upd_meta: str,
    ) -> None:
        ex = self.executor
        echo_debug(f"[snapshot] incremental run for {rel_name} (strategy={strategy})")

        existing = ex.spark.table(physical)
        src_df = ex._execute_sql(body)

        missing_keys_src = [k for k in unique_key if k not in src_df.columns]
        missing_keys_snap = [k for k in unique_key if k not in existing.columns]
        if missing_keys_src or missing_keys_snap:
            raise ValueError(
                f"{node.path}: snapshot unique_key columns must exist on both source and "
                f"snapshot table. Missing on source={missing_keys_src}, "
                f"on snapshot={missing_keys_snap}."
            )

        if strategy == "check":
            cols_expr = [F.coalesce(F.col(c).cast("string"), F.lit("")) for c in check_cols]
            concat_expr = F.concat_ws("||", *cols_expr)
            src_df = src_df.withColumn("__ff_new_hash", F.md5(concat_expr).cast("string"))

        current_df = existing.filter(F.col(is_cur) == True)  # noqa: E712

        s_alias = src_df.alias("s")
        t_alias = current_df.alias("t")
        joined = s_alias.join(t_alias, on=unique_key, how="left")

        if strategy == "timestamp":
            assert updated_at is not None, (
                "timestamp snapshots require a non-null updated_at column"
            )
            s_upd = F.col(f"s.{updated_at}")
            t_upd = F.col(f"t.{upd_meta}")
            cond_new = t_upd.isNull()
            cond_changed = t_upd.isNotNull() & (s_upd > t_upd)
            changed_or_new = cond_new | cond_changed
        else:
            s_hash = F.col("s.__ff_new_hash")
            t_hash = F.col(f"t.{hash_col}")
            cond_new = t_hash.isNull()
            cond_changed = t_hash.isNotNull() & (s_hash != F.coalesce(t_hash, F.lit("")))
            changed_or_new = cond_new | cond_changed

        changed_keys = (
            joined.filter(changed_or_new)
            .select(*[F.col(f"s.{k}").alias(k) for k in unique_key])
            .dropDuplicates()
        )

        prev_noncurrent = existing.filter(F.col(is_cur) == False)  # noqa: E712
        preserved_current = current_df.join(changed_keys, on=unique_key, how="left_anti")

        closed_prev = (
            current_df.join(changed_keys, on=unique_key, how="inner")
            .withColumn(vt, F.current_timestamp())
            .withColumn(is_cur, F.lit(False))
        )

        new_src = src_df.join(changed_keys, on=unique_key, how="inner")
        if strategy == "timestamp":
            assert updated_at is not None, (
                "timestamp snapshots require a non-null updated_at column"
            )
            new_versions = (
                new_src.withColumn(upd_meta, F.col(updated_at))
                .withColumn(vf, F.col(updated_at))
                .withColumn(vt, F.lit(None).cast("timestamp"))
                .withColumn(is_cur, F.lit(True))
                .withColumn(hash_col, F.lit(None).cast("string"))
            )
        else:
            upd_expr = F.col(updated_at) if updated_at else F.current_timestamp()
            new_versions = (
                new_src.withColumn(upd_meta, upd_expr)
                .withColumn(vf, F.current_timestamp())
                .withColumn(vt, F.lit(None).cast("timestamp"))
                .withColumn(is_cur, F.lit(True))
                .withColumn(hash_col, F.col("__ff_new_hash"))
            )

        parts = [prev_noncurrent, preserved_current, closed_prev, new_versions]
        snapshot_df = reduce(lambda a, b: a.unionByName(b, allowMissingColumns=True), parts)
        if "__ff_new_hash" in snapshot_df.columns:
            snapshot_df = snapshot_df.drop("__ff_new_hash")

        # Break lineage so Spark doesn't see this as "read from and overwrite the same table"
        try:
            snapshot_df = snapshot_df.localCheckpoint(eager=True)
        except Exception:
            snapshot_df = snapshot_df.cache()
            snapshot_df.count()

        storage_meta = ex._storage_meta(node, rel_name)
        ex._save_df_as_table(rel_name, snapshot_df, storage=storage_meta)
