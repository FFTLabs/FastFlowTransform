# src/fastflowtransform/executors/snowflake_snowpark.py
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from contextlib import suppress
from time import perf_counter
from typing import Any, cast

import pandas as pd

from fastflowtransform.contracts.runtime.snowflake_snowpark import SnowflakeSnowparkRuntimeContracts
from fastflowtransform.core import Node, relation_for
from fastflowtransform.executors._budget_runner import run_sql_with_budget
from fastflowtransform.executors._snapshot_sql_mixin import SnapshotSqlMixin
from fastflowtransform.executors._sql_identifier import SqlIdentifierMixin
from fastflowtransform.executors._test_utils import make_fetchable, rows_to_tuples
from fastflowtransform.executors.base import BaseExecutor
from fastflowtransform.executors.budget import BudgetGuard
from fastflowtransform.executors.query_stats import QueryStats
from fastflowtransform.meta import ensure_meta_table, upsert_meta
from fastflowtransform.typing import SNDF, SnowparkSession as Session


class SnowflakeSnowparkExecutor(SqlIdentifierMixin, SnapshotSqlMixin, BaseExecutor[SNDF]):
    ENGINE_NAME: str = "snowflake_snowpark"
    runtime_contracts: SnowflakeSnowparkRuntimeContracts
    """Snowflake executor operating on Snowpark DataFrames (no pandas)."""
    _BUDGET_GUARD = BudgetGuard(
        env_var="FF_SF_MAX_BYTES",
        estimator_attr="_estimate_query_bytes",
        engine_label="Snowflake",
        what="query",
    )

    def __init__(self, cfg: dict):
        # cfg: {account, user, password, warehouse, database, schema, role?}
        self.session = Session.builder.configs(cfg).create()
        self.database = cfg["database"]
        self.schema = cfg["schema"]

        self.allow_create_schema: bool = bool(cfg["allow_create_schema"])
        self._ensure_schema()
        self.runtime_contracts = SnowflakeSnowparkRuntimeContracts(self)

    def execute_test_sql(self, stmt: Any) -> Any:
        """
        Execute lightweight SQL for DQ tests via Snowpark and return fetchable rows.
        """

        def _run_one(s: Any) -> Any:
            if isinstance(s, str):
                return rows_to_tuples(self._execute_sql(s).collect())
            if isinstance(s, Iterable) and not isinstance(s, (bytes, bytearray, str)):
                res = None
                for item in s:
                    res = _run_one(item)
                return res
            return rows_to_tuples(self._execute_sql(str(s)).collect())

        return make_fetchable(_run_one(stmt))

    def compute_freshness_delay_minutes(self, table: str, ts_col: str) -> tuple[float | None, str]:
        sql = (
            f"select DATEDIFF('minute', max({ts_col}), CURRENT_TIMESTAMP())::float as delay_min "
            f"from {table}"
        )
        res = self.execute_test_sql(sql)
        row = getattr(res, "fetchone", lambda: None)()
        val = row[0] if row else None
        return (float(val) if val is not None else None, sql)

    # ---------- Cost estimation & central execution ----------

    def _estimate_query_bytes(self, sql: str) -> int | None:
        """
        Best-effort Snowflake bytes estimation.

        Uses `EXPLAIN USING TEXT` and tries to extract a "bytes=<n>"-style
        metric from the textual plan. If parsing fails or Snowflake doesn't
        expose such info, returns None and the guard is effectively disabled.
        """
        try:
            body = self._selectable_body(sql)
        except Exception:
            body = sql

        try:
            rows = self.session.sql(f"EXPLAIN USING JSON {body}").collect()
            if not rows:
                return None

            parts: list[str] = []
            for r in rows:
                try:
                    parts.append(str(r[0]))
                except Exception:
                    as_dict: dict[str, Any] = getattr(r, "asDict", lambda: {})()
                    if as_dict:
                        parts.extend(str(v) for v in as_dict.values())

            plan_text = "\n".join(parts).strip()
            if not plan_text:
                return None

            try:
                plan_data = json.loads(plan_text)
            except Exception:
                return None

            bytes_val = self._extract_bytes_from_plan(plan_data)
            if bytes_val is None or bytes_val <= 0:
                return None
            return bytes_val
        except Exception:
            # Any parsing / EXPLAIN issues → no estimate, guard skipped
            return None

    def _extract_bytes_from_plan(self, plan_data: Any) -> int | None:
        def _to_int(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except Exception:
                return None

        if isinstance(plan_data, dict):
            global_stats = plan_data.get("GlobalStats") or plan_data.get("globalStats")
            if isinstance(global_stats, dict):
                candidate = _to_int(
                    global_stats.get("bytesAssigned") or global_stats.get("bytes_assigned")
                )
                if candidate:
                    return candidate
            for val in plan_data.values():
                bytes_val = self._extract_bytes_from_plan(val)
                if bytes_val:
                    return bytes_val
        elif isinstance(plan_data, list):
            for item in plan_data:
                bytes_val = self._extract_bytes_from_plan(item)
                if bytes_val:
                    return bytes_val
        return None

    def _execute_sql(self, sql: str) -> SNDF:
        """
        Central Snowflake SQL runner.

        - Returns a Snowpark DataFrame (same as session.sql).
        - Records best-effort query stats for run_results.json.
        """

        def _exec() -> SNDF:
            return self.session.sql(sql)

        return run_sql_with_budget(
            self,
            sql,
            guard=self._BUDGET_GUARD,
            exec_fn=_exec,
            estimate_fn=self._estimate_query_bytes,
        )

    def _exec_many(self, sql: str) -> None:
        """
        Execute multiple SQL statements separated by ';' on the same connection.
        Snowflake normally accepts one statement per execute(), so we split here.
        """
        for stmt in (part.strip() for part in sql.split(";")):
            if not stmt:
                continue
            self._execute_sql(stmt).collect()

    # ---------- Helpers ----------
    def _q(self, s: str) -> str:
        return '"' + s.replace('"', '""') + '"'

    def _quote_identifier(self, ident: str) -> str:
        # Keep identifiers unquoted to match legacy Snowflake behaviour.
        return ident

    def _default_schema(self) -> str | None:
        return self.schema

    def _default_catalog(self) -> str | None:
        return self.database

    def _should_include_catalog(
        self, catalog: str | None, schema: str | None, *, explicit: bool
    ) -> bool:
        # Always include database when present; Snowflake expects DB.SCHEMA.TABLE.
        return bool(catalog)

    def _qualified(self, rel: str) -> str:
        # DATABASE.SCHEMA.TABLE  (no quotes)
        return self._format_identifier(rel, purpose="physical", quote=False)

    def _ensure_schema(self) -> None:
        """
        Best-effort schema creation when allow_create_schema=True.

        Mirrors BigQuery's `_ensure_dataset` behaviour:
        - If the flag is false → do nothing.
        - If true → `CREATE SCHEMA IF NOT EXISTS "DB"."SCHEMA"`.
        """
        if not getattr(self, "allow_create_schema", False):
            return
        if not self.database or not self.schema:
            # Misconfigured; let downstream errors surface naturally.
            return

        db = self._q(self.database)
        sch = self._q(self.schema)
        with suppress(Exception):
            # Fully qualified CREATE SCHEMA is allowed in Snowflake.
            self.session.sql(f"CREATE SCHEMA IF NOT EXISTS {db}.{sch}").collect()
            # Best-effort; permission issues or race conditions shouldn't crash the executor.
            # If the schema truly doesn't exist and we can't create it, later queries will fail
            # with a clearer engine error.

    # ---------- Frame-Hooks ----------
    def _read_relation(self, relation: str, node: Node, deps: Iterable[str]) -> SNDF:
        df = self.session.table(self._qualified(relation))
        # Present a *logical* lowercase schema to Python models:
        lowered = [c.lower() for c in df.schema.names]
        return df.toDF(*lowered)

    def _materialize_relation(self, relation: str, df: SNDF, node: Node) -> None:
        if not self._is_frame(df):
            raise TypeError("Snowpark model must return a Snowpark DataFrame")

        # Normalize to uppercase for storage in Snowflake
        cols = list(df.schema.names)
        upper_cols = [c.upper() for c in cols]
        if cols != upper_cols:
            df = df.toDF(*upper_cols)

        start = perf_counter()
        df.write.save_as_table(self._qualified(relation), mode="overwrite")
        duration_ms = int((perf_counter() - start) * 1000)
        bytes_est = self._estimate_frame_bytes(df)
        self._record_query_stats(
            QueryStats(
                bytes_processed=bytes_est,
                rows=None,
                duration_ms=duration_ms,
            )
        )

    def _estimate_frame_bytes(self, df: SNDF) -> int | None:
        """
        Best-effort bytes estimate for a Snowpark DataFrame.

        Strategy:
        1) Use DataFrame.queries["queries"] (public Snowpark API) to get SQL.
        2) Optionally fall back to df._plan.sql() if queries is missing/empty.
        3) Run our existing _estimate_query_bytes(sql_text).
        """
        try:
            sql_text = self._snowpark_df_sql(df)
            if not isinstance(sql_text, str) or not sql_text.strip():
                return None
            return self._estimate_query_bytes(sql_text)
        except Exception:
            return None

    def _snowpark_df_sql(self, df: Any) -> str | None:
        """
        Extract the main SQL statement for a Snowpark DataFrame.

        Uses the documented public APIs:
        - DataFrame.queries -> {"queries": [sql1, sql2, ...], "post_actions": [...]}
        - Optionally falls back to df._plan.sql() if needed.
        """
        # 1) Primary source: DataFrame.queries
        queries_dict = getattr(df, "queries", None)

        if isinstance(queries_dict, dict):
            queries = queries_dict.get("queries")
            if isinstance(queries, list) and queries:
                # Pick the most likely "main" query.
                # Snowflake examples use queries['queries'][0],
                # but we can be a bit safer and pick the longest non-empty SQL.
                candidates = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
                if candidates:
                    # Heuristic: longest SQL string is usually the main SELECT/CTE.
                    return max(candidates, key=len)

        # 2) Fallback: internal plan (undocumented but widely used)
        plan = getattr(df, "_plan", None)
        if plan is not None:
            # Prefer simplified plan if available
            with suppress(Exception):
                simplify = getattr(plan, "simplify", None)
                if callable(simplify):
                    simplified = simplify()
                    to_sql = getattr(simplified, "sql", None)
                    if callable(to_sql):
                        sql = to_sql()
                        if isinstance(sql, str) and sql.strip():
                            return sql.strip()

            # Raw plan.sql()
            with suppress(Exception):
                to_sql = getattr(plan, "sql", None)
                if callable(to_sql):
                    sql = to_sql()
                    if isinstance(sql, str) and sql.strip():
                        return sql.strip()

        return None

    def _create_view_over_table(self, view_name: str, backing_table: str, node: Node) -> None:
        qv = self._qualified(view_name)
        qb = self._qualified(backing_table)
        self._execute_sql(f"CREATE OR REPLACE VIEW {qv} AS SELECT * FROM {qb}").collect()

    def _validate_required(
        self, node_name: str, inputs: Any, requires: dict[str, set[str]]
    ) -> None:
        if not requires:
            return

        def cols(df: SNDF) -> set[str]:
            # Compare in lowercase to be case-insensitive for Snowflake
            return {c.lower() for c in df.schema.names}

        # Normalize the required sets too
        normalized_requires = {rel: {c.lower() for c in needed} for rel, needed in requires.items()}

        errors: list[str] = []

        if isinstance(inputs, SNDF):
            need = next(iter(normalized_requires.values()), set())
            missing = need - cols(inputs)
            if missing:
                errors.append(f"- missing columns: {sorted(missing)} | have={sorted(cols(inputs))}")
        else:
            for rel, need in normalized_requires.items():
                if rel not in inputs:
                    errors.append(f"- missing dependency key '{rel}'")
                    continue
                missing = need - cols(inputs[rel])
                if missing:
                    errors.append(
                        f"- [{rel}] missing: {sorted(missing)} | have={sorted(cols(inputs[rel]))}"
                    )

        if errors:
            raise ValueError(
                "Required columns check failed for Snowpark model "
                f"'{node_name}'.\n" + "\n".join(errors)
            )

    def _columns_of(self, frame: SNDF) -> list[str]:
        return list(frame.schema.names)

    def _is_frame(self, obj: Any) -> bool:
        # Accept real Snowpark DataFrames and test doubles with a compatible surface.
        schema = getattr(obj, "schema", None)
        return isinstance(obj, SNDF) or (
            schema is not None
            and hasattr(schema, "names")
            and callable(getattr(obj, "collect", None))
        )

    def _frame_name(self) -> str:
        return "Snowpark"

    def load_seed(
        self, table: str, df: pd.DataFrame, schema: str | None = None
    ) -> tuple[bool, str, bool]:
        """
        Materialize a pandas seed into Snowflake via Snowpark.

        - Qualifies with database + schema (defaults to configured schema).
        - Best-effort schema creation when allow_create_schema is enabled.
        - Normalizes columns to uppercase before writing.
        """
        db_part, schema_part, table_part = self._normalize_table_parts_for_introspection(table)

        if schema:
            schema_part = schema.strip().strip('`"') or schema_part

        target_db = db_part or self.database
        target_schema = schema_part or self.schema

        created_schema = False
        if target_db and target_schema and getattr(self, "allow_create_schema", False):
            db_ident = self._q(target_db)
            schema_ident = self._q(target_schema)
            try:
                self.session.sql(f"CREATE SCHEMA IF NOT EXISTS {db_ident}.{schema_ident}").collect()
                created_schema = True
            except Exception:
                # Best-effort; let the write fail later if schema truly missing.
                pass

        qualified = self._format_identifier(
            table_part,
            purpose="seed",
            schema=target_schema,
            catalog=target_db,
            quote=False,
        )

        snow_df = self.session.create_dataframe(df.reset_index(drop=True))
        cols = list(snow_df.schema.names)
        upper_cols = [c.upper() for c in cols]
        if cols != upper_cols:
            snow_df = snow_df.toDF(*upper_cols)

        snow_df.write.save_as_table(qualified, mode="overwrite")

        return True, qualified, created_schema

    # ---- SQL hooks ----
    def _this_identifier(self, node: Node) -> str:
        """
        Identifier for {{ this }} in SQL models.
        Use fully-qualified DB.SCHEMA.TABLE so all build/read/test paths agree.
        """
        return self._format_identifier(relation_for(node.name), purpose="this", quote=False)

    def _format_source_reference(
        self, cfg: dict[str, Any], source_name: str, table_name: str
    ) -> str:
        ident = cfg.get("identifier")
        if not ident:
            raise KeyError(f"Source {source_name}.{table_name} missing identifier")
        formatted = self._format_identifier(
            ident,
            purpose="source",
            source_cfg=cfg,
            source_name=source_name,
            table_name=table_name,
            quote=False,
        )
        # Ensure we resolved to DB.SCHEMA.TABLE; Snowflake needs both parts.
        if "." not in formatted:
            raise KeyError(
                f"Source {source_name}.{table_name} missing database/schema for Snowflake"
            )
        return formatted

    def _create_or_replace_view(self, target_sql: str, select_body: str, node: Node) -> None:
        self._execute_sql(f"CREATE OR REPLACE VIEW {target_sql} AS {select_body}").collect()

    def _create_or_replace_table(self, target_sql: str, select_body: str, node: Node) -> None:
        self._execute_sql(f"CREATE OR REPLACE TABLE {target_sql} AS {select_body}").collect()

    def _create_or_replace_view_from_table(
        self, view_name: str, backing_table: str, node: Node
    ) -> None:
        view_id = self._qualified(view_name)
        back_id = self._qualified(backing_table)
        self._execute_sql(f"CREATE OR REPLACE VIEW {view_id} AS SELECT * FROM {back_id}").collect()

    def _format_test_table(self, table: str | None) -> str | None:
        # Bypass mixin qualification to avoid double-qualifying already dotted names.
        formatted = BaseExecutor._format_test_table(self, table)
        if formatted is None:
            return None

        # If it's already qualified (DB.SCHEMA.TABLE) or quoted, leave it alone.
        if "." in formatted or '"' in formatted:
            return formatted

        # Otherwise, treat it as a logical relation name and fully-qualify it
        # with the executor's configured database/schema.
        return self._format_identifier(formatted, purpose="test", quote=False)

    # ---- Meta hook ----
    def on_node_built(self, node: Node, relation: str, fingerprint: str) -> None:
        """After successful materialization, upsert _ff_meta (best-effort)."""
        ensure_meta_table(self)
        upsert_meta(self, node.name, relation, fingerprint, "snowflake_snowpark")

    # ── Incremental API (parity with DuckDB/PG) ──────────────────────────
    def exists_relation(self, relation: str) -> bool:
        """Check existence via information_schema.tables."""
        db = self._q(self.database)
        schema_lit = f"'{self.schema.upper()}'"
        rel_lit = f"'{relation.upper()}'"
        q = f"""
        select 1
        from {db}.information_schema.tables
        where upper(table_schema) = {schema_lit}
            and upper(table_name) = {rel_lit}
        limit 1
        """
        try:
            return bool(self._execute_sql(q).collect())
        except Exception:
            return False

    def create_table_as(self, relation: str, select_sql: str) -> None:
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        self._execute_sql(
            f"CREATE OR REPLACE TABLE {self._qualified(relation)} AS {body}"
        ).collect()

    def full_refresh_table(self, relation: str, select_sql: str) -> None:
        """
        Engine-specific full refresh for incremental fallbacks.
        """
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        self._execute_sql(
            f"CREATE OR REPLACE TABLE {self._qualified(relation)} AS {body}"
        ).collect()

    def incremental_insert(self, relation: str, select_sql: str) -> None:
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        self._execute_sql(f"INSERT INTO {self._qualified(relation)} {body}").collect()

    def incremental_merge(self, relation: str, select_sql: str, unique_key: list[str]) -> None:
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        pred = " AND ".join([f"t.{k}=s.{k}" for k in unique_key]) or "FALSE"
        qrel = self._qualified(relation)

        # 1) Delete matching keys
        delete_sql = f"""
        DELETE FROM {qrel} AS t
        USING ({body}) AS s
        WHERE {pred}
        """
        self._execute_sql(delete_sql).collect()

        # 2) Insert all rows from the delta
        insert_sql = f"INSERT INTO {qrel} SELECT * FROM ({body})"
        self._execute_sql(insert_sql).collect()

    def alter_table_sync_schema(
        self, relation: str, select_sql: str, *, mode: str = "append_new_columns"
    ) -> None:
        """
        Best-effort additive schema sync:
        - infer SELECT schema via LIMIT 0
        - add missing columns as STRING
        """
        if mode not in {"append_new_columns", "sync_all_columns"}:
            return

        qrel = self._qualified(relation)

        # Use identifiers in FROM, but *string literals* in WHERE
        db_ident = self._q(self.database)
        schema_lit = self.schema.replace("'", "''")
        rel_lit = relation.replace("'", "''")

        try:
            existing = {
                r[0]
                for r in self._execute_sql(
                    f"""
                    select column_name
                    from {db_ident}.information_schema.columns
                    where upper(table_schema) = upper('{schema_lit}')
                    and upper(table_name)   = upper('{rel_lit}')
                    """
                ).collect()
            }
        except Exception:
            existing = set()

        # Probe SELECT columns
        body = self._first_select_body(select_sql).strip().rstrip(";\n\t ")
        probe = self.session.sql(f"SELECT * FROM ({body}) q WHERE 1=0")
        probe_cols = list(probe.schema.names)

        to_add = [c for c in probe_cols if c not in existing]
        if not to_add:
            return

        # Column names are identifiers → _q is correct here
        cols_sql = ", ".join(f"{self._q(c)} STRING" for c in to_add)
        self._execute_sql(f"ALTER TABLE {qrel} ADD COLUMN {cols_sql}").collect()

    # ---- Snapshot API (mixin hooks) --------------------------------------
    def _snapshot_target_identifier(self, rel_name: str) -> str:
        return self._qualified(rel_name)

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
        src_quoted = self._q(src_name)
        self._execute_sql(
            f"CREATE OR REPLACE TEMPORARY VIEW {src_quoted} AS {select_body}"
        ).collect()

        def _cleanup() -> None:
            self._execute_sql(f"DROP VIEW IF EXISTS {src_quoted}").collect()

        return src_quoted, _cleanup

    def execute_hook_sql(self, sql: str) -> None:
        """
        Execute one SQL statement for pre/post/on_run hooks.
        """
        self._exec_many(sql)

    # ---- Unit-test helpers -----------------------------------------------

    def utest_read_relation(self, relation: str) -> pd.DataFrame:
        """
        Read a relation into a pandas DataFrame for unit-test assertions.

        We use Snowpark to read the table and convert to pandas,
        normalizing column names to lowercase to match _read_relation.
        """
        df = self.session.table(self._qualified(relation))
        # Mirror _read_relation: present lowercase schema to the test layer
        lowered = [c.lower() for c in df.schema.names]
        df = df.toDF(*lowered)

        to_pandas = getattr(df, "to_pandas", None)

        pdf: pd.DataFrame
        if callable(to_pandas):
            pdf = cast(pd.DataFrame, to_pandas())
        else:
            rows = df.collect()
            records = [r.asDict() for r in rows]
            pdf = pd.DataFrame.from_records(records)

        # Return a new DF with lowercase columns (no attribute assignment)
        return pdf.rename(columns=lambda c: str(c).lower())

    def utest_load_relation_from_rows(self, relation: str, rows: list[dict]) -> None:
        """
        Load rows into a Snowflake table for unit tests (replace if exists).

        We build a Snowpark DataFrame from the Python rows and overwrite the
        target table using save_as_table().
        """
        # Best-effort: if rows are empty, create an empty table with no rows.
        # We assume at least one row in normal test usage so we can infer schema.
        if not rows:
            # Without any rows we don't know the schema; create a trivial
            # single-column table to surface the situation clearly.
            tmp_df = self.session.create_dataframe([[None]], schema=["__empty__"])
            tmp_df.write.save_as_table(self._qualified(relation), mode="overwrite")
            return

        # Infer column order from the first row
        first = rows[0]
        columns = list(first.keys())

        # Normalize data to a list of lists in a fixed column order
        data = [[row.get(col) for col in columns] for row in rows]

        df = self.session.create_dataframe(data, schema=columns)

        # Store with uppercase column names in Snowflake (conventional)
        upper_cols = [c.upper() for c in columns]
        if columns != upper_cols:
            df = df.toDF(*upper_cols)

        # Overwrite the target table
        df.write.save_as_table(self._qualified(relation), mode="overwrite")

    def utest_clean_target(self, relation: str) -> None:
        """
        For unit tests: drop any table or view with this name in the configured
        database/schema.

        We:
          - try DROP VIEW IF EXISTS DB.SCHEMA.REL
          - try DROP TABLE IF EXISTS DB.SCHEMA.REL

        and ignore "not a view/table" style errors so it doesn't matter what
        kind of object is currently there - after this, nothing with that name
        should remain (best-effort).
        """
        qualified = self._qualified(relation)

        # Drop view first; ignore errors if it's actually a table or doesn't exist.
        with suppress(Exception):
            self.session.sql(f"DROP VIEW IF EXISTS {qualified}").collect()

        # Then drop table; ignore errors if it's actually a view or doesn't exist.
        with suppress(Exception):
            self.session.sql(f"DROP TABLE IF EXISTS {qualified}").collect()

    def _normalize_table_parts_for_introspection(self, table: str) -> tuple[str, str, str]:
        """
        Return (database, schema, table_name) for a possibly qualified identifier.

        Accepts:
          - TABLE
          - SCHEMA.TABLE
          - DATABASE.SCHEMA.TABLE

        Quotes/backticks are stripped best-effort; names are returned as raw strings.
        """
        raw = (table or "").strip()
        raw = raw.replace('"', "").replace("`", "")
        parts = [p for p in raw.split(".") if p]

        if len(parts) >= 3:
            db, sch, tbl = parts[-3], parts[-2], parts[-1]
            return db, sch, tbl
        if len(parts) == 2:
            sch, tbl = parts[0], parts[1]
            return self.database, sch, tbl
        return self.database, self.schema, parts[0] if parts else raw

    def _compose_snowflake_type(
        self,
        data_type: Any,
        char_max: Any,
        num_precision: Any,
        num_scale: Any,
    ) -> str:
        dt = str(data_type).strip().upper()

        def _to_int(v: Any) -> int | None:
            try:
                if v is None:
                    return None
                return int(v)
            except Exception:
                return None

        SF_VARCHAR_MAX = 16777216

        if dt in {"NUMBER", "DECIMAL", "NUMERIC"}:
            p = _to_int(num_precision)
            s = _to_int(num_scale)
            if s is None:
                s = 0

            # Treat integer NUMBERs as base NUMBER so contracts can just say "NUMBER"
            if s == 0:
                return "NUMBER"

            # Only surface precision/scale for non-integer decimals
            if p is None:
                return "NUMBER"
            return f"NUMBER({p},{s})"

        if dt in {"VARCHAR", "CHAR", "CHARACTER", "STRING", "TEXT"}:
            n = _to_int(char_max)
            if n is None or n <= 0 or n >= SF_VARCHAR_MAX:
                return "VARCHAR"
            return f"VARCHAR({n})"

        return dt

    def introspect_table_physical_schema(self, table: str) -> dict[str, str]:
        """
        Snowflake: return {lower(column_name): type_string} for all columns.

        Uses <db>.information_schema.columns and composes NUMBER(p,s) / VARCHAR(n)
        when metadata is present.
        """
        db, sch, tbl = self._normalize_table_parts_for_introspection(table)

        db_ident = self._q(db)
        schema_lit = sch.replace("'", "''").upper()
        table_lit = tbl.replace("'", "''").upper()

        sql = f"""
        select
            column_name,
            data_type,
            character_maximum_length,
            numeric_precision,
            numeric_scale
        from {db_ident}.information_schema.columns
        where upper(table_schema) = '{schema_lit}'
          and upper(table_name)   = '{table_lit}'
        order by ordinal_position
        """

        rows = self._execute_sql(sql).collect()
        out: dict[str, str] = {}

        for r in rows or []:
            col_name = str(r[0]) if r and r[0] is not None else None
            if not col_name:
                continue
            typ = self._compose_snowflake_type(r[1], r[2], r[3], r[4])
            out[col_name.lower()] = typ

        return out

    def introspect_column_physical_type(self, table: str, column: str) -> str | None:
        """
        Snowflake: read column type from information_schema.columns and return a composed
        type string (e.g. NUMBER(38,0), VARCHAR(16777216), TIMESTAMP_NTZ).
        """
        db, sch, tbl = self._normalize_table_parts_for_introspection(table)

        db_ident = self._q(db)
        schema_lit = sch.replace("'", "''").upper()
        table_lit = tbl.replace("'", "''").upper()
        col_lit = (column or "").replace("'", "''").upper()

        sql = f"""
        select
            data_type,
            character_maximum_length,
            numeric_precision,
            numeric_scale
        from {db_ident}.information_schema.columns
        where upper(table_schema) = '{schema_lit}'
          and upper(table_name)   = '{table_lit}'
          and upper(column_name)  = '{col_lit}'
        limit 1
        """

        rows = self._execute_sql(sql).collect()
        if not rows:
            return None
        r = rows[0]
        return self._compose_snowflake_type(r[0], r[1], r[2], r[3])
