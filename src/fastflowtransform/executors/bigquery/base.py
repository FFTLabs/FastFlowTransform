# fastflowtransform/executors/bigquery/base.py
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from fastflowtransform.core import Node, relation_for
from fastflowtransform.executors._budget_runner import run_sql_with_budget
from fastflowtransform.executors._snapshot_sql_mixin import SnapshotSqlMixin
from fastflowtransform.executors._sql_identifier import SqlIdentifierMixin
from fastflowtransform.executors._test_utils import make_fetchable
from fastflowtransform.executors.base import BaseExecutor
from fastflowtransform.executors.budget import BudgetGuard
from fastflowtransform.executors.query_stats import _TrackedQueryJob
from fastflowtransform.meta import ensure_meta_table, upsert_meta
from fastflowtransform.typing import BadRequest, Client, NotFound, bigquery

TFrame = TypeVar("TFrame")


class BigQueryBaseExecutor(SqlIdentifierMixin, SnapshotSqlMixin, BaseExecutor[TFrame]):
    """
    Shared BigQuery executor logic (SQL, incremental, meta, DQ helpers).

    Subclasses are responsible for:
      - frame type (pandas / BigFrames / ...)
      - _read_relation()
      - _materialize_relation()
      - _is_frame()
      - _frame_name()
    """

    # Subclasses override ENGINE_NAME ("bigquery", "bigquery_batch", ...)
    ENGINE_NAME = "bigquery_base"
    _BUDGET_GUARD = BudgetGuard(
        env_var="FF_BQ_MAX_BYTES",
        estimator_attr="_estimate_query_bytes",
        engine_label="BigQuery",
        what="query",
    )

    def __init__(
        self,
        project: str,
        dataset: str,
        location: str | None = None,
        client: Client | None = None,
        allow_create_dataset: bool = False,
    ):
        self.project = project
        self.dataset = dataset
        self.location = location
        self.allow_create_dataset = allow_create_dataset
        self.client: Client = client or bigquery.Client(
            project=self.project,
            location=self.location,
        )

    # ---- Identifier helpers ----
    def _bq_quote(self, value: str) -> str:
        return value.replace("`", "\\`")

    def _quote_identifier(self, ident: str) -> str:
        return self._bq_quote(ident)

    def _default_schema(self) -> str | None:
        return self.dataset

    def _default_catalog(self) -> str | None:
        return self.project

    def _should_include_catalog(
        self, catalog: str | None, schema: str | None, *, explicit: bool
    ) -> bool:
        # BigQuery always expects a project + dataset.
        return True

    def _qualify_identifier(
        self,
        ident: str,
        *,
        schema: str | None = None,
        catalog: str | None = None,
        quote: bool = True,
    ) -> str:
        proj = self._clean_part(catalog) or self._default_catalog()
        dset = self._clean_part(schema) or self._default_schema()
        normalized = self._normalize_identifier(ident)
        parts = [proj, dset, normalized]
        if not quote:
            return ".".join(p for p in parts if p)
        return f"`{'.'.join(self._bq_quote(p) for p in parts if p)}`"

    def _qualified_identifier(
        self, relation: str, project: str | None = None, dataset: str | None = None
    ) -> str:
        return self._qualify_identifier(relation, schema=dataset, catalog=project)

    def _qualified_api_identifier(
        self, relation: str, project: str | None = None, dataset: str | None = None
    ) -> str:
        """
        Build an API-safe identifier (project.dataset.table) without backticks.
        """
        return self._qualify_identifier(
            relation,
            schema=dataset,
            catalog=project,
            quote=False,
        )

    def _ensure_dataset(self) -> None:
        ds_id = f"{self.project}.{self.dataset}"
        try:
            self.client.get_dataset(ds_id)
            return
        except NotFound:
            if not getattr(self, "allow_create_dataset", False):
                raise

        ds_obj = bigquery.Dataset(ds_id)
        if getattr(self, "location", None):
            ds_obj.location = self.location
        self.client.create_dataset(ds_obj, exists_ok=True)

    def execute_test_sql(self, stmt: Any) -> Any:
        """
        Execute lightweight SQL for DQ tests using the BigQuery client.
        """

        def _infer_param_type(value: Any) -> str:
            if isinstance(value, bool):
                return "BOOL"
            if isinstance(value, int) and not isinstance(value, bool):
                return "INT64"
            if isinstance(value, float):
                return "FLOAT64"
            return "STRING"

        def _run_job(sql: str, params: dict[str, Any] | None = None) -> Any:
            job_config = bigquery.QueryJobConfig()
            if self.dataset:
                job_config.default_dataset = bigquery.DatasetReference(self.project, self.dataset)
            if params:
                job_config.query_parameters = [
                    bigquery.ScalarQueryParameter(k, _infer_param_type(v), v)
                    for k, v in params.items()
                ]
            return self.client.query(sql, job_config=job_config, location=self.location)

        def _run_one(s: Any) -> Any:
            statement_len = 2
            if (
                isinstance(s, tuple)
                and len(s) == statement_len
                and isinstance(s[0], str)
                and isinstance(s[1], dict)
            ):
                return _run_job(s[0], s[1]).result()
            if isinstance(s, str):
                # Use guarded execution path for simple statements
                return self._execute_sql(s).result()
            if isinstance(s, Iterable) and not isinstance(s, (bytes, bytearray, str)):
                res = None
                for item in s:
                    res = _run_one(item)
                return res
            return _run_job(str(s)).result()

        return make_fetchable(_run_one(stmt))

    def compute_freshness_delay_minutes(self, table: str, ts_col: str) -> tuple[float | None, str]:
        sql = (
            f"select cast(TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), max({ts_col}), MINUTE) as float64) "
            f"as delay_min from {table}"
        )
        res = self.execute_test_sql(sql)
        delay = getattr(res, "fetchone", lambda: None)()
        val = delay[0] if delay else None
        return (float(val) if val is not None else None, sql)

    def _execute_sql(self, sql: str) -> _TrackedQueryJob:
        """
        Central BigQuery query runner.

        - All 'real' SQL statements in this executor should go through here.
        - Returns the QueryJob so callers can call .result().
        """

        def _exec() -> _TrackedQueryJob:
            job_config = bigquery.QueryJobConfig()
            if self.dataset:
                # Let unqualified tables resolve to project.dataset.table
                job_config.default_dataset = bigquery.DatasetReference(self.project, self.dataset)

            job = self.client.query(
                sql,
                job_config=job_config,
                location=self.location,
            )
            return _TrackedQueryJob(job, on_complete=self._record_query_job_stats)

        return run_sql_with_budget(
            self,
            sql,
            guard=self._BUDGET_GUARD,
            exec_fn=_exec,
            estimate_fn=self._estimate_query_bytes,
            record_stats=False,
        )

    # --- Cost estimation for the shared BudgetGuard -----------------

    def _estimate_query_bytes(self, sql: str) -> int | None:
        """
        Estimate bytes for a BigQuery SQL statement using a dry-run.

        Returns the estimated bytes, or None if estimation is not possible.
        """
        cfg = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
        )
        if self.dataset:
            # Let unqualified tables resolve to project.dataset.table
            cfg.default_dataset = bigquery.DatasetReference(self.project, self.dataset)

        job = self.client.query(
            sql,
            job_config=cfg,
            location=self.location,
        )
        # Dry-run is free; we just need the job metadata
        job.result()
        return int(getattr(job, "total_bytes_processed", 0) or 0)

    # ---- DQ test table formatting (fft test) ----
    def _format_test_table(self, table: str | None) -> str | None:
        """
        Ensure tests use fully-qualified BigQuery identifiers in fft test.
        """
        table = super()._format_test_table(table)
        if not isinstance(table, str):
            return table
        stripped = table.strip()
        if not stripped or stripped.startswith("`"):
            return stripped
        if "." in stripped:
            return stripped
        return self._qualified_identifier(stripped)

    # ---- SQL hooks ----
    def _this_identifier(self, node: Node) -> str:
        """
        Ensure {{ this }} renders as a fully-qualified identifier so BigQuery
        incremental SQL (e.g., subqueries against {{ this }}) includes project
        and dataset.
        """
        return self._qualify_identifier(relation_for(node.name))

    def _apply_sql_materialization(
        self,
        node: Node,
        target_sql: str,
        select_body: str,
        materialization: str,
    ) -> None:
        self._ensure_dataset()
        try:
            super()._apply_sql_materialization(node, target_sql, select_body, materialization)
        except BadRequest as e:
            raise RuntimeError(
                f"BigQuery SQL failed for {target_sql}:\n{select_body}\n\n{e}"
            ) from e

    def _create_or_replace_view(self, target_sql: str, select_body: str, node: Node) -> None:
        self._execute_sql(f"CREATE OR REPLACE VIEW {target_sql} AS {select_body}").result()

    def _create_or_replace_table(self, target_sql: str, select_body: str, node: Node) -> None:
        self._execute_sql(f"CREATE OR REPLACE TABLE {target_sql} AS {select_body}").result()

    def _create_or_replace_view_from_table(
        self,
        view_name: str,
        backing_table: str,
        node: Node,
    ) -> None:
        view_id = self._qualified_identifier(view_name)
        back_id = self._qualified_identifier(backing_table)
        self._ensure_dataset()
        self._execute_sql(f"CREATE OR REPLACE VIEW {view_id} AS SELECT * FROM {back_id}").result()

    # ---- Snapshot mixin hooks ----
    def _snapshot_prepare_target(self) -> None:
        self._ensure_dataset()

    def _snapshot_target_identifier(self, rel_name: str) -> str:
        return self._qualified_identifier(rel_name)

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

    # ---- Meta hook ----
    def on_node_built(self, node: Node, relation: str, fingerprint: str) -> None:
        """
        Write/update dataset._ff_meta after a successful build.
        Both pandas + BigFrames executors use the logical engine key 'bigquery'.
        """
        ensure_meta_table(self)
        upsert_meta(self, node.name, relation, fingerprint, "bigquery")

    # ── Incremental API (shared across BigQuery executors) ───────────────
    def exists_relation(self, relation: str) -> bool:
        """
        Check presence in INFORMATION_SCHEMA for tables/views.
        """
        proj = self.project
        dset = self.dataset
        rel = relation
        q = f"""
        SELECT 1
        FROM `{proj}.{dset}.INFORMATION_SCHEMA.TABLES`
        WHERE LOWER(table_name)=LOWER(@rel)
        UNION ALL
        SELECT 1
        FROM `{proj}.{dset}.INFORMATION_SCHEMA.VIEWS`
        WHERE LOWER(table_name)=LOWER(@rel)
        LIMIT 1
        """
        job = self.client.query(
            q,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("rel", "STRING", rel)]
            ),
            location=self.location,
        )
        return bool(list(job.result()))

    def create_table_as(self, relation: str, select_sql: str) -> None:
        """
        CREATE TABLE AS with cleaned SELECT body (no trailing semicolons).
        """
        self._ensure_dataset()
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        target = self._qualified_identifier(
            relation,
            project=self.project,
            dataset=self.dataset,
        )
        self._execute_sql(f"CREATE TABLE {target} AS {body}").result()

    def incremental_insert(self, relation: str, select_sql: str) -> None:
        """
        INSERT INTO with cleaned SELECT body.
        """
        self._ensure_dataset()
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        target = self._qualified_identifier(
            relation,
            project=self.project,
            dataset=self.dataset,
        )
        self._execute_sql(f"INSERT INTO {target} {body}").result()

    def incremental_merge(self, relation: str, select_sql: str, unique_key: list[str]) -> None:
        """
        Portable fallback without native MERGE:
          - DELETE collisions via WHERE EXISTS against the cleaned SELECT body
          - INSERT new rows from the same body
        """
        self._ensure_dataset()
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        target = self._qualified_identifier(
            relation,
            project=self.project,
            dataset=self.dataset,
        )
        pred = " AND ".join([f"t.{k}=s.{k}" for k in unique_key]) or "FALSE"

        delete_sql = f"""
        DELETE FROM {target} t
        WHERE EXISTS (SELECT 1 FROM ({body}) s WHERE {pred})
        """
        self._execute_sql(delete_sql).result()

        insert_sql = f"INSERT INTO {target} SELECT * FROM ({body})"
        self._execute_sql(insert_sql).result()

    def alter_table_sync_schema(
        self,
        relation: str,
        select_sql: str,
        *,
        mode: str = "append_new_columns",
    ) -> None:
        """
        Best-effort additive schema sync:
          - infer select schema via LIMIT 0 query
          - add missing columns as NULLABLE using inferred BigQuery types
        """
        if mode not in {"append_new_columns", "sync_all_columns"}:
            return
        self._ensure_dataset()

        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")

        # Infer schema using a no-row query (lets BigQuery type the expressions)
        probe = self.client.query(
            f"SELECT * FROM ({body}) WHERE 1=0",
            job_config=bigquery.QueryJobConfig(dry_run=False, use_query_cache=False),
            location=self.location,
        )
        probe.result()
        out_fields = {f.name: f for f in (probe.schema or [])}

        # Existing table schema
        table_ref = f"{self.project}.{self.dataset}.{relation}"
        try:
            tbl = self.client.get_table(table_ref)
        except NotFound:
            return
        existing_cols = {f.name for f in (tbl.schema or [])}

        to_add = [name for name in out_fields if name not in existing_cols]
        if not to_add:
            return

        target = self._qualified_identifier(
            relation,
            project=self.project,
            dataset=self.dataset,
        )
        for col in to_add:
            f = out_fields[col]
            typ = str(f.field_type) if hasattr(f, "field_type") else "STRING"
            self._execute_sql(f"ALTER TABLE {target} ADD COLUMN {col} {typ}").result()

    # ── Snapshots API (shared for pandas + BigFrames) ─────────────────────

    def execute_hook_sql(self, sql: str) -> None:
        """
        Execute one SQL statement for pre/post/on_run hooks.
        """
        self._execute_sql(sql).result()

    def _introspect_columns_metadata(
        self,
        table: str,
        *,
        column: str | None = None,
    ) -> list[tuple[str, str]]:
        """
        Internal helper: return [(column_name_lower, data_type_upper), ...]
        for a BigQuery table using INFORMATION_SCHEMA.COLUMNS.

        Accepts:
          - `table` as "table" or "dataset.table" or "project.dataset.table"
          - optional `column` to restrict to a single column
        """
        project = self.project
        dataset = self.dataset
        table_name = table

        parts = table.split(".")
        if len(parts) == 3:
            project, dataset, table_name = parts
        elif len(parts) == 2:
            dataset, table_name = parts

        table_name = table_name.strip("`")
        dataset = dataset.strip("`") if dataset else dataset
        project = project.strip("`") if project else project

        if not table_name:
            return []

        where = ["lower(table_name) = lower(@t)"]
        params = [bigquery.ScalarQueryParameter("t", "STRING", table_name)]

        if column is not None:
            where.append("lower(column_name) = lower(@c)")
            params.append(bigquery.ScalarQueryParameter("c", "STRING", column))

        sql = f"""
        select lower(column_name) as column_name, upper(data_type) as data_type
        from `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
        where {" and ".join(where)}
        order by ordinal_position
        """

        job = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                default_dataset=bigquery.DatasetReference(project, dataset),
            ),
            location=self.location,
        )
        rows = list(job.result())
        return [(str(r[0]), str(r[1])) for r in rows]

    def introspect_column_physical_type(self, table: str, column: str) -> str | None:
        rows = self._introspect_columns_metadata(table, column=column)
        return rows[0][1] if rows else None

    def introspect_table_physical_schema(self, table: str) -> dict[str, str]:
        rows = self._introspect_columns_metadata(table, column=None)
        # keys are lowercased to match the DuckRuntimeContracts verify logic
        return {name: dtype for (name, dtype) in rows}

    def load_seed(self, table: str, df: Any, schema: str | None = None) -> tuple[bool, str, bool]:
        dataset_id = schema or self.dataset

        table_id = self._qualified_api_identifier(
            table,
            project=self.project,
            dataset=dataset_id,
        )
        full_name = table_id
        self._ensure_dataset()

        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE
        )

        load_job = self.client.load_table_from_dataframe(df, table_id, job_config=job_config)
        load_job.result()

        return True, full_name, False
