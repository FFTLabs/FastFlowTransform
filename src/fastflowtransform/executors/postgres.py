# fastflowtransform/executors/postgres.py
import re
from collections.abc import Iterable
from time import perf_counter
from typing import Any, cast

import pandas as pd
from jinja2 import Environment
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.sql import Executable
from sqlalchemy.sql.elements import ClauseElement

from fastflowtransform.contracts.runtime.postgres import PostgresRuntimeContracts
from fastflowtransform.core import Node
from fastflowtransform.errors import ModelExecutionError, ProfileConfigError
from fastflowtransform.executors._sql_identifier import SqlIdentifierMixin
from fastflowtransform.executors._test_utils import make_fetchable
from fastflowtransform.executors.base import BaseExecutor, ColumnInfo, _scalar
from fastflowtransform.executors.budget.runtime.postgres import PostgresBudgetRuntime
from fastflowtransform.executors.common import _q_ident
from fastflowtransform.executors.query_stats.runtime.postgres import PostgresQueryStatsRuntime
from fastflowtransform.meta import ensure_meta_table, upsert_meta
from fastflowtransform.snapshots.runtime.postgres import PostgresSnapshotRuntime


def _base_type(t: str) -> str:
    # Strip modifiers so DQ compares are stable (varchar(10) -> varchar, numeric(18,0) -> numeric)
    s = re.sub(r"\s+", " ", (t or "").strip().lower())
    s = re.sub(r"\s*\(.*\)\s*$", "", s)
    return s


class PostgresExecutor(SqlIdentifierMixin, BaseExecutor[pd.DataFrame]):
    ENGINE_NAME: str = "postgres"
    runtime_contracts: PostgresRuntimeContracts
    runtime_query_stats: PostgresQueryStatsRuntime
    runtime_budget: PostgresBudgetRuntime
    snapshot_runtime: PostgresSnapshotRuntime

    def __init__(self, dsn: str, schema: str | None = None):
        """
        Initialize Postgres executor.

        dsn     e.g.: postgresql+psycopg://user:pass@localhost:5432/dbname
        schema  default schema for reads/writes (also used for search_path)
        """
        if not dsn:
            raise ProfileConfigError(
                "Postgres DSN not set. Hint: profiles.yml → postgres.dsn or env FF_PG_DSN."
            )
        self.engine: Engine = create_engine(dsn, future=True)
        self.schema = schema

        if self.schema:
            try:
                self._execute_sql_maintenance(
                    f"CREATE SCHEMA IF NOT EXISTS {_q_ident(self.schema)}", set_search_path=False
                )
            except SQLAlchemyError as exc:
                raise ProfileConfigError(
                    f"Failed to ensure schema '{self.schema}' exists: {exc}"
                ) from exc

        # Enable runtime helpers and contracts.
        self.runtime_query_stats = PostgresQueryStatsRuntime(self)
        self.runtime_budget = PostgresBudgetRuntime(self)
        self.runtime_contracts = PostgresRuntimeContracts(self)
        self.snapshot_runtime = PostgresSnapshotRuntime(self)

    def execute_test_sql(self, stmt: Any) -> Any:
        """
        Execute lightweight SQL for DQ tests using a transactional connection.
        """

        def _run_one(s: Any, conn: Connection) -> Any:
            statement_len = 2
            if (
                isinstance(s, tuple)
                and len(s) == statement_len
                and isinstance(s[0], str)
                and isinstance(s[1], dict)
            ):
                return conn.execute(text(s[0]), s[1])
            if isinstance(s, str):
                return conn.execute(text(s))
            if isinstance(s, ClauseElement):
                return conn.execute(cast(Executable, s))
            if isinstance(s, Iterable) and not isinstance(s, (bytes, bytearray, str)):
                res = None
                for item in s:
                    res = _run_one(item, conn)
                return res
            return conn.execute(text(str(s)))

        with self.engine.begin() as conn:
            self._set_search_path(conn)
            return make_fetchable(_run_one(stmt, conn))

    def compute_freshness_delay_minutes(self, table: str, ts_col: str) -> tuple[float | None, str]:
        sql = f"select date_part('epoch', now() - max({ts_col})) / 60.0 as delay_min from {table}"
        delay = _scalar(self, sql)
        return (float(delay) if delay is not None else None, sql)

    def _execute_sql_core(
        self,
        sql: str,
        *args: Any,
        conn: Connection,
        set_search_path: bool = True,
        **kwargs: Any,
    ) -> Any:
        """
        Lowest-level SQL executor:

        - sets search_path
        - executes the statement via given connection
        - NO budget guard
        - NO timing / stats

        Used by both the high-level _execute_sql and maintenance helpers.
        """
        if set_search_path:
            self._set_search_path(conn)
        return conn.execute(text(sql), *args, **kwargs)

    def _execute_sql_maintenance(
        self,
        sql: str,
        *args: Any,
        conn: Connection | None = None,
        set_search_path: bool = True,
        **kwargs: Any,
    ) -> Any:
        """
        Utility/maintenance SQL:

        - sets search_path
        - NO budget guard
        - NO stats

        Intended for:
          - utest cleanup
          - ANALYZE
          - DDL that shouldn't be budget-accounted
        """
        if conn is None:
            with self.engine.begin() as local_conn:
                return self._execute_sql_core(
                    sql, *args, conn=local_conn, set_search_path=set_search_path, **kwargs
                )
        else:
            return self._execute_sql_core(
                sql, *args, conn=conn, set_search_path=set_search_path, **kwargs
            )

    def _execute_sql(
        self,
        sql: str,
        *args: Any,
        conn: Connection | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Central Postgres SQL runner.

        All model-driven SQL in this executor should go through here.

        If `conn` is provided, reuse that connection (important for temp tables /
        snapshots). Otherwise, open a fresh transaction via engine.begin().

        Also records simple per-query stats for run_results.json.
        """

        def _exec() -> Any:
            if conn is None:
                with self.engine.begin() as local_conn:
                    return self._execute_sql_core(sql, *args, conn=local_conn, **kwargs)
            return self._execute_sql_core(sql, *args, conn=conn, **kwargs)

        def _rows(result: Any) -> int | None:
            return self.runtime_query_stats.rowcount_from_result(result)

        return self.runtime_budget.run_sql(
            sql,
            exec_fn=_exec,
            stats_runtime=self.runtime_query_stats,
            rowcount_extractor=_rows,
        )

    def _analyze_relations(
        self,
        relations: Iterable[str],
        conn: Connection | None = None,
    ) -> None:
        """
        Run ANALYZE on the given relations.

        - Never goes through _execute_sql (avoids the budget guard recursion).
        - Uses passed-in conn if given, otherwise opens its own transaction.
        - Best-effort: logs and continues on failure.
        """
        for rel in relations:
            try:
                # If it already looks qualified, leave it; otherwise qualify.
                qrel = self._qualified(rel) if "." not in rel else rel
                self._execute_sql_maintenance(f"ANALYZE {qrel}", conn=conn)
            except Exception:
                pass

    # --- Helpers ---------------------------------------------------------
    def _quote_identifier(self, ident: str) -> str:
        return _q_ident(ident)

    def _qualified(self, relation: str, schema: str | None = None, *, quoted: bool = True) -> str:
        return self._format_identifier(relation, purpose="physical", schema=schema, quote=quoted)

    def _set_search_path(self, conn: Connection) -> None:
        if self.schema:
            conn.execute(text(f"SET LOCAL search_path = {_q_ident(self.schema)}"))

    def _extract_select_like(self, sql_or_body: str) -> str:
        """
        Normalize a SELECT/CTE body:
        - Accept full statements and strip everything before the first WITH/SELECT.
        - Strip trailing semicolons/whitespace.
        """
        s = (sql_or_body or "").lstrip()
        lower = s.lower()
        pos_with = lower.find("with")
        pos_select = lower.find("select")
        if pos_with == -1 and pos_select == -1:
            return s.rstrip(";\n\t ")
        start = min([p for p in (pos_with, pos_select) if p != -1])
        return s[start:].rstrip(";\n\t ")

    # ---------- IO ----------
    def _read_relation(self, relation: str, node: Node, deps: Iterable[str]) -> pd.DataFrame:
        qualified = self._qualified(relation)
        try:
            with self.engine.begin() as conn:
                if self.schema:
                    conn.execute(text(f'SET LOCAL search_path = "{self.schema}"'))
                return pd.read_sql_query(text(f"select * from {qualified}"), conn)
        except ProgrammingError as e:
            raise e

    def _materialize_relation(self, relation: str, df: pd.DataFrame, node: Node) -> None:
        self._write_dataframe_with_stats(relation, df, node)

    def _write_dataframe_with_stats(self, relation: str, df: pd.DataFrame, node: Node) -> None:
        start = perf_counter()
        try:
            df.to_sql(
                relation,
                self.engine,
                if_exists="replace",
                index=False,
                schema=self.schema,
                method="multi",
            )
        except SQLAlchemyError as e:
            raise ModelExecutionError(
                node_name=node.name, relation=self._qualified(relation), message=str(e)
            ) from e
        else:
            self._analyze_relations([relation])
            self.runtime_query_stats.record_dataframe(df, int((perf_counter() - start) * 1000))

    def load_seed(
        self, table: str, df: pd.DataFrame, schema: str | None = None
    ) -> tuple[bool, str, bool]:
        target_schema = schema or self.schema
        qualified = self._qualify_identifier(table, schema=target_schema)

        drop_sql = f"DROP TABLE IF EXISTS {qualified} CASCADE"
        self._execute_sql_maintenance(drop_sql)

        df.to_sql(
            table,
            self.engine,
            if_exists="replace",
            index=False,
            schema=target_schema,
            method="multi",
        )

        self._execute_sql_maintenance(f"ANALYZE {qualified}")

        return True, qualified, False

    # ---------- Python view helper ----------
    def _create_or_replace_view_from_table(
        self, view_name: str, backing_table: str, node: Node
    ) -> None:
        q_view = self._qualified(view_name)
        q_back = self._qualified(backing_table)
        try:
            with self.engine.begin() as conn:
                self._execute_sql_maintenance(f"DROP VIEW IF EXISTS {q_view} CASCADE", conn=conn)
                self._execute_sql_maintenance(
                    f"CREATE OR REPLACE VIEW {q_view} AS SELECT * FROM {q_back}", conn=conn
                )

        except Exception as e:
            raise ModelExecutionError(node.name, q_view, str(e)) from e

    def _frame_name(self) -> str:
        return "pandas"

    def _create_or_replace_view(self, target_sql: str, select_body: str, node: Node) -> None:
        try:
            self._execute_sql_maintenance(f"DROP VIEW IF EXISTS {target_sql} CASCADE")
            self._execute_sql_maintenance(f"CREATE OR REPLACE VIEW {target_sql} AS {select_body}")
        except Exception as e:
            preview = f"-- target={target_sql}\n{select_body}"
            raise ModelExecutionError(node.name, target_sql, str(e), sql_snippet=preview) from e

    def _create_or_replace_table(self, target_sql: str, select_body: str, node: Node) -> None:
        """
        Postgres does NOT support 'CREATE OR REPLACE TABLE'.
        Use DROP TABLE IF EXISTS + CREATE TABLE AS, and accept CTE bodies.
        """
        try:
            self._execute_sql_maintenance(f"DROP TABLE IF EXISTS {target_sql} CASCADE")
            self._execute_sql(f"CREATE TABLE {target_sql} AS {select_body}")
            self._analyze_relations([target_sql])
        except Exception as e:
            preview = f"-- target={target_sql}\n{select_body}"
            raise ModelExecutionError(node.name, target_sql, str(e), sql_snippet=preview) from e

    # ---------- meta ----------
    def on_node_built(self, node: Node, relation: str, fingerprint: str) -> None:
        """
        Write/update _ff_meta in the current schema after a successful build.
        """
        ensure_meta_table(self)
        upsert_meta(self, node.name, relation, fingerprint, "postgres")

    # ── Incremental API ────────────────────────────────────────────────────
    def exists_relation(self, relation: str) -> bool:
        """
        Return True if a table OR view exists for 'relation' in current schema.
        """
        sql = """
            select 1
            from information_schema.tables
            where table_schema = current_schema()
              and lower(table_name) = lower(:t)
            union all
            select 1
            from information_schema.views
            where table_schema = current_schema()
              and lower(table_name) = lower(:t)
            limit 1
            """

        return bool(self._execute_sql_maintenance(sql, {"t": relation}).fetchone())

    def create_table_as(self, relation: str, select_sql: str) -> None:
        body = self._extract_select_like(select_sql)
        qrel = self._qualified(relation)
        self._execute_sql(f"create table {qrel} as {body}")
        self._analyze_relations([relation])

    def full_refresh_table(self, relation: str, select_sql: str) -> None:
        """
        Full refresh for incremental fallbacks:
        DROP TABLE IF EXISTS + CREATE TABLE AS.
        """
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        qrel = self._qualified(relation)
        self._execute_sql(f"drop table if exists {qrel}")
        self._execute_sql(f"create table {qrel} as {body}")
        self._analyze_relations([relation])

    def incremental_insert(self, relation: str, select_sql: str) -> None:
        body = self._extract_select_like(select_sql)
        qrel = self._qualified(relation)
        self._execute_sql(f"insert into {qrel} {body}")
        self._analyze_relations([relation])

    def incremental_merge(self, relation: str, select_sql: str, unique_key: list[str]) -> None:
        """
        Portable fallback: staging + delete + insert.
        """
        body = self._extract_select_like(select_sql)
        qrel = self._qualified(relation)
        pred = " AND ".join([f"t.{k}=s.{k}" for k in unique_key])
        self._execute_sql(f"create temporary table ff_stg as {body}")
        try:
            self._execute_sql(f"delete from {qrel} t using ff_stg s where {pred}")
            self._execute_sql(f"insert into {qrel} select * from ff_stg")
            self._analyze_relations([relation])
        finally:
            self._execute_sql("drop table if exists ff_stg")

    def alter_table_sync_schema(
        self, relation: str, select_sql: str, *, mode: str = "append_new_columns"
    ) -> None:
        """
        Add new columns present in SELECT but missing on target (as text).
        """
        body = self._extract_select_like(select_sql)
        qrel = self._qualified(relation)

        with self.engine.begin() as conn:
            # Probe output columns
            cols = [r[0] for r in self._execute_sql(f"select * from ({body}) q limit 0")]

            # Existing columns in target table
            existing = {
                r[0]
                for r in self._execute_sql(
                    """
                    select column_name
                    from information_schema.columns
                    where table_schema = current_schema()
                    and lower(table_name)=lower(:t)
                    """,
                    {"t": relation},
                ).fetchall()
            }

            add = [c for c in cols if c not in existing]
            for c in add:
                self._execute_sql(f'alter table {qrel} add column "{c}" text', conn=conn)

    # ── Snapshot API ──────────────────────────────────────────────────────
    def run_snapshot_sql(self, node: Node, env: Environment) -> None:
        """
        Delegate snapshot materialization to the Postgres snapshot runtime.
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

    def execute_hook_sql(self, sql: str) -> None:
        """
        Execute one or multiple SQL statements for pre/post/on_run hooks.

        Accepts a string that may contain ';'-separated statements.
        """
        self._execute_sql(sql)

    # ---- Unit-test helpers -------------------------------------------------

    def utest_load_relation_from_rows(self, relation: str, rows: list[dict]) -> None:
        """
        Load rows into a Postgres table for unit tests (replace if exists),
        without using pandas.to_sql.
        """
        qualified = self._qualified(relation)

        if not rows:
            # Ensure an empty table exists (corner case).
            try:
                self._execute_sql_maintenance(f"DROP TABLE IF EXISTS {qualified} CASCADE")
                self._execute_sql_maintenance(f"CREATE TABLE {qualified} ()")
            except SQLAlchemyError as e:
                raise ModelExecutionError(
                    node_name=f"utest::{relation}",
                    relation=self._qualified(relation),
                    message=str(e),
                ) from e
            return

        first = rows[0]
        if not isinstance(first, dict):
            raise ModelExecutionError(
                node_name=f"utest::{relation}",
                relation=self._qualified(relation),
                message=f"Expected list[dict] for rows, got {type(first).__name__}",
            )

        cols = list(first.keys())
        col_list_sql = ", ".join(_q_ident(c) for c in cols)
        select_exprs = ", ".join(f":{c} AS {_q_ident(c)}" for c in cols)
        insert_values_sql = ", ".join(f":{c}" for c in cols)

        try:
            # Replace any existing table
            self._execute_sql_maintenance(f"DROP TABLE IF EXISTS {qualified} CASCADE")

            # Create table from first row
            create_sql = f"CREATE TABLE {qualified} AS SELECT {select_exprs}"
            self._execute_sql_maintenance(create_sql, first)

            # Insert remaining rows
            if len(rows) > 1:
                insert_sql = (
                    f"INSERT INTO {qualified} ({col_list_sql}) VALUES ({insert_values_sql})"
                )
                for row in rows[1:]:
                    self._execute_sql_maintenance(insert_sql, row)

        except SQLAlchemyError as e:
            raise ModelExecutionError(
                node_name=f"utest::{relation}",
                relation=self._qualified(relation),
                message=str(e),
            ) from e

    def utest_read_relation(self, relation: str) -> pd.DataFrame:
        """
        Read a relation as a DataFrame for unit-test assertions.
        """
        qualified = self._qualified(relation)
        with self.engine.begin() as conn:
            self._set_search_path(conn)
            return pd.read_sql_query(text(f"select * from {qualified}"), conn)

    def utest_clean_target(self, relation: str) -> None:
        """
        For unit tests: drop any view or table with this name in the configured schema.

        We avoid WrongObjectType by:
          - querying information_schema for existing table/view with this name
          - dropping only the matching kinds.
        """
        with self.engine.begin() as conn:
            # Decide which schema to inspect
            cur_schema = self._execute_sql_maintenance(
                "select current_schema()", conn=conn
            ).scalar()
            schema = self.schema or cur_schema

            # Find objects named <relation> in that schema
            info_sql = """
                select kind, table_schema, table_name from (
                  select 'table' as kind, table_schema, table_name
                  from information_schema.tables
                  where lower(table_schema) = lower(:schema)
                    and lower(table_name) = lower(:rel)
                  union all
                  select 'view' as kind, table_schema, table_name
                  from information_schema.views
                  where lower(table_schema) = lower(:schema)
                    and lower(table_name) = lower(:rel)
                ) s
                order by kind;
            """
            rows = self._execute_sql_maintenance(
                info_sql, {"schema": schema, "rel": relation}, conn=conn
            ).fetchall()

            for kind, table_schema, table_name in rows:
                qualified = f'"{table_schema}"."{table_name}"'
                if kind == "view":
                    self._execute_sql_maintenance(
                        f"DROP VIEW IF EXISTS {qualified} CASCADE", conn=conn
                    )
                else:  # table
                    self._execute_sql_maintenance(
                        f"DROP TABLE IF EXISTS {qualified} CASCADE", conn=conn
                    )

    def collect_docs_columns(self) -> dict[str, list[ColumnInfo]]:
        """
        Column metadata for docs, scoped to the effective schema.
        """
        sql = """
          select table_name, column_name, data_type, is_nullable
          from information_schema.columns
          where table_schema = current_schema()
          order by table_name, ordinal_position
        """
        try:
            with self.engine.begin() as conn:
                self._set_search_path(conn)
                rows = conn.execute(text(sql)).fetchall()
        except Exception:
            return {}

        out: dict[str, list[ColumnInfo]] = {}
        for table, col, dtype, nullable in rows:
            out.setdefault(table, []).append(
                ColumnInfo(col, str(dtype), str(nullable).upper() == "YES")
            )
        return out

    def _introspect_columns_metadata(
        self,
        table: str,
        column: str | None = None,
    ) -> list[tuple[str, str]]:
        """
        Return [(column_name, canonical_type), ...] for a Postgres table.

        Uses pg_catalog + format_type() so aliases/modifiers are represented consistently.
        """
        schema, table_name = self._normalize_table_identifier(table)

        params: dict[str, Any] = {}
        where: list[str] = []

        # table match
        where.append("lower(c.relname) = lower(:table)")
        params["table"] = table_name

        # schema match
        if schema:
            where.append("lower(n.nspname) = lower(:schema)")
            params["schema"] = schema
        else:
            where.append("n.nspname = current_schema()")

        # column match (optional)
        if column is not None:
            col = self._normalize_column_identifier(column)
            where.append("lower(a.attname) = lower(:column)")
            params["column"] = col

        where_sql = " AND ".join(where)

        sql = f"""
        select
        a.attname as column_name,
        format_type(a.atttypid, a.atttypmod) as data_type
        from pg_attribute a
        join pg_class c on c.oid = a.attrelid
        join pg_namespace n on n.oid = c.relnamespace
        where {where_sql}
        and a.attnum > 0
        and not a.attisdropped
        order by a.attnum
        """

        rows = self._execute_sql_maintenance(sql, params).fetchall()
        # Return canonical type *base* by default
        return [(str(name), _base_type(str(dtype))) for (name, dtype) in rows]

    def introspect_column_physical_type(self, table: str, column: str) -> str | None:
        """
        Postgres: read `data_type` from information_schema.columns for a single column.
        """
        rows = self._introspect_columns_metadata(table, column=column)
        return rows[0][1] if rows else None

    def introspect_table_physical_schema(self, table: str) -> dict[str, str]:
        """
        Postgres: return {lower(column_name): data_type} for all columns of `table`.
        """
        rows = self._introspect_columns_metadata(table, column=None)
        # Lower keys to match your runtime contract verifier's `.lower()` comparisons.
        return {name.lower(): dtype for (name, dtype) in rows}

    def normalize_physical_type(self, t: str | None) -> str:
        s = (t or "").strip()
        if not s:
            return ""

        # Ask Postgres to resolve the type name and return its canonical spelling.
        # Works great for aliases like TIMESTAMP / TIMESTAMPTZ / INT / etc.
        sql = """
        SELECT lower(pg_catalog.format_type(pg_catalog.to_regtype(:t), NULL))
        """
        try:
            row = self._execute_sql(sql, {"t": s}).fetchone()
            canon = row[0] if row else None
            if canon:
                return str(canon).strip()
        except Exception:
            pass

        # If Postgres can't resolve it (e.g. includes typmods like varchar(10)),
        # just return a normalized string. (Optional: you can choose to error instead.)
        return s.lower()
