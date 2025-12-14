# fastflowtransform/executors/duckdb.py
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar

import duckdb
import pandas as pd
from duckdb import CatalogException

from fastflowtransform.contracts.runtime.duckdb import DuckRuntimeContracts
from fastflowtransform.core import Node
from fastflowtransform.executors._budget_runner import run_sql_with_budget
from fastflowtransform.executors._snapshot_sql_mixin import SnapshotSqlMixin
from fastflowtransform.executors._sql_identifier import SqlIdentifierMixin
from fastflowtransform.executors._test_utils import make_fetchable
from fastflowtransform.executors.base import BaseExecutor, _scalar
from fastflowtransform.executors.budget import BudgetGuard
from fastflowtransform.meta import ensure_meta_table, upsert_meta


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


class DuckExecutor(SqlIdentifierMixin, SnapshotSqlMixin, BaseExecutor[pd.DataFrame]):
    ENGINE_NAME: str = "duckdb"
    runtime_contracts: DuckRuntimeContracts

    _FIXED_TYPE_SIZES: ClassVar[dict[str, int]] = {
        "boolean": 1,
        "bool": 1,
        "tinyint": 1,
        "smallint": 2,
        "integer": 4,
        "int": 4,
        "bigint": 8,
        "float": 4,
        "real": 4,
        "double": 8,
        "double precision": 8,
        "decimal": 16,
        "numeric": 16,
        "uuid": 16,
        "json": 64,
        "jsonb": 64,
        "timestamp": 8,
        "timestamp_ntz": 8,
        "timestamp_ltz": 8,
        "timestamptz": 8,
        "date": 4,
        "time": 4,
        "interval": 16,
    }
    _VARCHAR_DEFAULT_WIDTH = 64
    _VARCHAR_MAX_WIDTH = 1024
    _DEFAULT_ROW_WIDTH = 128
    _BUDGET_GUARD = BudgetGuard(
        env_var="FF_DUCKDB_MAX_BYTES",
        estimator_attr="_estimate_query_bytes",
        engine_label="DuckDB",
        what="query",
    )

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
        self._table_row_width_cache: dict[tuple[str | None, str], int] = {}
        if catalog_override:
            if self._apply_catalog_override(catalog_override):
                self.catalog = catalog_override
            else:
                self.catalog = self._detect_catalog()
        if self.schema:
            safe_schema = _q(self.schema)
            self._execute_sql(f"create schema if not exists {safe_schema}")
            self._execute_sql(f"set schema '{self.schema}'")

        self.runtime_contracts = DuckRuntimeContracts(self)

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
                return self.con.execute(s[0], s[1])
            if isinstance(s, str):
                return self.con.execute(s)
            if isinstance(s, Iterable) and not isinstance(s, (bytes, bytearray, str)):
                res = None
                for item in s:
                    res = _run_one(item)
                return res
            return self.con.execute(str(s))

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
            rc = getattr(result, "rowcount", None)
            if isinstance(rc, int) and rc >= 0:
                return rc
            return None

        return run_sql_with_budget(
            self,
            sql,
            guard=self._BUDGET_GUARD,
            exec_fn=_exec,
            rowcount_extractor=_rows,
            estimate_fn=self._estimate_query_bytes,
        )

    # --- Cost estimation for the shared BudgetGuard -----------------

    def _estimate_query_bytes(self, sql: str) -> int | None:
        """
        Estimate query size via DuckDB's EXPLAIN (FORMAT JSON).

        The JSON plan exposes an \"Estimated Cardinality\" per node.
        We walk the parsed tree, take the highest non-zero estimate and
        return it as a byte-estimate surrogate (row count ≈ bytes) so the
        cost guard can still make a meaningful decision without executing
        the query.
        """
        try:
            body = self._selectable_body(sql).strip().rstrip(";\n\t ")
        except AttributeError:
            body = sql.strip().rstrip(";\n\t ")

        lower = body.lower()
        if not lower.startswith(("select", "with")):
            return None

        explain_sql = f"EXPLAIN (FORMAT JSON) {body}"
        try:
            rows = self.con.execute(explain_sql).fetchall()
        except Exception:
            return None

        if not rows:
            return None

        fragments: list[str] = []
        for row in rows:
            for cell in row:
                if cell is None:
                    continue
                fragments.append(str(cell))

        if not fragments:
            return None

        plan_text = "\n".join(fragments).strip()
        start = plan_text.find("[")
        end = plan_text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None

        try:
            plan_data = json.loads(plan_text[start : end + 1])
        except Exception:
            return None

        def _to_int(value: Any) -> int | None:
            if value is None:
                return None
            if isinstance(value, (int, float)):
                try:
                    converted = int(value)
                except Exception:
                    return None
                return converted
            text = str(value)
            match = re.search(r"(\d+(?:\.\d+)?)", text)
            if not match:
                return None
            try:
                return int(float(match.group(1)))
            except ValueError:
                return None

        def _walk_node(node: dict[str, Any]) -> int:
            best = 0
            extra = node.get("extra_info") or {}
            for key in (
                "Estimated Cardinality",
                "estimated_cardinality",
                "Cardinality",
                "cardinality",
            ):
                candidate = _to_int(extra.get(key))
                if candidate is not None:
                    best = max(best, candidate)
            candidate = _to_int(node.get("cardinality"))
            if candidate is not None:
                best = max(best, candidate)
            for child in node.get("children") or []:
                if isinstance(child, dict):
                    best = max(best, _walk_node(child))
            return best

        nodes: list[Any]
        nodes = plan_data if isinstance(plan_data, list) else [plan_data]

        estimate = 0
        for entry in nodes:
            if isinstance(entry, dict):
                estimate = max(estimate, _walk_node(entry))

        if estimate <= 0:
            return None

        tables = self._collect_tables_from_plan(nodes)
        row_width = self._row_width_for_tables(tables)
        if row_width <= 0:
            row_width = self._DEFAULT_ROW_WIDTH

        bytes_estimate = int(estimate * row_width)
        return bytes_estimate if bytes_estimate > 0 else None

    def _collect_tables_from_plan(self, nodes: list[dict[str, Any]]) -> set[tuple[str | None, str]]:
        tables: set[tuple[str | None, str]] = set()

        def _walk(entry: dict[str, Any]) -> None:
            extra = entry.get("extra_info") or {}
            table_val = extra.get("Table")
            schema_val = extra.get("Schema") or extra.get("Database") or extra.get("Catalog")
            if isinstance(table_val, str) and table_val.strip():
                schema, table = self._split_identifier(table_val, schema_val)
                if table:
                    tables.add((schema, table))
            for child in entry.get("children") or []:
                if isinstance(child, dict):
                    _walk(child)

        for node in nodes:
            if isinstance(node, dict):
                _walk(node)
        return tables

    def _split_identifier(
        self, identifier: str, explicit_schema: str | None
    ) -> tuple[str | None, str]:
        parts = [part.strip() for part in identifier.split(".") if part.strip()]
        if not parts:
            return explicit_schema, identifier
        if len(parts) >= 2:
            schema_candidate = self._strip_quotes(parts[-2])
            table_candidate = self._strip_quotes(parts[-1])
            return schema_candidate or explicit_schema, table_candidate
        return explicit_schema, self._strip_quotes(parts[-1])

    def _strip_quotes(self, value: str) -> str:
        if value.startswith('"') and value.endswith('"'):
            return value[1:-1]
        return value

    def _row_width_for_tables(self, tables: Iterable[tuple[str | None, str]]) -> int:
        widths: list[int] = []
        for schema, table in tables:
            width = self._row_width_for_table(schema, table)
            if width > 0:
                widths.append(width)
        return max(widths) if widths else 0

    def _row_width_for_table(self, schema: str | None, table: str) -> int:
        key = (schema or "", table.lower())
        cached = self._table_row_width_cache.get(key)
        if cached:
            return cached

        columns = self._columns_for_table(table, schema)
        width = sum(self._estimate_column_width(col) for col in columns)
        if width <= 0:
            width = self._DEFAULT_ROW_WIDTH
        self._table_row_width_cache[key] = width
        return width

    def _columns_for_table(
        self, table: str, schema: str | None
    ) -> list[tuple[str | None, int | None, int | None, int | None]]:
        table_lower = table.lower()
        columns: list[tuple[str | None, int | None, int | None, int | None]] = []
        seen_schemas: set[str | None] = set()
        for candidate in self._schema_candidates(schema):
            if candidate in seen_schemas:
                continue
            seen_schemas.add(candidate)
            if candidate is not None:
                try:
                    rows = self.con.execute(
                        """
                        select lower(data_type) as dtype,
                               character_maximum_length,
                               numeric_precision,
                               numeric_scale
                        from information_schema.columns
                        where lower(table_name)=lower(?)
                          and lower(table_schema)=lower(?)
                        order by ordinal_position
                        """,
                        [table_lower, candidate.lower()],
                    ).fetchall()
                except Exception:
                    continue
            else:
                try:
                    rows = self.con.execute(
                        """
                        select lower(data_type) as dtype,
                               character_maximum_length,
                               numeric_precision,
                               numeric_scale
                        from information_schema.columns
                        where lower(table_name)=lower(?)
                        order by lower(table_schema), ordinal_position
                        """,
                        [table_lower],
                    ).fetchall()
                except Exception:
                    continue
            if rows:
                return rows
        return columns

    def _schema_candidates(self, schema: str | None) -> list[str | None]:
        candidates: list[str | None] = []

        def _add(value: str | None) -> None:
            normalized = self._normalize_schema(value)
            if normalized not in candidates:
                candidates.append(normalized)

        _add(schema)
        _add(self.schema)
        for alt in ("main", "temp"):
            _add(alt)
        _add(None)
        return candidates

    def _normalize_schema(self, schema: str | None) -> str | None:
        if not schema:
            return None
        stripped = schema.strip()
        return stripped or None

    def _estimate_column_width(
        self, column_info: tuple[str | None, int | None, int | None, int | None]
    ) -> int:
        dtype_raw, char_max, numeric_precision, _ = column_info
        dtype = self._normalize_data_type(dtype_raw)
        if dtype and dtype in self._FIXED_TYPE_SIZES:
            return self._FIXED_TYPE_SIZES[dtype]

        if dtype in {"character", "varchar", "char", "text", "string"}:
            if char_max and char_max > 0:
                return min(char_max, self._VARCHAR_MAX_WIDTH)
            return self._VARCHAR_DEFAULT_WIDTH

        if dtype in {"varbinary", "blob", "binary"}:
            if char_max and char_max > 0:
                return min(char_max, self._VARCHAR_MAX_WIDTH)
            return self._VARCHAR_DEFAULT_WIDTH

        if dtype in {"numeric", "decimal"} and numeric_precision and numeric_precision > 0:
            return min(max(int(numeric_precision), 16), 128)

        return 16

    def _normalize_data_type(self, dtype: str | None) -> str | None:
        if not dtype:
            return None
        stripped = dtype.strip().lower()
        if "(" in stripped:
            stripped = stripped.split("(", 1)[0].strip()
        if stripped.endswith("[]"):
            stripped = stripped[:-2]
        return stripped or None

    def _detect_catalog(self) -> str | None:
        try:
            rows = self._execute_sql("PRAGMA database_list").fetchall()
            if rows:
                return str(rows[0][1])
        except Exception:
            return None
        return None

    def _apply_catalog_override(self, name: str) -> bool:
        alias = name.strip()
        if not alias:
            return False
        try:
            if self.db_path != ":memory:":
                resolved = str(Path(self.db_path).resolve())
                with suppress(Exception):
                    self._execute_sql(f"detach database {_q(alias)}")
                self._execute_sql(f"attach database '{resolved}' as {_q(alias)} (READ_ONLY FALSE)")
            self._execute_sql(f"set catalog '{alias}'")
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
        return _q(ident)

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
                for r in self._execute_sql(
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
                self._execute_sql(f'drop view if exists "{tmp}"')

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
        if self._execute_sql(sql_tables, params).fetchone():
            return True
        sql_views = f"select 1 from information_schema.views where {where} limit 1"
        return bool(self._execute_sql(sql_views, params).fetchone())

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
        probe = self._execute_sql(f"select * from ({body}) as q limit 0")
        cols = [c[0] for c in probe.description or []]
        existing = {
            r[0]
            for r in self._execute_sql(
                "select column_name from information_schema.columns "
                + "where lower(table_name)=lower(?)"
                + (" and lower(table_schema)=lower(?)" if self.schema else ""),
                ([relation, self.schema] if self.schema else [relation]),
            ).fetchall()
        }
        add = [c for c in cols if c not in existing]
        for c in add:
            col = _q(c)
            target = self._qualified(relation)
            try:
                self._execute_sql(f"alter table {target} add column {col} varchar")
            except Exception:
                self._execute_sql(f"alter table {target} add column {col} varchar")

    def execute_hook_sql(self, sql: str) -> None:
        """
        Execute one or multiple SQL statements for pre/post/on_run hooks.

        Accepts a string that may contain ';'-separated statements.
        """
        self._exec_many(sql)

    # ---- Snapshot mixin hooks ----
    def _snapshot_target_identifier(self, rel_name: str) -> str:
        return self._qualified(rel_name)

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
        src_quoted = _q(src_view_name)
        self._execute_sql(f"create or replace temp view {src_quoted} as {select_body}")

        def _cleanup() -> None:
            self._execute_sql(f"drop view if exists {src_quoted}")

        return src_quoted, _cleanup

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
            self._execute_sql(f"create or replace table {target} as select * from {tmp}")
        finally:
            with suppress(Exception):
                self.con.unregister(tmp)
            # Fallback for older DuckDB where unregister might not exist
            with suppress(Exception):
                self._execute_sql(f'drop view if exists "{tmp}"')

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
            self._execute_sql(f"drop view if exists {target}")
        with suppress(Exception):
            self._execute_sql(f"drop table if exists {target}")

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

        rows = self._execute_sql(sql, params).fetchall()

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
            safe_schema = _q(target_schema)
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
                self._execute_sql(f'drop view if exists "{tmp}"')

        return True, qualified, created_schema
