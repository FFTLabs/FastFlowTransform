# fastflowtransform/executors/duckdb.py
from __future__ import annotations

import uuid
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import duckdb
import pandas as pd
from duckdb import CatalogException
from jinja2 import Environment

from fastflowtransform.contracts.runtime.duckdb import DuckRuntimeContracts
from fastflowtransform.core import Node
from fastflowtransform.executors._sql_identifier import SqlIdentifierMixin
from fastflowtransform.executors._test_utils import make_fetchable
from fastflowtransform.executors.base import BaseExecutor, ColumnInfo, _scalar
from fastflowtransform.executors.budget.runtime.duckdb import DuckBudgetRuntime
from fastflowtransform.executors.common import _q_ident
from fastflowtransform.executors.query_stats.runtime.duckdb import DuckQueryStatsRuntime
from fastflowtransform.meta import ensure_meta_table, upsert_meta
from fastflowtransform.snapshots.runtime.duckdb import DuckSnapshotRuntime


class DuckExecutor(SqlIdentifierMixin, BaseExecutor[pd.DataFrame]):
    ENGINE_NAME: str = "duckdb"
    runtime_contracts: DuckRuntimeContracts
    runtime_query_stats: DuckQueryStatsRuntime
    runtime_budget: DuckBudgetRuntime
    snapshot_runtime: DuckSnapshotRuntime

    def __init__(
        self, db_path: str = ":memory:", schema: str | None = None, catalog: str | None = None
    ):
        if db_path and db_path != ":memory:" and "://" not in db_path:
            with suppress(Exception):
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.con = duckdb.connect(db_path)
        self.schema = schema.strip() if isinstance(schema, str) and schema.strip() else None
        catalog_override = catalog.strip() if isinstance(catalog, str) and catalog.strip() else None
        self.catalog = self._detect_catalog()
        if catalog_override:
            if self._apply_catalog_override(catalog_override):
                self.catalog = catalog_override
            else:
                self.catalog = self._detect_catalog()
        self.runtime_query_stats = DuckQueryStatsRuntime(self)
        self.runtime_budget = DuckBudgetRuntime(self)
        self.runtime_contracts = DuckRuntimeContracts(self)
        self.snapshot_runtime = DuckSnapshotRuntime(self)

        if self.schema:
            safe_schema = _q_ident(self.schema)
            self._execute_basic(f"create schema if not exists {safe_schema}")
            self._execute_basic(f"set schema '{self.schema}'")

    def _execute_basic(self, sql: str, params: Any | None = None) -> duckdb.DuckDBPyConnection:
        """
        Minimal helper to execute a statement and return the DuckDB cursor.
        Centralises raw connection use for test + runtime helpers.
        """
        return self.con.execute(sql, params) if params is not None else self.con.execute(sql)

    def _execute_fetchall(self, sql: str, params: Any | None = None) -> list[Any]:
        """
        Helper for runtimes that need full result sets without exposing cursors.
        """
        res = self._execute_basic(sql, params)
        fetchall = getattr(res, "fetchall", None)
        return list(cast(Iterable[Any], fetchall())) if callable(fetchall) else []

    def execute_test_sql(self, stmt: Any) -> Any:
        """
        Execute lightweight SQL for DQ tests using the underlying DuckDB connection.
        """

        def _run_one(s: Any) -> Any:
            statement_len = 2
            if (
                isinstance(s, tuple)
                and len(s) == statement_len
                and isinstance(s[0], str)
                and isinstance(s[1], dict)
            ):
                return self._execute_basic(s[0], s[1])
            if isinstance(s, str):
                return self._execute_basic(s)
            if isinstance(s, Iterable) and not isinstance(s, (bytes, bytearray, str)):
                res = None
                for item in s:
                    res = _run_one(item)
                return res
            return self._execute_basic(str(s))

        return make_fetchable(_run_one(stmt))

    def compute_freshness_delay_minutes(self, table: str, ts_col: str) -> tuple[float | None, str]:
        now_expr = "cast(now() as timestamp)"
        sql = (
            f"select date_part('epoch', {now_expr} - max({ts_col})) "
            f"/ 60.0 as delay_min from {table}"
        )
        delay = _scalar(self, sql)
        return (float(delay) if delay is not None else None, sql)

    def _execute_sql(self, sql: str, *args: Any, **kwargs: Any) -> duckdb.DuckDBPyConnection:
        """
        Central DuckDB SQL runner.

        All model-driven SQL in this executor should go through here.
        The cost guard may call _estimate_query_bytes(sql) before executing.
        This wrapper also records simple per-query stats for run_results.json.
        """

        def _exec() -> duckdb.DuckDBPyConnection:
            return self.con.execute(sql, *args, **kwargs)

        def _rows(result: Any) -> int | None:
            return self.runtime_query_stats.rowcount_from_result(result)

        return self.runtime_budget.run_sql(
            sql,
            exec_fn=_exec,
            stats_runtime=self.runtime_query_stats,
            rowcount_extractor=_rows,
        )

    def _detect_catalog(self) -> str | None:
        rows = self._execute_basic("PRAGMA database_list").fetchall()
        if rows:
            return str(rows[0][1])
        return None

    def _apply_catalog_override(self, name: str) -> bool:
        alias = name.strip()
        if not alias:
            return False
        try:
            if self.db_path != ":memory:":
                resolved = str(Path(self.db_path).resolve())
                with suppress(Exception):
                    self._execute_basic(f"detach database {_q_ident(alias)}")
                self._execute_basic(
                    f"attach database '{resolved}' as {_q_ident(alias)} (READ_ONLY FALSE)"
                )
            self._execute_basic(f"set catalog '{alias}'")
            return True
        except Exception:
            return False

    def clone(self) -> DuckExecutor:
        """
        Generates a new Executor instance with its own connection for Thread-Worker.
        Copies runtime-contract configuration from the parent.
        """
        cloned = DuckExecutor(self.db_path, schema=self.schema, catalog=self.catalog)

        # Propagate contracts + project contracts to the clone
        contracts = getattr(self, "_ff_contracts", None)
        project_contracts = getattr(self, "_ff_project_contracts", None)
        if contracts is not None or project_contracts is not None:
            # configure_contracts lives on BaseExecutor
            cloned.configure_contracts(contracts or {}, project_contracts)

        return cloned

    def _exec_many(self, sql: str) -> None:
        """
        Execute multiple SQL statements separated by ';' on the same connection.
        DuckDB normally accepts one statement per execute(), so we split here.
        """
        for stmt in (part.strip() for part in sql.split(";")):
            if not stmt:
                continue
            self._execute_sql(stmt)

    # ---- Frame hooks ----
    def _quote_identifier(self, ident: str) -> str:
        return _q_ident(ident)

    def _should_include_catalog(
        self, catalog: str | None, schema: str | None, *, explicit: bool
    ) -> bool:
        """
        DuckDB includes catalog only when explicitly provided or when it matches
        the schema (mirrors previous behaviour).
        """
        if explicit:
            return bool(catalog)
        return bool(catalog and schema and catalog.lower() == schema.lower())

    def _default_catalog_for_source(self, schema: str | None) -> str | None:
        """
        For sources, fall back to DuckDB's detected catalog when:
        - schema is set and matches the catalog, or
        - neither schema nor catalog was provided (keep old fallback)
        """
        cat = self._default_catalog()
        if not cat:
            return None
        if schema is None or cat.lower() == schema.lower():
            return cat
        return None

    def _qualified(self, relation: str, *, quoted: bool = True) -> str:
        """
        Return (catalog.)schema.relation if schema is set; otherwise just relation.
        When quoted=False, emit bare identifiers for APIs like con.table().
        """
        return self._format_identifier(relation, purpose="physical", quote=quoted)

    def _read_relation(self, relation: str, node: Node, deps: Iterable[str]) -> pd.DataFrame:
        try:
            target = self._qualified(relation, quoted=False)
            return self.con.table(target).df()
        except CatalogException as e:
            existing = [
                r[0]
                for r in self._execute_basic(
                    "select table_name from information_schema.tables "
                    "where table_schema in ('main','temp')"
                ).fetchall()
            ]
            raise RuntimeError(
                f"Dependency table not found: '{relation}'\n"
                f"Deps: {list(deps)}\nExisting tables: {existing}\n"
                "Note: Use same File-DB/Connection for Seeding & Run."
            ) from e

    def _materialize_relation(self, relation: str, df: pd.DataFrame, node: Node) -> None:
        tmp = "_ff_py_out"
        try:
            self.con.register(tmp, df)
            target = self._qualified(relation)
            self._execute_sql(f'create or replace table {target} as select * from "{tmp}"')
        finally:
            try:
                self.con.unregister(tmp)
            except Exception:
                # housekeeping only; stats here are not important but harmless if recorded
                self._execute_basic(f'drop view if exists "{tmp}"')

    def _create_or_replace_view_from_table(
        self, view_name: str, backing_table: str, node: Node
    ) -> None:
        view_target = self._qualified(view_name)
        backing = self._qualified(backing_table)
        self._execute_sql(f"create or replace view {view_target} as select * from {backing}")

    def _frame_name(self) -> str:
        return "pandas"

    # ---- SQL hooks ----
    def _create_or_replace_view(self, target_sql: str, select_body: str, node: Node) -> None:
        self._execute_sql(f"create or replace view {target_sql} as {select_body}")

    def _create_or_replace_table(self, target_sql: str, select_body: str, node: Node) -> None:
        self._execute_sql(f"create or replace table {target_sql} as {select_body}")

    # ---- Meta hook ----
    def on_node_built(self, node: Node, relation: str, fingerprint: str) -> None:
        """
        After successful materialization, ensure the meta table exists and upsert the row.
        """
        ensure_meta_table(self)
        upsert_meta(self, node.name, relation, fingerprint, "duckdb")

    # ── Incremental API ────────────────────────────────────────────────────
    def exists_relation(self, relation: str) -> bool:
        where_tables: list[str] = ["lower(table_name) = lower(?)"]
        params: list[str] = [relation]
        if self.catalog:
            where_tables.append("lower(table_catalog) = lower(?)")
            params.append(self.catalog)
        if self.schema:
            where_tables.append("lower(table_schema) = lower(?)")
            params.append(self.schema)
        else:
            where_tables.append("table_schema in ('main','temp')")
        where = " AND ".join(where_tables)
        sql_tables = f"select 1 from information_schema.tables where {where} limit 1"
        if self._execute_basic(sql_tables, params).fetchone():
            return True
        sql_views = f"select 1 from information_schema.views where {where} limit 1"
        return bool(self._execute_basic(sql_views, params).fetchone())

    def create_table_as(self, relation: str, select_sql: str) -> None:
        # Use only the SELECT body and strip trailing semicolons for safety.
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        self._execute_sql(f"create table {self._qualified(relation)} as {body}")

    def incremental_insert(self, relation: str, select_sql: str) -> None:
        # Ensure the inner SELECT is clean (no trailing semicolon; SELECT body only).
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        self._execute_sql(f"insert into {self._qualified(relation)} {body}")

    def incremental_merge(self, relation: str, select_sql: str, unique_key: list[str]) -> None:
        """
        Fallback strategy for DuckDB:
        - DELETE collisions via DELETE ... USING (<select>) s
        - INSERT all rows via INSERT ... SELECT * FROM (<select>)
        """
        # 1) clean inner SELECT
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")

        # 2) predicate for DELETE
        keys_pred = " AND ".join([f"t.{k}=s.{k}" for k in unique_key]) or "FALSE"

        # 3) first: delete collisions
        delete_sql = f"delete from {self._qualified(relation)} t using ({body}) s where {keys_pred}"
        self._execute_sql(delete_sql)

        # 4) then: insert fresh rows
        insert_sql = f"insert into {self._qualified(relation)} select * from ({body}) src"
        self._execute_sql(insert_sql)

    def alter_table_sync_schema(
        self, relation: str, select_sql: str, *, mode: str = "append_new_columns"
    ) -> None:
        """
        Best-effort: add new columns with inferred type.
        """
        # Probe: empty projection from the SELECT (cleaned to avoid parser issues).
        body = self._first_select_body(select_sql).strip().rstrip(";\n\t ")
        probe = self._execute_basic(f"select * from ({body}) as q limit 0")
        cols = [c[0] for c in probe.description or []]
        existing = {
            r[0]
            for r in self._execute_basic(
                "select column_name from information_schema.columns "
                + "where lower(table_name)=lower(?)"
                + (" and lower(table_schema)=lower(?)" if self.schema else ""),
                ([relation, self.schema] if self.schema else [relation]),
            ).fetchall()
        }
        add = [c for c in cols if c not in existing]
        for c in add:
            col = _q_ident(c)
            target = self._qualified(relation)
            try:
                self._execute_basic(f"alter table {target} add column {col} varchar")
            except Exception:
                self._execute_basic(f"alter table {target} add column {col} varchar")

    def execute_hook_sql(self, sql: str) -> None:
        """
        Execute one or multiple SQL statements for pre/post/on_run hooks.

        Accepts a string that may contain ';'-separated statements.
        """
        self._exec_many(sql)

    # ---- Snapshot runtime delegation ----
    def run_snapshot_sql(self, node: Node, env: Environment) -> None:
        """
        Delegate snapshot materialization to the DuckDB snapshot runtime.
        """
        self.snapshot_runtime.run_snapshot_sql(node, env)

    def snapshot_prune(
        self,
        relation: str,
        unique_key: list[str],
        keep_last: int,
        *,
        dry_run: bool = False,
    ) -> None:
        self.snapshot_runtime.snapshot_prune(
            relation,
            unique_key,
            keep_last,
            dry_run=dry_run,
        )

        # ---- Unit-test helpers -------------------------------------------------

    def utest_load_relation_from_rows(self, relation: str, rows: list[dict]) -> None:
        """
        Load rows into a DuckDB table for unit tests, fully qualified to
        this executor's schema/catalog.
        """
        df = pd.DataFrame(rows)
        tmp = f"_ff_utest_tmp_{uuid.uuid4().hex[:12]}"
        self.con.register(tmp, df)
        try:
            target = self._qualified(relation)
            self._execute_basic(f"create or replace table {target} as select * from {tmp}")
        finally:
            with suppress(Exception):
                self.con.unregister(tmp)
            # Fallback for older DuckDB where unregister might not exist
            with suppress(Exception):
                self._execute_basic(f'drop view if exists "{tmp}"')

    def utest_read_relation(self, relation: str) -> pd.DataFrame:
        """
        Read a relation as a DataFrame for unit-test assertions.
        """
        target = self._qualified(relation, quoted=False)
        return self.con.table(target).df()

    def utest_clean_target(self, relation: str) -> None:
        """
        Drop any table/view with the given name in this schema/catalog.
        Safe because utest uses its own DB/path.
        """
        target = self._qualified(relation)
        # best-effort; ignore failures
        with suppress(Exception):
            self._execute_basic(f"drop view if exists {target}")
        with suppress(Exception):
            self._execute_basic(f"drop table if exists {target}")

    def collect_docs_columns(self) -> dict[str, list[ColumnInfo]]:
        """
        Best-effort column metadata for docs (schema-aware, supports catalog).
        """
        where: list[str] = []
        params: list[str] = []

        if self.catalog:
            where.append("lower(table_catalog) = lower(?)")
            params.append(self.catalog)
        if self.schema:
            where.append("lower(table_schema) = lower(?)")
            params.append(self.schema)
        else:
            where.append("table_schema in ('main','temp')")

        where_sql = " AND ".join(where) if where else "1=1"
        sql = f"""
        select table_name, column_name, data_type, is_nullable
        from information_schema.columns
        where {where_sql}
        order by table_schema, table_name, ordinal_position
        """

        try:
            rows = self._execute_basic(sql, params or None).fetchall()
        except Exception:
            return {}

        out: dict[str, list[ColumnInfo]] = {}
        for table, col, dtype, nullable in rows:
            out.setdefault(table, []).append(
                ColumnInfo(col, str(dtype), str(nullable) in (True, "YES", "Yes"))
            )
        return out

    def _introspect_columns_metadata(
        self,
        table: str,
        column: str | None = None,
    ) -> list[tuple[str, str]]:
        """
        Internal helper: return [(column_name, data_type), ...] for a DuckDB table.

        - Uses _normalize_table_identifier / _normalize_column_identifier
        - Works with or without schema qualification
        - Optionally restricts to a single column
        """
        schema, table_name = self._normalize_table_identifier(table)

        table_lower = table_name.lower()
        params: list[str] = [table_lower]

        where_clauses: list[str] = ["lower(table_name) = lower(?)"]

        if schema:
            where_clauses.append("lower(table_schema) = lower(?)")
            params.append(schema.lower())

        if column is not None:
            column_lower = self._normalize_column_identifier(column).lower()
            where_clauses.append("lower(column_name) = lower(?)")
            params.append(column_lower)

        where_sql = " AND ".join(where_clauses)

        sql = (
            "select column_name, data_type "
            "from information_schema.columns "
            f"where {where_sql} "
            "order by table_schema, ordinal_position"
        )

        rows = self._execute_basic(sql, params).fetchall()

        # Normalize to plain strings
        return [(str(name), str(dtype)) for (name, dtype) in rows]

    def introspect_column_physical_type(self, table: str, column: str) -> str | None:
        """
        DuckDB: read `data_type` from information_schema.columns for a single column.
        """
        rows = self._introspect_columns_metadata(table, column=column)
        # rows: [(column_name, data_type), ...]
        return rows[0][1] if rows else None

    def introspect_table_physical_schema(self, table: str) -> dict[str, str]:
        """
        DuckDB: return {column_name: data_type} for all columns of `table`.
        """
        rows = self._introspect_columns_metadata(table, column=None)
        return {name: dtype for (name, dtype) in rows}

    def load_seed(
        self, table: str, df: pd.DataFrame, schema: str | None = None
    ) -> tuple[bool, str, bool]:
        target_schema = schema or self.schema
        created_schema = False

        # Qualify identifier with optional schema/catalog
        qualified = self._qualify_identifier(table, schema=target_schema, catalog=self.catalog)

        if target_schema and "." not in table:
            safe_schema = _q_ident(target_schema)
            self._execute_sql(f"create schema if not exists {safe_schema}")
            created_schema = True

        tmp = f"_ff_seed_{uuid.uuid4().hex[:8]}"
        self.con.register(tmp, df)
        try:
            self._execute_sql(f'create or replace table {qualified} as select * from "{tmp}"')
        finally:
            with suppress(Exception):
                self.con.unregister(tmp)
            with suppress(Exception):
                self._execute_basic(f'drop view if exists "{tmp}"')

        return True, qualified, created_schema
