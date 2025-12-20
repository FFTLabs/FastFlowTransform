from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, ClassVar, Protocol

from fastflowtransform.executors.budget.core import BudgetGuard
from fastflowtransform.executors.budget.runtime.base import BaseBudgetRuntime, BudgetExecutor


class DuckBudgetExecutor(BudgetExecutor, Protocol):
    schema: str | None

    def _execute_fetchall(self, sql: str, params: Any | None = None) -> list[Any]: ...
    def _selectable_body(self, sql: str) -> str: ...


class DuckBudgetRuntime(BaseBudgetRuntime[DuckBudgetExecutor]):
    """DuckDB-specific budget runtime with plan-based estimation."""

    DEFAULT_GUARD = BudgetGuard(
        env_var="FF_DUCKDB_MAX_BYTES",
        estimator_attr="_estimate_query_bytes",
        engine_label="DuckDB",
        what="query",
    )

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

    def __init__(self, executor: DuckBudgetExecutor, guard: BudgetGuard | None = None):
        super().__init__(executor, guard)
        self._table_row_width_cache: dict[tuple[str | None, str], int] = {}

    # ------------------------------------------------------------------ #
    # Cost estimation used by BudgetGuard                                #
    # ------------------------------------------------------------------ #

    def estimate_query_bytes(self, sql: str) -> int | None:
        """
        Estimate query size via DuckDB's EXPLAIN (FORMAT JSON).
        """
        # Try to normalize to a SELECT/CTE body if the executor exposes it
        body = self.executor._selectable_body(sql).strip().rstrip(";\n\t ")

        lower = body.lower()
        if not lower.startswith(("select", "with")):
            return None

        explain_sql = f"EXPLAIN (FORMAT JSON) {body}"
        try:
            rows = self.executor._execute_fetchall(explain_sql)
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

        estimate = self._max_cardinality(plan_data)
        if estimate <= 0:
            return None

        tables = self._collect_tables_from_plan(
            plan_data if isinstance(plan_data, list) else [plan_data]
        )
        row_width = self._row_width_for_tables(tables)
        if row_width <= 0:
            row_width = self._DEFAULT_ROW_WIDTH

        bytes_estimate = int(estimate * row_width)
        return bytes_estimate if bytes_estimate > 0 else None

    def _max_cardinality(self, plan_data: Any) -> int:
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

        nodes = plan_data if isinstance(plan_data, list) else [plan_data]

        estimate = 0
        for entry in nodes:
            if isinstance(entry, dict):
                estimate = max(estimate, _walk_node(entry))
        return estimate

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
            try:
                if candidate is not None:
                    rows = self.executor._execute_fetchall(
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
                    )
                else:
                    rows = self.executor._execute_fetchall(
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
                    )
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
        _add(getattr(self.executor, "schema", None))
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
