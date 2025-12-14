# src/fastflowtransform/executors/databricks_spark.py
from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import suppress
from functools import reduce
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from urllib.parse import unquote, urlparse

import pandas as pd
from jinja2 import Environment

from fastflowtransform import storage
from fastflowtransform.contracts.runtime.databricks_spark import DatabricksSparkRuntimeContracts
from fastflowtransform.core import REGISTRY, Node, relation_for
from fastflowtransform.errors import ModelExecutionError
from fastflowtransform.executors._budget_runner import run_sql_with_budget
from fastflowtransform.executors._query_stats_adapter import SparkDataFrameStatsAdapter
from fastflowtransform.executors._spark_imports import (
    get_spark_functions,
    get_spark_window,
)
from fastflowtransform.executors._test_utils import make_fetchable, rows_to_tuples
from fastflowtransform.executors.base import BaseExecutor
from fastflowtransform.executors.budget import BudgetGuard
from fastflowtransform.logging import echo, echo_debug
from fastflowtransform.meta import ensure_meta_table, upsert_meta
from fastflowtransform.snapshots import resolve_snapshot_config
from fastflowtransform.table_formats import get_spark_format_handler
from fastflowtransform.table_formats.base import SparkFormatHandler
from fastflowtransform.typing import SDF, DataType, SparkSession

# ---------------------------------------------------------------------------
# Delta integration
# ---------------------------------------------------------------------------

# Enable Delta Lake via delta-spark when available
configure_spark_with_delta_pip: Callable[..., Any] | None
try:
    from delta import configure_spark_with_delta_pip as _configure_spark_with_delta_pip

    configure_spark_with_delta_pip = _configure_spark_with_delta_pip
except Exception:  # pragma: no cover
    configure_spark_with_delta_pip = None

_DELTA_EXTENSION = "io.delta.sql.DeltaSparkSessionExtension"
_DELTA_CATALOG = "org.apache.spark.sql.delta.catalog.DeltaCatalog"


def _csv_tokens(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part and part.strip()]


def _ensure_csv_token(value: str | None, token: str) -> tuple[str | None, bool]:
    tokens = _csv_tokens(value)
    if token in tokens:
        return value, False
    tokens.append(token)
    return ",".join(tokens), True


def _as_nonempty_str(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_conf(spark: SparkSession, key: str, default: str = "<unset>") -> str:
    try:
        return str(spark.conf.get(key, default))
    except Exception as exc:
        return f"<error: {exc}>"


def _has_delta(spark: SparkSession) -> bool:
    """
    Best-effort Delta availability check that works with:
      * local Spark + delta-spark
      * Databricks runtime
      * Databricks Connect

    We first inspect Spark configuration, then fall back to checking that
    delta-spark is importable, and finally use the old JVM heuristic for
    plain local Spark.
    """
    # 1) Look at Spark SQL extensions (delta-spark & Databricks both wire this)
    try:
        exts = spark.conf.get("spark.sql.extensions", "")
        if _DELTA_EXTENSION in str(exts):
            return True
    except Exception:
        pass

    # 2) If delta-spark is importable, we assume Delta is available
    #    (this covers Databricks Connect as well in practice)
    try:
        from delta.tables import DeltaTable  # noqa PLC0415

        _ = DeltaTable  # silence linters; import succeeded
        return True
    except Exception:
        pass

    # 3) Fallback: old JVM classpath heuristic for bare Spark installs
    def _handles() -> list[Any]:
        refs: list[Any] = []
        with suppress(Exception):
            refs.append(getattr(spark, "_jvm", None))
        with suppress(Exception):
            sc = getattr(spark, "sparkContext", None)
            if sc:
                gw = getattr(sc, "_gateway", None)
                if gw:
                    refs.append(getattr(gw, "jvm", None))
        return [ref for ref in refs if ref is not None]

    def _try_for_name(jvm: Any) -> bool:
        candidates: list[Any] = []

        java_pkg = getattr(jvm, "java", None)
        if java_pkg is not None:
            with suppress(Exception):
                candidates.append(java_pkg.lang.Class)

        lang_pkg = getattr(jvm, "lang", None)
        if lang_pkg is not None:
            with suppress(Exception):
                candidates.append(lang_pkg.Class)

        cls = getattr(jvm, "Class", None)
        if cls is not None:
            candidates.append(cls)

        for target in candidates:
            try:
                target.forName(_DELTA_CATALOG)
                return True
            except Exception:
                continue
        return False

    return any(_try_for_name(handle) for handle in _handles())


def _log_delta_capabilities(
    spark: SparkSession,
    *,
    wants_delta: bool,
    delta_ok: bool,
    user_spark: SparkSession | None,
    table_format: str | None,
) -> None:
    """
    Debug helper: log what we know about Spark/Delta capabilities.
    Useful for environments like Databricks Connect where JVM probing is tricky.
    """
    lines: list[str] = []
    lines.append("=== DatabricksSparkExecutor capabilities ===")
    lines.append(f"Spark version: {getattr(spark, 'version', '<unknown>')}")
    lines.append(f"user_spark_provided: {user_spark is not None}")
    lines.append(f"table_format: {table_format!r}")
    lines.append(f"wants_delta: {wants_delta}")
    lines.append(f"delta_ok: {delta_ok}")
    lines.append(f"spark.sql.extensions: {_safe_conf(spark, 'spark.sql.extensions')}")
    lines.append(
        f"spark.sql.catalog.spark_catalog: {_safe_conf(spark, 'spark.sql.catalog.spark_catalog')}"
    )

    # Check whether delta-spark is importable
    try:
        from delta.tables import DeltaTable  # noqa PLC0415

        _ = DeltaTable
        lines.append("delta.tables.DeltaTable import: OK")
    except Exception as exc:
        lines.append(f"delta.tables.DeltaTable import: FAILED ({exc})")

    echo_debug("\n".join(lines))


class DatabricksSparkExecutor(BaseExecutor[SDF]):
    """Spark/Databricks executor without pandas: Python models operate on Spark DataFrames."""

    ENGINE_NAME: str = "databricks_spark"
    runtime_contracts: DatabricksSparkRuntimeContracts
    _BUDGET_GUARD = BudgetGuard(
        env_var="FF_SPK_MAX_BYTES",
        estimator_attr="_estimate_query_bytes",
        engine_label="Databricks/Spark",
        what="query",
    )

    def __init__(
        self,
        master: str = "local[*]",
        app_name: str = "fastflowtransform",
        *,
        extra_conf: dict[str, Any] | None = None,
        warehouse_dir: str | None = None,
        use_hive_metastore: bool = False,
        catalog: str | None = None,
        database: str | None = None,
        table_format: str | None = "parquet",
        table_options: dict[str, Any] | None = None,
        spark: SparkSession | None = None,
    ):
        extra_conf = dict(extra_conf or {})
        self._user_spark = spark

        builder = SparkSession.builder.master(master).appName(app_name)
        catalog_key = "spark.sql.catalog.spark_catalog"
        ext_key = "spark.sql.extensions"

        # Warehouse directory
        warehouse_path: Path | None = None
        if warehouse_dir:
            warehouse_path = Path(warehouse_dir).expanduser()
            if not warehouse_path.is_absolute():
                warehouse_path = Path.cwd() / warehouse_path
            warehouse_path.mkdir(parents=True, exist_ok=True)
            builder = builder.config("spark.sql.warehouse.dir", str(warehouse_path))

        catalog_value = _as_nonempty_str(catalog)
        if catalog_value:
            builder = builder.config(catalog_key, catalog_value)

        # Extra config
        if extra_conf:
            for key, value in extra_conf.items():
                if value is not None:
                    builder = builder.config(str(key), str(value))

        if use_hive_metastore:
            builder = builder.config("spark.sql.catalogImplementation", "hive")
            builder = builder.enableHiveSupport()

        fmt_requested = (table_format or "").strip().lower()
        wants_delta = fmt_requested == "delta"

        # Apply Delta configuration last, after all Spark configs are set.
        if not wants_delta and self._user_spark is None:
            catalog_overridden = bool(catalog_value)
            if not catalog_overridden:
                # Leave Spark catalog untouched; downstream environments may supply
                # their own defaults (e.g., Unity, Glue). We only force a catalog
                # when the user explicitly opts into Delta.
                pass

        # Apply Delta configuration last, after all Spark configs are set.
        if wants_delta and self._user_spark is None:
            if configure_spark_with_delta_pip is None:
                raise RuntimeError(
                    "Delta table_format requested for DatabricksSparkExecutor, "
                    "but 'delta-spark' is not installed. "
                    "Install it with: pip install delta-spark"
                )
            builder = configure_spark_with_delta_pip(builder)

            ext_value = _as_nonempty_str(extra_conf.get(ext_key))
            merged_ext, changed = _ensure_csv_token(ext_value, _DELTA_EXTENSION)
            if changed or ext_value is None:
                builder = builder.config(ext_key, merged_ext)

            extra_catalog = _as_nonempty_str(extra_conf.get(catalog_key))
            catalog_overridden = bool(catalog_value) or bool(extra_catalog)
            if not catalog_overridden:
                builder = builder.config(catalog_key, _DELTA_CATALOG)

        self.spark = self._user_spark or builder.getOrCreate()
        self._registered_path_sources: dict[str, dict[str, Any]] = {}
        self.warehouse_dir = warehouse_path
        self.catalog = catalog
        self.database = database
        self.schema = database

        if database:
            self._execute_sql(f"CREATE DATABASE IF NOT EXISTS `{database}`")
            with suppress(Exception):
                self.spark.catalog.setCurrentDatabase(database)

        self.spark_table_format: str | None = fmt_requested or None
        self.spark_table_options = {str(k): str(v) for k, v in (table_options or {}).items()}

        # ---- Delta availability check ----
        self._delta_ok = _has_delta(self.spark)

        # Log capabilities whenever Delta is requested or detected
        if wants_delta or self._delta_ok:
            _log_delta_capabilities(
                self.spark,
                wants_delta=wants_delta,
                delta_ok=self._delta_ok,
                user_spark=self._user_spark,
                table_format=self.spark_table_format,
            )

        if wants_delta and not self._delta_ok and self._user_spark is None:
            raise RuntimeError(
                "Delta table_format requested, but the Delta Lake classes are not available. "
                "Install delta-spark or provide a SparkSession already configured for Delta."
            )

        # Unified format handler for managed tables (Delta, Iceberg, generic Parquet/ORC/etc.)
        self._format_handler: SparkFormatHandler = get_spark_format_handler(
            self.spark_table_format,
            self.spark,
            table_options=self.spark_table_options,
            sql_runner=self._execute_sql,
        )

        self._spark_default_size = self._detect_default_size()
        self.runtime_contracts = DatabricksSparkRuntimeContracts(self)

    # ---------- Cost estimation & central execution ----------

    def _detect_default_size(self) -> int:
        """
        Detect Spark's defaultSizeInBytes sentinel.

        - Prefer spark.sql.defaultSizeInBytes if available.
        - Fall back to Long.MaxValue (2^63 - 1) otherwise.
        """
        try:
            conf_val = self.spark.conf.get("spark.sql.defaultSizeInBytes")
            if conf_val is not None:
                return int(conf_val)
        except Exception:
            # config not set / older Spark / weird environment
            pass

        # Fallback: Spark uses Long.MaxValue by default
        return 2**63 - 1  # 9223372036854775807

    def _parse_spark_stats_size(self, size_val: Any) -> int | None:
        if size_val is None:
            return None
        try:
            size_int = int(str(size_val))
        except Exception:
            return None
        return size_int if size_int > 0 else None

    def _jplan_uses_default_size(self, jplan: Any) -> bool:
        """
        Recursively walk a JVM LogicalPlan and return True if any node's
        stats.sizeInBytes equals spark.sql.defaultSizeInBytes.
        """
        if self._spark_default_size is None:
            return False

        try:
            stats = jplan.stats()
            size_val = stats.sizeInBytes()
            size_int = int(str(size_val))
            if size_int == self._spark_default_size:
                return True
        except Exception:
            # ignore stats errors and keep walking
            pass

        # children() is a Scala Seq[LogicalPlan]; iterate via .size() / .apply(i)
        try:
            children = jplan.children()
            n = children.size()
            for idx in range(n):
                child = children.apply(idx)
                if self._jplan_uses_default_size(child):
                    return True
        except Exception:
            # if we can't inspect children, stop here
            pass

        return False

    def _spark_plan_bytes(self, sql: str) -> int | None:
        """
        Inspect the optimized logical plan via the JVM and return sizeInBytes
        as an integer, or None if not available.

        This does *not* execute the query; it only goes through analysis/planning.
        """
        try:
            normalized = self._selectable_body(sql).rstrip(";\n\t ")
            if not normalized:
                normalized = sql
        except Exception:
            normalized = sql

        stmt = normalized.lstrip().lower()
        if not stmt.startswith(("select", "with")):
            # DDL/DML statements (ALTER/INSERT/etc.) should not be executed twice.
            return None

        try:
            df = self.spark.sql(normalized)

            jdf = cast(Any, getattr(df, "_jdf", None))
            if jdf is None:
                return None

            qe = jdf.queryExecution()
            jplan = qe.optimizedPlan()

            # If any node relies on defaultSizeInBytes, we don't trust the stats
            if self._jplan_uses_default_size(jplan):
                return None

            stats = jplan.stats()

            size_attr = getattr(stats, "sizeInBytes", None)
            size_val = size_attr() if callable(size_attr) else size_attr

            return self._parse_spark_stats_size(size_val)
        except Exception:
            return None

    def _estimate_query_bytes(self, sql: str) -> int | None:
        """
        Best-effort logical-plan size estimate using Spark's stats.

        It inspects the optimized plan's sizeInBytes via the JVM API without
        executing the query. If unavailable or unsupported, returns None and
        the guard is effectively disabled.
        """
        return self._spark_plan_bytes(sql)

    def execute_test_sql(self, stmt: Any) -> Any:
        """
        Execute lightweight SQL for DQ tests via Spark and return fetchable rows.
        """

        def _run_one(s: Any) -> Any:
            if isinstance(s, str):
                return rows_to_tuples(self.spark.sql(s).collect())
            if isinstance(s, Iterable) and not isinstance(s, (bytes, bytearray, str)):
                res = None
                for item in s:
                    res = _run_one(item)
                return res
            return rows_to_tuples(self.spark.sql(str(s)).collect())

        return make_fetchable(_run_one(stmt))

    def compute_freshness_delay_minutes(self, table: str, ts_col: str) -> tuple[float | None, str]:
        sql = (
            f"select (unix_timestamp(current_timestamp()) - unix_timestamp(max({ts_col}))) / 60.0 "
            f"as delay_min from {table}"
        )
        res = self.execute_test_sql(sql)
        row = getattr(res, "fetchone", lambda: None)()
        val = row[0] if row else None
        return (float(val) if val is not None else None, sql)

    def _execute_sql(self, sql: str) -> SDF:
        """
        Central Spark SQL runner.

        - Guarded by FF_SPK_MAX_BYTES via the cost guard.
        - Returns a Spark DataFrame (same as spark.sql).
        - Records best-effort query stats for run_results.json.
        """

        def _exec() -> SDF:
            return self.spark.sql(sql)

        return run_sql_with_budget(
            self,
            sql,
            guard=self._BUDGET_GUARD,
            exec_fn=_exec,
            estimate_fn=self._spark_plan_bytes,
            post_estimate_fn=lambda _, __: self._spark_plan_bytes(sql),
        )

    # ---------- Frame hooks (required) ----------
    def _read_relation(self, relation: str, node: Node, deps: Iterable[str]) -> SDF:
        # relation may optionally be "db.table" (via source()/ref())
        physical = self._format_handler.qualify_identifier(relation, database=self.database)
        return self.spark.table(physical)

    def _materialize_relation(self, relation: str, df: SDF, node: Node) -> None:
        if not self._is_frame(df):
            raise TypeError("Spark model must return a Spark DataFrame")
        storage_meta = self._storage_meta(node, relation)
        # Delegate managed/unmanaged handling to _save_df_as_table so Iceberg
        # (or other handlers) can consistently enforce managed tables.
        start = perf_counter()
        self._save_df_as_table(relation, df, storage=storage_meta)
        duration_ms = int((perf_counter() - start) * 1000)
        self._record_spark_dataframe_stats(df, duration_ms)

    def _create_view_over_table(self, view_name: str, backing_table: str, node: Node) -> None:
        """Compatibility hook: create a simple SELECT * view over an existing table."""
        view_sql = self._sql_identifier(view_name)
        backing_sql = self._sql_identifier(backing_table)
        self._execute_sql(f"CREATE OR REPLACE VIEW {view_sql} AS SELECT * FROM {backing_sql}")

    def _validate_required(
        self, node_name: str, inputs: Any, requires: dict[str, set[str]]
    ) -> None:
        if not requires:
            return

        def cols(df: SDF) -> set[str]:
            return set(df.schema.fieldNames())

        errors: list[str] = []
        # Single dependency: requires typically contains exactly one entry (ignore the key)
        if isinstance(inputs, SDF):
            need = next(iter(requires.values()), set())
            missing = need - cols(inputs)
            if missing:
                errors.append(f"- missing columns: {sorted(missing)} | have={sorted(cols(inputs))}")
        else:
            # Multiple dependencies: keys in requires = physical relations (relation_for(dep))
            for rel, need in requires.items():
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
                "Required columns check failed for Spark model "
                f"'{node_name}'.\n" + "\n".join(errors)
            )

    def _columns_of(self, frame: SDF) -> list[str]:  # pragma: no cover
        return frame.schema.fieldNames()

    def _is_frame(self, obj: Any) -> bool:  # pragma: no cover
        return isinstance(obj, SDF)

    def _frame_name(self) -> str:  # pragma: no cover
        return "Spark"

    # ---- Helpers ----
    @staticmethod
    def _q_ident(value: str | None) -> str:
        if value is None:
            return ""
        return f"`{value.replace('`', '``')}`"

    def _storage_meta(self, node: Node | None, relation: str) -> dict[str, Any]:
        """
        Retrieve configured storage overrides for the logical node backing `relation`.
        """
        rel_clean = self._strip_quotes(relation)

        # 1) Direct node meta / storage config
        if node is not None:
            meta = dict((node.meta or {}).get("storage") or {})
            if meta:
                return meta
            lookup = storage.get_model_storage(node.name)
            if lookup:
                return lookup

        # 2) Search REGISTRY nodes by relation_for(name)
        for cand in getattr(REGISTRY, "nodes", {}).values():
            try:
                if self._strip_quotes(relation_for(cand.name)) == rel_clean:
                    meta = dict((cand.meta or {}).get("storage") or {})
                    if meta:
                        return meta
                    lookup = storage.get_model_storage(cand.name)
                    if lookup:
                        return lookup
            except Exception:
                continue

        # 3) Direct storage override by relation name
        return storage.get_model_storage(rel_clean)

    def _write_to_storage_path(self, relation: str, df: SDF, storage_meta: dict[str, Any]) -> None:
        parts = self._identifier_parts(relation)
        identifier = ".".join(parts)

        storage.spark_write_to_path(
            self.spark,
            identifier,
            df,
            storage=storage_meta,
            default_format=self.spark_table_format,
            default_options=self.spark_table_options,
        )

        path = storage_meta.get("path")
        if path:
            with suppress(Exception):
                self.spark.catalog.refreshByPath(path)

    def _record_spark_dataframe_stats(self, df: SDF, duration_ms: int) -> None:
        adapter = SparkDataFrameStatsAdapter(self._spark_dataframe_bytes)
        stats = adapter.collect(df, duration_ms=duration_ms)
        self._record_query_stats(stats)

    def _spark_dataframe_bytes(self, df: SDF) -> int | None:
        try:
            jdf = cast(Any, getattr(df, "_jdf", None))
            if jdf is None:
                return None

            qe = jdf.queryExecution()
            jplan = qe.optimizedPlan()

            if self._jplan_uses_default_size(jplan):
                return None

            stats = jplan.stats()
            size_attr = getattr(stats, "sizeInBytes", None)
            size_val = size_attr() if callable(size_attr) else size_attr
            return self._parse_spark_stats_size(size_val)
        except Exception:
            return None

    # ---- SQL hooks ----
    def _format_relation_for_ref(self, name: str) -> str:
        """
        Format a ref(...) relation for use in SQL.

        - Default: just backtick-quote the logical relation name.
        - Iceberg: qualify with the Iceberg catalog so that models point at
          tables in `iceberg.<db>.<table>`, matching the seed & incremental
          write path.
        """
        base = relation_for(name)
        return self._sql_identifier(base)

    def _this_identifier(self, node: Node) -> str:
        base = relation_for(node.name)
        return self._sql_identifier(base)

    def _format_source_reference(
        self, cfg: dict[str, Any], source_name: str, table_name: str
    ) -> str:
        location = cfg.get("location")
        identifier = cfg.get("identifier")

        if location:
            alias = identifier or f"__ff_src_{source_name}_{table_name}"
            fmt_src = cfg.get("format")
            if not fmt_src:
                raise KeyError(
                    f"Source {source_name}.{table_name} requires 'format' when using a location"
                )

            options = dict(cfg.get("options") or {})
            descriptor = {
                "location": location,
                "format": fmt_src,
                "options": options,
            }
            existing = self._registered_path_sources.get(alias)
            if existing != descriptor:
                reader = self.spark.read.format(fmt_src)
                if options:
                    reader = reader.options(**options)
                df = reader.load(location)
                df.createOrReplaceTempView(alias)
                self._registered_path_sources[alias] = descriptor
            return self._q_ident(alias)

        if not identifier:
            raise KeyError(f"Source {source_name}.{table_name} missing identifier")
        catalog = cfg.get("catalog")
        schema = cfg.get("schema") or cfg.get("database")
        if catalog or schema:
            logical = ".".join([p for p in (catalog, schema, identifier) if p])
            return self._sql_identifier(logical)

        fallback_db = self.database or self.spark.catalog.currentDatabase()
        return self._sql_identifier(str(identifier), database=fallback_db)

    def _format_test_table(self, table: str | None) -> str | None:
        formatted = super()._format_test_table(table)
        if not isinstance(formatted, str):
            return formatted
        return self._format_handler.format_test_table(formatted, database=self.database)

    # ---- Spark table helpers ----
    @staticmethod
    def _strip_quotes(identifier: str) -> str:
        return identifier.replace("`", "").replace('"', "")

    def _identifier_parts(self, identifier: str) -> list[str]:
        cleaned = self._strip_quotes(identifier)
        return [part for part in cleaned.split(".") if part]

    def _physical_identifier(self, identifier: str, *, database: str | None = None) -> str:
        db = database if database is not None else self.database
        return self._format_handler.qualify_identifier(identifier, database=db)

    def _sql_identifier(self, identifier: str, *, database: str | None = None) -> str:
        db = database if database is not None else self.database
        return self._format_handler.format_identifier_for_sql(identifier, database=db)

    def _warehouse_base(self) -> Path | None:
        try:
            conf_val = self.spark.conf.get("spark.sql.warehouse.dir", "spark-warehouse")
        except Exception:
            conf_val = "spark-warehouse"

        if not isinstance(conf_val, str):
            conf_val = str(conf_val)
        parsed = urlparse(conf_val)
        scheme = (parsed.scheme or "").lower()

        if scheme and scheme != "file":
            return None

        if scheme == "file":
            if parsed.netloc and parsed.netloc not in {"", "localhost"}:
                return None
            raw_path = unquote(parsed.path or "")
            if not raw_path:
                return None
            base = Path(raw_path)
        else:
            base = Path(conf_val)

        if not base.is_absolute():
            base = Path.cwd() / base
        return base

    def _table_location(self, parts: list[str]) -> Path | None:
        base = self._warehouse_base()
        if base is None or not parts:
            return None

        filtered = [p for p in parts if p]
        if not filtered:
            return None

        catalog_cutoff = 3
        if len(filtered) >= catalog_cutoff and filtered[0].lower() in {"spark_catalog", "spark"}:
            filtered = filtered[1:]

        table = filtered[-1]
        schema_cutoff = 2
        schema = filtered[-2] if len(filtered) >= schema_cutoff else None

        location = base
        if schema:
            location = location / f"{schema}.db"
        return location / table

    def _save_df_as_table(
        self, identifier: str, df: SDF, *, storage: dict[str, Any] | None = None
    ) -> None:
        """
        Save a DataFrame as a (managed or unmanaged) table.

        - If storage["path"] is set -> unmanaged/path-based via storage.spark_write_to_path.
        - Otherwise -> managed table via the configured format handler
          (Delta, Parquet, future Iceberg, ...).
        """
        parts = self._identifier_parts(identifier)
        if not parts:
            raise ValueError(f"Invalid Spark table identifier: {identifier}")

        storage_meta = dict(storage or self._storage_meta(None, identifier) or {})

        path_override = storage_meta.get("path")
        if path_override and not self._format_handler.allows_unmanaged_paths():
            echo_debug(
                f"Ignoring storage.path override for table '{identifier}' because "
                f"format '{self._format_handler.table_format or 'default'}' "
                "requires managed tables."
            )
            path_override = None

        if path_override:
            self._write_to_storage_path(identifier, df, storage_meta)
            return

        table_name = ".".join(parts)
        # Managed tables: delegate to the format handler (Delta, Parquet, Iceberg, ...)
        self._format_handler.save_df_as_table(table_name, df)

        with suppress(Exception):
            self._execute_sql(
                f"ANALYZE TABLE {self._sql_identifier(table_name)} COMPUTE STATISTICS"
            )

    def _create_or_replace_view(self, target_sql: str, select_body: str, node: Node) -> None:
        self._execute_sql(f"CREATE OR REPLACE VIEW {target_sql} AS {select_body}")

    def _create_or_replace_table(self, target_sql: str, select_body: str, node: Node) -> None:
        preview = f"-- target={target_sql}\n{select_body}"
        try:
            df = self._execute_sql(select_body)
            storage_meta = self._storage_meta(node, target_sql)
            self._save_df_as_table(target_sql, df, storage=storage_meta)
        except Exception as exc:
            raise ModelExecutionError(node.name, target_sql, str(exc), sql_snippet=preview) from exc

    def _create_or_replace_view_from_table(
        self, view_name: str, backing_table: str, node: Node
    ) -> None:
        view_sql = self._sql_identifier(view_name)
        backing_sql = self._sql_identifier(backing_table)
        self._execute_sql(f"CREATE OR REPLACE VIEW {view_sql} AS SELECT * FROM {backing_sql}")

    # ---- Meta hook ----
    def on_node_built(self, node: Node, relation: str, fingerprint: str) -> None:
        """After successful materialization, upsert _ff_meta (best-effort)."""
        ensure_meta_table(self)
        upsert_meta(self, node.name, relation, fingerprint, "databricks_spark")

    # ── Incremental API ─────────────────────────────────────────
    def exists_relation(self, relation: str) -> bool:
        """Check whether a table/view exists (optionally qualified with database)."""
        return self._format_handler.relation_exists(relation, database=self.database)

    def create_table_as(self, relation: str, select_sql: str) -> None:
        """CREATE TABLE AS with cleaned SELECT body."""
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        df = self._execute_sql(body)
        self._save_df_as_table(relation, df)

    def full_refresh_table(self, relation: str, select_sql: str) -> None:
        """
        Engine-specific full refresh for incremental fallbacks.
        Important: NO 'REPLACE TABLE' SQL, but DataFrame path + saveAsTable instead.
        """
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        # Delegate to format handler via _save_df_as_table for managed, or storage for unmanaged
        df = self._execute_sql(body)
        self._save_df_as_table(relation, df)

    def incremental_insert(self, relation: str, select_sql: str) -> None:
        """INSERT INTO with cleaned SELECT body (format-aware via handler)."""
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")
        self._format_handler.incremental_insert(relation, body)

    def incremental_merge(self, relation: str, select_sql: str, unique_key: list[str]) -> None:
        body = self._selectable_body(select_sql).strip().rstrip(";\n\t ")

        # First: let the current format handler try to do a native merge.
        # - DeltaFormatHandler -> DeltaTable.merge()
        # - IcebergFormatHandler -> Spark SQL MERGE INTO
        try:
            self._format_handler.incremental_merge(relation, body, unique_key)
            return
        except NotImplementedError:
            # Format handler doesn't support MERGE → fall back to generic Spark strategy.
            pass

        # Fallback for formats without native merge:
        # overwrite = (existing minus keys being updated) UNION (new rows)
        materialized: list[SDF] = []

        def _materialize(df: SDF) -> SDF:
            """
            Ensure the frame is realized independently of the source table so an
            overwrite doesn't conflict with the read path.
            """
            try:
                cp = df.localCheckpoint(eager=True)
                materialized.append(cp)
                return cp
            except Exception:
                cached = df.cache()
                cached.count()
                materialized.append(cached)
                return cached

        try:
            physical = self._physical_identifier(relation)
            existing = _materialize(self.spark.table(physical))
            incoming = _materialize(self.spark.sql(body))

            if unique_key:
                # ensure key columns exist on incoming
                missing = [k for k in unique_key if k not in incoming.columns]
                if missing:
                    raise ModelExecutionError(
                        node_name="__python_incremental__",
                        relation=relation,
                        message=(
                            "incremental_merge fallback: missing key columns on incoming: "
                            f"{missing}"
                        ),
                    )
                key_df = incoming.select(*unique_key).dropDuplicates()
                # left_anti: keep only rows whose keys are NOT in incoming
                kept = existing.join(key_df, on=unique_key, how="left_anti")
                merged = kept.unionByName(incoming, allowMissingColumns=True)
            else:
                # No keys → append & deduplicate
                merged = existing.unionByName(incoming, allowMissingColumns=True).dropDuplicates()

            merged = _materialize(merged)
            # Full overwrite with merged result
            self._save_df_as_table(relation, merged)
        finally:
            for handle in materialized:
                with suppress(Exception):
                    handle.unpersist()

    def alter_table_sync_schema(
        self, relation: str, select_sql: str, *, mode: str = "append_new_columns"
    ) -> None:
        """
        Best-effort additive schema sync:
          - infer select schema via LIMIT 0
          - add missing columns as STRING (safe default)
        """
        if mode not in {"append_new_columns", "sync_all_columns"}:
            return
        # Target schema
        try:
            physical = self._physical_identifier(relation)
            target_df = self.spark.table(physical)
        except Exception:
            return
        existing = {f.name for f in target_df.schema.fields}
        # Output schema from the SELECT
        body = self._first_select_body(select_sql).strip().rstrip(";\n\t ")
        probe = self._execute_sql(f"SELECT * FROM ({body}) q LIMIT 0")
        to_add = [f for f in probe.schema.fields if f.name not in existing]
        if not to_add:
            return

        def _spark_sql_type(dt: DataType) -> str:
            """Simple, portable mapping for Spark SQL types."""
            return (
                getattr(dt, "simpleString", lambda: "string")().upper()
                if hasattr(dt, "simpleString")
                else "STRING"
            )

        cols_sql = ", ".join([f"`{f.name}` {_spark_sql_type(f.dataType)}" for f in to_add])
        table_sql = self._sql_identifier(relation)
        self._execute_sql(f"ALTER TABLE {table_sql} ADD COLUMNS ({cols_sql})")

    # ── Snapshot API ─────────────────────────────────────────────────────

    def run_snapshot_sql(self, node: Node, env: Environment) -> None:
        """
        Snapshot materialization for Spark/Databricks.
        """
        F = get_spark_functions()

        meta = self._validate_snapshot_node(node)
        cfg = resolve_snapshot_config(node, meta)

        strategy = cfg.strategy
        unique_key = cfg.unique_key
        updated_at = cfg.updated_at
        check_cols = cfg.check_cols

        body, rel_name, physical = self._snapshot_sql_body(node, env)

        vf = BaseExecutor.SNAPSHOT_VALID_FROM_COL
        vt = BaseExecutor.SNAPSHOT_VALID_TO_COL
        is_cur = BaseExecutor.SNAPSHOT_IS_CURRENT_COL
        hash_col = BaseExecutor.SNAPSHOT_HASH_COL
        upd_meta = BaseExecutor.SNAPSHOT_UPDATED_AT_COL

        if not self.exists_relation(rel_name):
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

    def _validate_snapshot_node(self, node: Node) -> dict[str, Any]:
        if node.kind != "sql":
            raise TypeError(
                f"Snapshot materialization is only supported for SQL models, "
                f"got kind={node.kind!r} for {node.name}."
            )

        meta = getattr(node, "meta", {}) or {}
        if not self._meta_is_snapshot(meta):
            raise ValueError(f"Node {node.name} is not configured with materialized='snapshot'.")
        return meta

    def _snapshot_sql_body(
        self,
        node: Node,
        env: Environment,
    ) -> tuple[str, str, str]:
        sql_rendered = self.render_sql(
            node,
            env,
            ref_resolver=lambda name: self._resolve_ref(name, env),
            source_resolver=self._resolve_source,
        )
        sql_clean = self._strip_leading_config(sql_rendered).strip()
        body = self._selectable_body(sql_clean).rstrip(" ;\n\t")

        rel_name = relation_for(node.name)
        physical = self._physical_identifier(rel_name)
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
        src_df = self._execute_sql(body)

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

        storage_meta = self._storage_meta(node, rel_name)
        self._save_df_as_table(rel_name, df_snap, storage=storage_meta)

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
        echo_debug(f"[snapshot] incremental run for {rel_name} (strategy={strategy})")

        existing = self.spark.table(physical)
        src_df = self._execute_sql(body)

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

        storage_meta = self._storage_meta(node, rel_name)
        self._save_df_as_table(rel_name, snapshot_df, storage=storage_meta)

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

        if not unique_key:
            return

        vf = BaseExecutor.SNAPSHOT_VALID_FROM_COL

        try:
            physical = self._physical_identifier(relation)
            df = self.spark.table(physical)
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

        # Materialize before overwrite to avoid Spark's
        # [UNSUPPORTED_OVERWRITE.TABLE] "target that is also being read from".
        materialized: list[SDF] = []

        def _materialize(df: SDF) -> SDF:
            try:
                cp = df.localCheckpoint(eager=True)
                materialized.append(cp)
                return cp
            except Exception:
                cached = df.cache()
                cached.count()
                materialized.append(cached)
                return cached

        try:
            out = _materialize(pruned)
            self._save_df_as_table(relation, out)
        finally:
            for handle in materialized:
                with suppress(Exception):
                    handle.unpersist()

    def execute_hook_sql(self, sql: str) -> None:
        """
        Entry point for hook SQL.

        Accepts a string that may contain multiple ';'-separated statements.
        `_RunEngine._execute_hook_sql` has already normalized away semicolons
        in full-line comments, so naive splitting by ';' is acceptable here.
        """
        for stmt in (part.strip() for part in sql.split(";")):
            if not stmt:
                continue
            # Reuse your existing single-statement executor
            self._execute_sql(stmt)

    def load_seed(
        self, table: str, df: pd.DataFrame, schema: str | None = None
    ) -> tuple[bool, str, bool]:
        cleaned_table = self._strip_quotes(table)
        parts = self._identifier_parts(cleaned_table)

        created_schema = False
        if schema and len(parts) == 1:
            schema_part = self._strip_quotes(schema)
            if schema_part:
                # Ensure database exists when a separate schema is provided.
                self._execute_sql(f"CREATE DATABASE IF NOT EXISTS {self._q_ident(schema_part)}")
                created_schema = True
                parts = [schema_part, parts[0]]

        if not parts:
            raise ValueError(f"Invalid Spark table identifier: {table}")

        target_identifier = ".".join(parts)
        target_sql = self._sql_identifier(target_identifier)
        format_handler = getattr(self, "_format_handler", None)

        storage_meta = storage.get_seed_storage(target_identifier)

        sdf = self.spark.createDataFrame(df)

        allows_unmanaged = bool(getattr(format_handler, "allows_unmanaged_paths", lambda: True)())

        if storage_meta.get("path") and allows_unmanaged:
            try:
                self._write_to_storage_path(target_identifier, sdf, storage_meta)
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(f"Spark seed load failed for {target_sql}: {exc}") from exc
        else:
            try:
                self._save_df_as_table(target_identifier, sdf, storage={"path": None})
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(f"Spark seed load failed for {target_sql}: {exc}") from exc

        return True, target_identifier, created_schema

        # ---- Unit-test helpers -------------------------------------------------

    def utest_load_relation_from_rows(self, relation: str, rows: list[dict]) -> None:
        """
        Load rows into a Spark table for unit tests (replace if exists).

        We go via pandas → Spark so schema is inferred from the Python
        data, then delegate to the same table-writing pipeline as the
        normal engine (_save_df_as_table), so table_format / storage
        options / catalogs are all respected.
        """
        pdf = pd.DataFrame(rows)
        # Spark can infer schema from the pandas DataFrame, even for empty
        # frames (it will just create an empty table with no rows).
        sdf = self.spark.createDataFrame(pdf)
        # Use the same path as normal model materialization so that
        # Delta/Iceberg/etc. are handled consistently.
        self._save_df_as_table(relation, sdf)

    def utest_read_relation(self, relation: str) -> pd.DataFrame:
        """
        Read a relation as a pandas DataFrame for unit-test assertions.

        The utest framework always compares on pandas, so we convert from
        Spark DataFrame here.
        """
        physical = self._physical_identifier(relation)
        sdf = self.spark.table(physical)
        return sdf.toPandas()

    def utest_clean_target(self, relation: str) -> None:
        """
        For unit tests: drop any view or table with this name.

        We:
          - try DROP VIEW IF EXISTS ...
          - try DROP TABLE IF EXISTS ...
        and ignore type-mismatch errors, so it doesn't matter whether a
        table or a view currently exists under that name.
        """
        ident = self._sql_identifier(relation)

        # Drop view first; ignore errors if it's actually a table or missing.
        with suppress(Exception):
            self._execute_sql(f"DROP VIEW IF EXISTS {ident}")

        # Then drop table; ignore errors if it's actually a view or missing.
        with suppress(Exception):
            self._execute_sql(f"DROP TABLE IF EXISTS {ident}")

    def _introspect_columns_metadata(
        self,
        table: str,
        column: str | None = None,
    ) -> list[tuple[str, str]]:
        """
        Internal helper: return [(column_name, spark_sql_type), ...] for a Spark table.

        - Uses Spark's DataFrame schema (no information_schema dependency).
        - Works with db.table identifiers via _physical_identifier().
        - Optionally restricts to a single column (case-insensitive).
        """
        physical = self._physical_identifier(table)
        df = self.spark.table(physical)

        want = column.lower() if column is not None else None

        out: list[tuple[str, str]] = []
        for field in df.schema.fields:
            name = field.name
            if want is not None and name.lower() != want:
                continue

            dt = field.dataType
            try:
                # e.g. "bigint", "string", "timestamp", "decimal(10,2)", "array<string>", ...
                typ = dt.simpleString()
            except Exception:
                typ = str(dt)

            # Keep consistent with your existing introspect_column_physical_type()
            out.append((str(name), str(typ).upper()))

        return out

    def introspect_column_physical_type(self, table: str, column: str) -> str | None:
        """
        Spark: return Spark SQL type (simpleString) for one column, uppercased.
        """
        rows = self._introspect_columns_metadata(table, column=column)
        return rows[0][1] if rows else None

    def introspect_table_physical_schema(self, table: str) -> dict[str, str]:
        """
        Spark: return {lower(column_name): spark_sql_type} for all columns of `table`.
        """
        rows = self._introspect_columns_metadata(table, column=None)
        # Lower keys to match runtime verifier behavior (case-insensitive compare)
        return {name.lower(): typ for (name, typ) in rows}
