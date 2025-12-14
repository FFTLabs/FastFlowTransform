# fastflowtransform/executors/base.py
from __future__ import annotations

import contextvars
import importlib
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar, cast

import pandas as pd
from jinja2 import Environment
from pandas import DataFrame as _PDDataFrame

from fastflowtransform import incremental as _ff_incremental
from fastflowtransform.api import context as _http_ctx
from fastflowtransform.config.contracts import ContractsFileModel, ProjectContractsModel
from fastflowtransform.config.sources import resolve_source_entry
from fastflowtransform.core import REGISTRY, Node, relation_for
from fastflowtransform.errors import ModelExecutionError
from fastflowtransform.executors._query_stats_adapter import JobStatsAdapter
from fastflowtransform.executors.budget import BudgetGuard
from fastflowtransform.executors.query_stats import QueryStats
from fastflowtransform.incremental import _normalize_unique_key
from fastflowtransform.logging import echo, echo_debug
from fastflowtransform.validation import validate_required_columns


def _python_incremental_merge_default(
    df_old: _PDDataFrame,
    df_new: _PDDataFrame,
    unique_key: list[str],
    update_cols: list[str],
) -> _PDDataFrame:
    """
    Default merge for Python-Incremental:
      - unique_key: key columns
      - update_cols: columns from which to determine delta
    Strategy:
      - df_old + df_new concat,
      - sorted by unique_key + update_cols
      - Deduplicate unique_key (keep='last').
    """
    if df_old is None or df_old.empty:
        return df_new.copy()
    if df_new is None or df_new.empty:
        return df_old.copy()

    if not unique_key:
        combined = pd.concat([df_old, df_new], ignore_index=True)
        combined = combined.drop_duplicates()
        return combined

    combined = pd.concat([df_old, df_new], ignore_index=True)

    # Nur Update-Spalten verwenden, die es wirklich gibt
    update_cols = [c for c in update_cols if c in combined.columns]

    sort_cols = unique_key + update_cols if update_cols else unique_key
    combined = combined.sort_values(sort_cols)
    combined = combined.drop_duplicates(subset=unique_key, keep="last")
    return combined


def _load_callable(path: str) -> Callable[..., Any]:
    """
    Import a callable from 'pkg.mod:func' or 'pkg.mod.func'.
    """
    text = path.strip()
    if ":" in text:
        mod_name, func_name = text.split(":", 1)
    elif "." in text:
        mod_name, func_name = text.rsplit(".", 1)
    else:
        raise ValueError(
            f"Invalid callable path {path!r}; expected 'module:func' or 'module.func'."
        )

    mod = importlib.import_module(mod_name)
    fn = getattr(mod, func_name, None)
    if not callable(fn):
        raise ValueError(f"{path!r} is not a callable")
    return fn


def _scalar(executor: BaseExecutor, sql: Any) -> Any:
    """Execute SQL and return the first column of the first row (or None)."""
    row = executor.execute_test_sql(sql).fetchone()
    return None if row is None else row[0]


# Frame type (pandas.DataFrame, pyspark.sql.DataFrame, snowflake.snowpark.DataFrame, ...)
TFrame = TypeVar("TFrame")


class _ThisProxy:
    """
    Jinja compatible proxy for {{ this }}:
    - Use as string ({{ this }}) -> physical relation name.
    - attributes available ({{ this.name }}, {{ this.materialized }}, ...)
    """

    def __init__(self, relation: str, materialized: str, schema: str | None, database: str | None):
        self.name = relation  # Back-compat: {{ this.name }}
        self.relation = relation  # alias, if someone uses {{ this.relation }}
        self.materialized = materialized
        self.schema = schema
        self.database = database

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"_ThisProxy(name={self.name!r})"


class BaseExecutor[TFrame](ABC):
    """
    Shared workflow for SQL rendering and Python models.
    I/O is frame-agnostic; subclasses provide frame-specific hooks:
      - _read_relation
      - _materialize_relation
      - _validate_required
      - _columns_of
      - _is_frame
      - (optional) _frame_name
    """

    # Standard meta columns used by snapshot materialization.
    SNAPSHOT_VALID_FROM_COL = "_ff_valid_from"
    SNAPSHOT_VALID_TO_COL = "_ff_valid_to"
    SNAPSHOT_IS_CURRENT_COL = "_ff_is_current"
    SNAPSHOT_HASH_COL = "_ff_snapshot_hash"
    SNAPSHOT_UPDATED_AT_COL = "_ff_updated_at"

    _ff_contracts: Mapping[str, ContractsFileModel] | None = None
    _ff_project_contracts: ProjectContractsModel | None = None

    def configure_contracts(
        self,
        contracts: Mapping[str, ContractsFileModel] | None,
        project_contracts: ProjectContractsModel | None,
    ) -> None:
        """
        Inject parsed contracts into this executor instance.
        The run engine should call this once at startup.
        """
        self._ff_contracts = contracts or {}
        self._ff_project_contracts = project_contracts

    # ---------- SQL ----------
    def render_sql(
        self,
        node: Node,
        env: Environment,
        ref_resolver: Callable[[str], str] | None = None,
        source_resolver: Callable[[str, str], str] | None = None,
    ) -> str:
        # ---- thread-/task-local config()-hook
        _RENDER_CFG: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
            "_RENDER_CFG", default=None
        )

        def get_render_cfg() -> dict[str, Any]:
            cfg = _RENDER_CFG.get()
            if cfg is None:
                cfg = {}
                _RENDER_CFG.set(cfg)
            return cfg

        def _config_hook(**kwargs: Any) -> str:
            cfg = get_render_cfg()
            cfg.update(kwargs)
            return ""

        if "config" not in env.globals:
            env.globals["config"] = _config_hook

        # ---- var() builtin: CLI overrides > project.yml vars > default
        if "var" not in env.globals:

            def _var(key: str, default: Any = None) -> Any:
                cli = getattr(REGISTRY, "cli_vars", {}) or {}
                if key in cli:
                    return cli[key]
                proj = getattr(REGISTRY, "project_vars", {}) or {}
                if key in proj:
                    return proj[key]
                return default

            env.globals["var"] = _var

        # ---- is_incremental() builtin
        # True iff meta marks the model as incremental AND the target relation exists.
        if "is_incremental" not in env.globals:

            def _is_incremental() -> bool:
                try:
                    meta = getattr(node, "meta", {}) or {}
                    if not self._meta_is_incremental(meta):
                        return False
                    rel = relation_for(node.name)
                    return bool(self.exists_relation(rel))
                except Exception:
                    # Be conservative: if anything is off, treat as non-incremental.
                    return False

            env.globals["is_incremental"] = _is_incremental

        raw = Path(node.path).read_text(encoding="utf-8")
        tmpl = env.from_string(raw)

        def _default_ref(name: str) -> str:
            return relation_for(name)

        def _default_source(source_name: str, table_name: str) -> str:
            group = REGISTRY.sources.get(source_name)
            if not group:
                raise KeyError(f"Unknown source {source_name}.{table_name}")
            entry = group.get(table_name)
            if not entry:
                raise KeyError(f"Unknown source {source_name}.{table_name}")
            cfg = resolve_source_entry(entry, self.engine_name, default_identifier=table_name)
            if cfg.get("location"):
                raise KeyError(
                    "Path-based sources require executor context; "
                    "default resolver cannot handle them."
                )
            identifier = cfg.get("identifier")
            if not identifier:
                raise KeyError(f"Source {source_name}.{table_name} missing identifier")
            return identifier

        _RENDER_CFG.set({})

        # expose 'this' to the template: Proxy-Objekt, das wie String wirkt
        this_obj = _ThisProxy(
            self._this_identifier(node),
            (getattr(node, "meta", {}) or {}).get("materialized", "table"),
            getattr(self, "schema", None) or getattr(self, "dataset", None),
            getattr(self, "database", None) or getattr(self, "project", None),
        )

        sql = tmpl.render(
            ref=ref_resolver or _default_ref,
            source=source_resolver or _default_source,
            this=this_obj,
        )

        cfg = _RENDER_CFG.get()
        if cfg:
            for k, v in cfg.items():
                node.meta.setdefault(k, v)
        return sql

    def run_sql(self, node: Node, env: Environment) -> None:
        """
        Orchestrate SQL models:
          1) Render Jinja (ref/source/this) and strip leading {{ config(...) }}.
          2) If the SQL is full DDL (CREATE …), execute it verbatim (passthrough).
          3) Otherwise, normalize to CREATE OR REPLACE {TABLE|VIEW} AS <body>.
             The body is CTE-aware (keeps WITH … SELECT … intact).
        On failure, raise ModelExecutionError with a helpful snippet.
        """
        meta = getattr(node, "meta", {}) or {}
        if self._meta_is_incremental(meta):
            # Delegates to incremental engine: render, schema sync, merge/insert, etc.
            return _ff_incremental.run_or_dispatch(self, node, env)

        if self._meta_is_snapshot(meta):
            # Snapshots are executed via the dedicated CLI: `fft snapshot run`.
            raise ModelExecutionError(
                node_name=node.name,
                relation=relation_for(node.name),
                message=(
                    "Snapshot models cannot be executed via 'fft run'. "
                    "Use 'fft snapshot run' instead."
                ),
                sql_snippet="",
            )

        sql_rendered = self.render_sql(
            node,
            env,
            ref_resolver=lambda name: self._resolve_ref(name, env),
            source_resolver=self._resolve_source,
        )
        sql = self._strip_leading_config(sql_rendered).strip()

        materialization = (node.meta or {}).get("materialized", "table")
        if materialization == "ephemeral":
            return

        # 1) Direct DDL passthrough (CREATE [OR REPLACE] {TABLE|VIEW} …)
        if self._looks_like_direct_ddl(sql):
            try:
                self._execute_sql_direct(sql, node)
                return
            except NotImplementedError:
                # Engine doesn't implement direct DDL → fall back to normalized materialization.
                pass
            except Exception as e:
                raise ModelExecutionError(
                    node_name=node.name,
                    relation=relation_for(node.name),
                    message=str(e),
                    sql_snippet=sql,
                ) from e

        # 2) Normalized materialization path (CTE-safe body)
        body = self._selectable_body(sql).rstrip(" ;\n\t")
        target_sql = self._format_relation_for_ref(node.name)

        # Centralized SQL preview logging (applies to ALL engines)
        preview = (
            f"=== MATERIALIZE ===\n"
            f"-- model: {node.name}\n"
            f"-- materialized: {materialization}\n"
            f"-- target: {target_sql}\n"
            f"{body}\n"
        )
        echo_debug(preview)

        try:
            runtime = getattr(self, "runtime_contracts", None)
            # contracts only for TABLE materialization for now
            if runtime is not None and materialization == "table":
                contracts = getattr(self, "_ff_contracts", {}) or {}
                project_contracts = getattr(self, "_ff_project_contracts", None)

                # keying: prefer the logical table name (contracts.table),
                # but node.name or relation_for(node.name) is usually what you want.
                logical_name = relation_for(node.name)
                contract = contracts.get(logical_name) or contracts.get(node.name)

                ctx = runtime.build_context(
                    node=node,
                    relation=logical_name,
                    physical_table=target_sql,
                    contract=contract,
                    project_contracts=project_contracts,
                    is_incremental=self._meta_is_incremental(meta),
                )
                # Engine-specific enforcement (verify/cast/off)
                runtime.apply_sql_contracts(ctx=ctx, select_body=body)
            else:
                # Old behavior
                self._apply_sql_materialization(node, target_sql, body, materialization)
        except Exception as e:
            preview = f"-- materialized={materialization}\n-- target={target_sql}\n{body}"
            raise ModelExecutionError(
                node_name=node.name,
                relation=relation_for(node.name),
                message=str(e),
                sql_snippet=preview,
            ) from e

    def run_snapshot_sql(self, node: Node, env: Environment) -> None:
        """
        Execute a SQL model materialized as 'snapshot'.

        Default implementation: engines must override this or snapshots
        will fail with a clear error.
        """
        raise NotImplementedError(
            f"Snapshot materialization is not implemented for engine '{self.engine_name}'."
        )

    # --- Helpers for materialization & ephemeral inlining (instance methods) ---
    def _first_select_body(self, sql: str) -> str:
        """
        Fallback: extract the substring starting at the first SELECT token.
        If no SELECT is found, return the original string unchanged.
        Prefer using _selectable_body() which is CTE-aware.
        """
        m = re.search(r"\bselect\b", sql, flags=re.I | re.S)
        return sql[m.start() :] if m else sql

    def _strip_leading_config(self, sql: str) -> str:
        """
        Remove a leading Jinja {{ config(...) }} so the engine receives clean SQL.
        """
        return re.sub(
            r"^\s*\{\{\s*config\s*\(.*?\)\s*\}\}\s*",
            "",
            sql,
            flags=re.I | re.S,
        )

    def _strip_leading_sql_comments(self, sql: str) -> tuple[str, int]:
        """
        Remove *only* leading SQL comments and blank lines, return (trimmed_sql, start_idx).

        Supports:
          -- single line comments
          /* block comments */
        """
        # Match chain of: whitespace, comment, whitespace, comment, ...
        # Using DOTALL so block comments spanning lines are handled.
        pat = re.compile(
            r"""^\s*(?:
                                --[^\n]*\n        # line comment
                              | /\*.*?\*/\s*      # block comment
                             )*""",
            re.VERBOSE | re.DOTALL,
        )
        m = pat.match(sql)
        start = m.end() if m else 0
        return sql[start:], start

    def _selectable_body(self, sql: str) -> str:
        """
        Normalize a SELECT/CTE body:

        - Strip leading SQL comments/blank lines.
        - Find the first WITH or SELECT keyword (as a word) anywhere in the statement.
        - Return from that keyword onward, stripping trailing semicolons/whitespace.

        This works for:
        * plain SELECT
        * WITH ... (CTEs)
        * CREATE TABLE/VIEW ... AS WITH ...
        * CREATE TABLE/VIEW ... AS SELECT ...
        * INSERT INTO ... SELECT ...
        """
        s0 = sql or ""

        # Strip leading comments; s starts at 'offset' in the original string.
        s, offset = self._strip_leading_sql_comments(s0)
        s_ws = s.lstrip()

        # Find first WITH or SELECT as a whole word (case-insensitive)
        m = re.search(r"\b(with|select)\b", s_ws, flags=re.IGNORECASE)
        if not m:
            # No obvious SELECT/CTE - just return the statement minus trailing semicolons.
            return s0.strip().rstrip(";\n\t ")

        # m.start() is index within s_ws. Need to map back into the original sql.
        leading_ws_len = len(s) - len(s_ws)  # spaces we lstripped
        start_in_s = leading_ws_len + m.start()
        start_in_sql = offset + start_in_s

        body = s0[start_in_sql:]
        # Strip trailing semicolons and whitespace
        return body.strip().rstrip(";\n\t ")

    def _looks_like_direct_ddl(self, sql: str) -> bool:
        """
        True if the rendered SQL starts with CREATE (TABLE|VIEW) so it should be
        executed verbatim as a user-provided DDL statement.
        """
        head = sql.lstrip().lower()
        return (
            head.startswith("create table")
            or head.startswith("create view")
            or head.startswith("create or replace")
        )

    def _execute_sql_direct(self, sql: str, node: Node) -> None:
        """
        Execute a full CREATE … statement as-is. Default: use `self.con.execute(sql)`.
        Engines can override this for custom dispatch. If not available, raise
        NotImplementedError so the caller can fall back to normalized materialization.
        """
        con = getattr(self, "con", None)
        if con is None or not hasattr(con, "execute"):
            raise NotImplementedError("Direct DDL execution is not implemented for this executor.")
        con.execute(sql)

    def _execute_sql(
        self, sql: str, *args: Any, **kwargs: Any
    ) -> Any:  # pragma: no cover - abstract
        """
        Engine-specific SQL execution hook used by shared helpers (snapshots, pruning, etc.).
        Concrete executors override this with their own signatures and semantics.
        """
        raise NotImplementedError

    def execute_test_sql(self, stmt: Any) -> Any:  # pragma: no cover - abstract
        """
        Execute a lightweight SQL statement for DQ tests.

        Implementations should accept:
          - str
          - (str, params dict)
          - ClauseElement (optional, where supported)
          - Sequence of the above (executed sequentially; return last result)
        and return an object supporting .fetchone() / .fetchall().
        """
        raise NotImplementedError

    def compute_freshness_delay_minutes(self, table: str, ts_col: str) -> tuple[float | None, str]:
        """
        Compute delay in minutes between now and max(ts_col) for a relation.

        Returns (delay_minutes, sql_used).
        Default implementation is not provided; executors implement engine-specific logic.
        """
        raise NotImplementedError

    def _render_ephemeral_sql(self, name: str, env: Environment) -> str:
        """
        Render the SQL for an 'ephemeral' model and return it as a parenthesized
        subquery. This is CTE-safe: we keep the full WITH…SELECT… statement and
        only strip the leading {{ config(...) }} and trailing semicolons.
        """
        node = REGISTRY.get_node(name) if hasattr(REGISTRY, "get_node") else REGISTRY.nodes[name]

        raw = Path(node.path).read_text(encoding="utf-8")
        tmpl = env.from_string(raw)

        sql = tmpl.render(
            ref=lambda n: self._resolve_ref(n, env),
            source=self._resolve_source,
            this=_ThisProxy(
                self._this_identifier(node),
                (getattr(node, "meta", {}) or {}).get("materialized", "table"),
                getattr(self, "schema", None) or getattr(self, "dataset", None),
                getattr(self, "database", None) or getattr(self, "project", None),
            ),
        )
        # Remove a leading config block and keep the full, CTE-capable statement
        sql = self._strip_leading_config(sql).strip()
        body = self._selectable_body(sql).rstrip(" ;\n\t")
        return f"(\n{body}\n)"

    # ---------- Query stats (per-node, aggregated across queries) ----------

    def _record_query_stats(self, stats: QueryStats) -> None:
        """
        Append per-query stats to an internal buffer.

        Executors call this from their engine-specific recording logic.
        The run engine can later drain this buffer per node.
        """
        buf = getattr(self, "_ff_query_stats_buffer", None)
        if buf is None:
            buf = []
            self._ff_query_stats_buffer = buf
        buf.append(stats)

    def _drain_query_stats(self) -> list[QueryStats]:
        """
        Drain and return the buffered stats, resetting the buffer.

        Used by the run engine around per-node execution.
        """
        buf = getattr(self, "_ff_query_stats_buffer", None)
        if not buf:
            self._ff_query_stats_buffer = []
            return []
        self._ff_query_stats_buffer = []
        return list(buf)

    def _record_query_job_stats(self, job: Any) -> None:
        """
        Best-effort extraction of stats from a 'job-like' object.

        This is intentionally generic; engines that return job handles
        (BigQuery, Snowflake, Spark) can pass them here. Engines can
        override this if they want more precise logic.
        """
        adapter = JobStatsAdapter()
        self._record_query_stats(adapter.collect(job))

    def configure_query_budget_limit(self, limit: int | None) -> None:
        """
        Inject a configured per-query byte limit (e.g. from budgets.yml).
        """
        if limit is None:
            self._ff_configured_query_limit = None
            return
        try:
            iv = int(limit)
        except Exception:
            self._ff_configured_query_limit = None
            return
        self._ff_configured_query_limit = iv if iv > 0 else None

    def _configured_query_limit(self) -> int | None:
        val = getattr(self, "_ff_configured_query_limit", None)
        if val is None:
            return None
        try:
            iv = int(val)
        except Exception:
            return None
        return iv if iv > 0 else None

    def _set_query_budget_estimate(self, estimate: int | None) -> None:
        self._ff_last_query_budget_estimate = estimate

    def _consume_query_budget_estimate(self) -> int | None:
        estimate = getattr(self, "_ff_last_query_budget_estimate", None)
        self._ff_last_query_budget_estimate = None
        return estimate

    def _apply_budget_guard(self, guard: BudgetGuard | None, sql: str) -> int | None:
        if guard is None:
            self._set_query_budget_estimate(None)
            self._ff_budget_guard_active = False
            return None
        limit, source = guard.resolve_limit(self._configured_query_limit())
        if not limit:
            self._set_query_budget_estimate(None)
            self._ff_budget_guard_active = False
            return None
        self._ff_budget_guard_active = True
        estimate = guard.enforce(sql, self, limit=limit, source=source)
        self._set_query_budget_estimate(estimate)
        return estimate

    def _is_budget_guard_active(self) -> bool:
        return bool(getattr(self, "_ff_budget_guard_active", False))

    # ---------- Per-node stats API (used by run engine) ----------

    def reset_node_stats(self) -> None:
        """
        Reset per-node statistics buffer.

        The run engine calls this before executing a model so that all
        stats recorded via `_record_query_stats(...)` belong to that node.
        """
        # just clear the buffer; next recording will re-create it
        self._ff_query_stats_buffer = []

    def get_node_stats(self) -> dict[str, int]:
        """
        Aggregate buffered QueryStats into a simple dict:

            {
              "bytes_scanned": <sum>,
              "rows": <sum>,
              "query_duration_ms": <sum>,
            }

        Called by the run engine after a node finishes.
        """
        stats_list = self._drain_query_stats()
        if not stats_list:
            return {}

        total_bytes = 0
        total_rows = 0
        total_duration = 0

        for s in stats_list:
            if s.bytes_processed is not None:
                total_bytes += int(s.bytes_processed)
            if s.rows is not None:
                total_rows += int(s.rows)
            if s.duration_ms is not None:
                total_duration += int(s.duration_ms)

        return {
            "bytes_scanned": total_bytes,
            "rows": total_rows,
            "query_duration_ms": total_duration,
        }

    # ---------- Python models ----------
    def run_python(self, node: Node) -> None:
        """Execute the Python model for a given node and materialize its result."""
        func = REGISTRY.py_funcs[node.name]
        deps = REGISTRY.nodes[node.name].deps or []

        self._reset_http_ctx(node)

        args, argmap = self._build_python_inputs(node, deps)
        requires = REGISTRY.py_requires.get(node.name, {})
        if deps:
            # Required-columns check works against the mapping
            self._validate_required(node.name, argmap, requires)

        # out = self._execute_python_func(func, arg, node)
        out = self._execute_python_func(func, args, node)

        target = relation_for(node.name)
        meta = getattr(node, "meta", {}) or {}
        mat = self._resolve_materialization_strategy(meta)

        # ---------- Runtime contracts for Python models ----------
        runtime = getattr(self, "runtime_contracts", None)
        ctx = None
        took_over = False

        if runtime is not None:
            contracts = getattr(self, "_ff_contracts", {}) or {}
            project_contracts = getattr(self, "_ff_project_contracts", None)

            logical = target  # usually relation_for(node.name)
            contract = contracts.get(logical) or contracts.get(node.name)

            if contract is not None or project_contracts is not None:
                physical_table = self._format_relation_for_ref(node.name)
                ctx = runtime.build_context(
                    node=node,
                    relation=logical,
                    physical_table=physical_table,
                    contract=contract,
                    project_contracts=project_contracts,
                    is_incremental=(mat == "incremental"),
                )

                # Optional pre-coercion (default is no-op).
                if hasattr(runtime, "coerce_frame_schema"):
                    out = runtime.coerce_frame_schema(out, ctx)

                # Allow engine-specific runtime to take over Python materialization
                if mat == "table" and hasattr(runtime, "materialize_python"):
                    took_over = bool(runtime.materialize_python(ctx=ctx, df=out))

        # ---------- Materialization ----------
        if not took_over:
            if mat == "incremental":
                self._materialize_incremental(target, out, node, meta)
            elif mat == "view":
                self._materialize_view(target, out, node)
            else:
                self._materialize_relation(target, out, node)

        if ctx is not None and runtime is not None:
            runtime.verify_after_materialization(ctx=ctx)

        self._snapshot_http_ctx(node)

    # ----------------- helpers -----------------

    def _reset_http_ctx(self, node: Node) -> None:
        """Reset HTTP context for the given node if available."""
        if _http_ctx is None:
            return
        with suppress(Exception):
            _http_ctx.reset_for_node(node.name)

    def _build_python_inputs(
        self, node: Node, deps: list[str]
    ) -> tuple[list[TFrame], dict[str, TFrame]]:
        """
        Load input frames for the Python model.
        Returns:
            - args:  positional argument list in the order of `deps`
            - argmap: mapping {relation_name -> frame} for validation
        """
        args: list[TFrame] = []
        argmap: dict[str, TFrame] = {}
        for dep in deps or []:
            rel = relation_for(dep)
            df = self._read_relation(rel, node, deps)
            args.append(df)
            argmap[rel] = df
        return args, argmap

    def _execute_python_func(
        self,
        func: Callable[[Any], Any],
        args: Any,
        node: Node,
    ) -> TFrame:
        """Execute the Python function and ensure it returns a valid frame."""
        # raw = func(arg)
        raw = func(*args)
        if not self._is_frame(raw):
            raise TypeError(
                f"Python model '{node.name}' must return {self._frame_name()} DataFrame."
            )
        return cast(TFrame, raw)

    def _resolve_materialization_strategy(self, meta: dict[str, Any]) -> str:
        """
        Determine how the Python model result should be materialized.

        Returns "table" by default, but respects:
            - meta["materialized"]
            - meta["incremental"] (bool or dict) as a shortcut for incremental
              materialization.
        """
        if self._meta_is_incremental(meta):
            return "incremental"
        mat = meta.get("materialized") or "table"
        return str(mat)

    def _materialize_view(self, target: str, out: TFrame, node: Node) -> None:
        """Materialize a Python model as a backing table and expose it as a view."""
        backing = self._py_view_backing_name(target)
        self._materialize_relation(backing, out, node)
        self._create_or_replace_view_from_table(target, backing, node)

    def _materialize_incremental(
        self,
        target: str,
        out: TFrame,
        node: Node,
        meta: dict[str, Any],
    ) -> None:
        """Materialize a Python model using incremental semantics."""
        if not self._relation_exists_safely(target):
            # First run -> write full table
            self._materialize_relation(target, out, node)
            return

        if not isinstance(out, _PDDataFrame):
            # Non-pandas frames: fall back to full refresh
            self._materialize_relation(target, out, node)
            return

        df_old = self._safe_read_existing_incremental(target, node)
        if df_old is None or not isinstance(df_old, _PDDataFrame):
            # Fallback: full-refresh
            self._materialize_relation(target, out, node)
            return

        merged = self._merge_incremental_frames(df_old, out, meta, node)
        self._materialize_relation(target, merged, node)

    def _relation_exists_safely(self, target: str) -> bool:
        """Check whether the target relation exists, swallowing backend errors."""
        try:
            return bool(self.exists_relation(target))
        except Exception:
            return False

    def _safe_read_existing_incremental(self, target: str, node: Node) -> Any:
        """Try to read an existing incremental relation, swallowing backend errors."""
        try:
            return self._read_relation(target, node, deps=[])
        except Exception:
            return None

    def _merge_incremental_frames(
        self,
        df_old: _PDDataFrame,
        df_new: _PDDataFrame,
        meta: dict[str, Any],
        node: Node,
    ) -> TFrame:
        """
        Merge existing and new frames using a custom delta function if configured,
        otherwise fall back to the default incremental merge.
        """
        delta_fn_ref = meta.get("delta_python")

        if isinstance(delta_fn_ref, str) and delta_fn_ref.strip():
            delta_fn = _load_callable(delta_fn_ref)
            merged = delta_fn(
                existing=df_old,
                new=df_new,
                node=node,
                executor=self,
                meta=meta,
            )
            if not self._is_frame(merged):
                raise TypeError(
                    f"delta_python '{delta_fn_ref}' must return a DataFrame {self._frame_name()}."
                )
            return cast(TFrame, merged)

        unique_key = _normalize_unique_key(meta.get("unique_key") or meta.get("primary_key"))
        update_cols = _normalize_unique_key(
            meta.get("delta_columns")
            or meta.get("updated_at_columns")
            or meta.get("updated_at")
            or meta.get("timestamp_columns")
        )
        merged_default = _python_incremental_merge_default(df_old, df_new, unique_key, update_cols)
        return cast(TFrame, merged_default)

    def _snapshot_http_ctx(self, node: Node) -> None:
        """Store an HTTP snapshot into node.meta if HTTP context is available."""
        if _http_ctx is None:
            return

        try:
            snap = _http_ctx.snapshot()
        except Exception:
            return

        with suppress(Exception):
            if not isinstance(node.meta, dict) or not node.meta:
                node.meta = {}
            node.meta["_http_snapshot"] = snap

        requests = int(snap.get("requests") or 0)
        if requests <= 0:
            return
        cache_hits = int(snap.get("cache_hits") or 0)
        bytes_read = int(snap.get("bytes") or 0)
        offline = bool(snap.get("used_offline"))
        echo(
            f"HTTP stats for {node.name}: requests={requests} cache_hits={cache_hits} "
            f"bytes={bytes_read} offline={offline}"
        )
        if offline:
            echo(f"Node {node.name} served responses from offline cache")

    # -------- Python model view helpers (shared) --------
    def _py_view_backing_name(self, relation: str) -> str:
        """
        Backing table name for Python models materialized as views.
        Must be a valid identifier for the target engine.
        """
        return f"__ff_py_{relation}"

    @abstractmethod
    def _create_or_replace_view_from_table(
        self, view_name: str, backing_table: str, node: Node
    ) -> None:
        """
        Create (or replace) a VIEW named `view_name` that selects from `backing_table`.
        Implement engine-specific DDL here.
        """
        ...

    # ---------- SQL hook contracts ----------

    def execute_hook_sql(self, sql: str) -> None:
        """
        Execute a SQL hook block (pre-/post-run, on-run-start, on-run-end, etc.).
        """
        raise NotImplementedError(f"SQL hooks are not implemented for engine '{self.engine_name}'.")

    # ---------- SQL hook contracts ----------
    @abstractmethod
    def _format_relation_for_ref(self, name: str) -> str:
        """
        Return the engine-specific SQL identifier used to reference a model's materialised relation.
        """
        ...

    @abstractmethod
    def _format_source_reference(
        self, cfg: dict[str, Any], source_name: str, table_name: str
    ) -> str:
        """
        Return the SQL identifier used to reference a configured source.
        """
        ...

    def _apply_sql_materialization(
        self, node: Node, target_sql: str, select_body: str, materialization: str
    ) -> None:
        """
        Materialise the rendered SELECT according to the requested kind (`table`, `view`, ...).
        The default implementation delegates to `create_or_replace_*` hooks.
        """
        if materialization == "view":
            self._create_or_replace_view(target_sql, select_body, node)
        else:
            self._create_or_replace_table(target_sql, select_body, node)

    @abstractmethod
    def _create_or_replace_view(self, target_sql: str, select_body: str, node: Node) -> None:
        """
        Engine-specific implementation for CREATE OR REPLACE VIEW ... AS <body>.
        """
        ...

    @abstractmethod
    def _create_or_replace_table(self, target_sql: str, select_body: str, node: Node) -> None:
        """
        Engine-specific implementation for CREATE OR REPLACE TABLE ... AS <body>.
        """
        ...

    # ---------- Resolution helpers ----------
    def _this_identifier(self, node: Node) -> str:
        """
        Physical identifier backing {{ this }} in SQL templates.

        Engines may override to inject catalog/schema qualification.
        """
        return relation_for(node.name)

    def _format_test_table(self, table: str | None) -> str | None:
        """
        Format table identifiers for data-quality tests (fft test).

        Default behavior normalizes '.ff' suffixes only; engines can override
        to add catalog/schema qualification.
        """
        if not isinstance(table, str):
            return table
        stripped = table.strip()
        if not stripped:
            return stripped
        return relation_for(stripped) if stripped.endswith(".ff") else stripped

    def _resolve_ref(self, name: str, env: Environment) -> str:
        dep = REGISTRY.get_node(name) if hasattr(REGISTRY, "get_node") else REGISTRY.nodes[name]
        if dep.meta.get("materialized") == "ephemeral":
            return self._render_ephemeral_sql(dep.name, env)
        return self._format_relation_for_ref(name)

    def _resolve_source(self, source_name: str, table_name: str) -> str:
        group = REGISTRY.sources.get(source_name)
        if not group:
            known = ", ".join(sorted(REGISTRY.sources.keys())) or "<none>"
            raise KeyError(f"Unknown source '{source_name}'. Known sources: {known}")

        entry = group.get(table_name)
        if not entry:
            known_tables = ", ".join(sorted(group.keys())) or "<none>"
            raise KeyError(
                f"Unknown source table '{source_name}.{table_name}'. Known tables: {known_tables}"
            )

        engine_key = self.engine_name
        try:
            cfg = resolve_source_entry(entry, engine_key, default_identifier=table_name)
        except KeyError as exc:
            raise KeyError(
                f"Source {source_name}.{table_name} missing "
                f"identifier/location for engine '{engine_key}'"
            ) from exc

        cfg = dict(cfg)
        cfg.setdefault("options", {})
        return self._format_source_reference(cfg, source_name, table_name)

    # ---------- Abstract Frame-Hooks ----------
    @abstractmethod
    def _read_relation(self, relation: str, node: Node, deps: Iterable[str]) -> TFrame: ...

    @abstractmethod
    def _materialize_relation(self, relation: str, df: TFrame, node: Node) -> None: ...

    def _validate_required(
        self, node_name: str, inputs: Any, requires: dict[str, set[str]]
    ) -> None:
        """
        inputs: either TFrame (single dependency) or dict[str, TFrame] (multiple dependencies)
        raises: ValueError with a clear explanation when columns/keys are missing
        """
        if not requires:
            return

        validate_required_columns(node_name, inputs, requires)

    def _columns_of(self, frame: TFrame) -> list[str]:
        """List of columns for debug logging."""
        columns = getattr(frame, "columns", None)
        if columns is not None:
            return [str(c) for c in list(columns)]
        raise NotImplementedError("_columns_of needs to be implemented for non-pandas frame types")

    def _is_frame(self, obj: Any) -> bool:
        """Is 'obj' a valid frame for this executor?"""
        return isinstance(obj, _PDDataFrame)

    def _frame_name(self) -> str:
        """Only used when formatting error messages (default)."""
        return "a"

    # ---------- Build meta hook ----------
    def on_node_built(self, node: Node, relation: str, fingerprint: str) -> None:
        """
        Hook invoked after a node has been successfully materialized.
        Engines should override this to write/update the meta table (e.g. _ff_meta).

        Default: no-op.
        """
        return

    # ── Incremental API ───────────────────────────────────────────────
    def exists_relation(self, relation: str) -> bool:  # pragma: no cover - abstract
        """Returns True if physical relation exists (table/view)."""
        raise NotImplementedError

    def create_table_as(self, relation: str, select_sql: str) -> None:  # pragma: no cover
        """CREATE TABLE AS SELECT …"""
        raise NotImplementedError

    def incremental_insert(self, relation: str, select_sql: str) -> None:  # pragma: no cover
        """INSERT-only (Append)."""
        raise NotImplementedError

    def incremental_merge(
        self, relation: str, select_sql: str, unique_key: list[str]
    ) -> None:  # pragma: no cover
        """Best-effort UPSERT; Default fallback via staging delete+insert."""
        raise NotImplementedError

    def alter_table_sync_schema(
        self, relation: str, select_sql: str, *, mode: str = "append_new_columns"
    ) -> None:  # pragma: no cover
        """
        Optional: Additive schema synchronisation. 'mode' = append_new_columns|sync_all_columns.
        Default implementation: No-Op.
        """
        return None

    @staticmethod
    def _meta_is_incremental(meta: Mapping[str, Any] | None) -> bool:
        """
        Return True if the given meta mapping describes an incremental model.

        This mirrors the semantics of ModelConfig.is_incremental_enabled(), but
        works on a plain mapping to avoid tight coupling to the Pydantic model.
        """
        if not meta:
            return False

        incremental_cfg = meta.get("incremental")
        materialized = str(meta.get("materialized") or "").lower()

        # Explicit materialized='incremental' always wins.
        if materialized == "incremental":
            return True

        # incremental: true / false
        if isinstance(incremental_cfg, bool):
            return incremental_cfg

        # incremental: {enabled: bool, ...}
        if isinstance(incremental_cfg, dict):
            enabled = incremental_cfg.get("enabled")
            if isinstance(enabled, bool):
                return enabled
            # Default: treat presence of a dict as "enabled" if no explicit flag is set.
            return True

        # Fallback: any non-empty incremental value is treated as "enabled".
        return bool(incremental_cfg)

    # ── Snapshot API ──────────────────────────────────────────────────
    def snapshot_prune(
        self,
        relation: str,
        unique_key: list[str],
        keep_last: int,
        *,
        dry_run: bool = False,
    ) -> None:  # pragma: no cover - abstract
        """
        Prune old snapshot versions for the given relation.

        Engines may implement this in a best-effort manner. Default: not supported.
        """
        raise NotImplementedError(
            f"Snapshot pruning is not implemented for engine '{self.engine_name}'."
        )

    @staticmethod
    def _meta_is_snapshot(meta: Mapping[str, Any] | None) -> bool:
        """
        Return True if the given meta mapping describes a snapshot model.

        For now we define snapshots purely by materialized='snapshot'.
        """
        if not meta:
            return False
        materialized = str(meta.get("materialized") or "").lower()
        return materialized == "snapshot"

    # ---------- Unit-test helpers (to be overridden by engines) ----------

    def utest_load_relation_from_rows(self, relation: str, rows: list[dict]) -> None:
        """
        Load test input rows into a physical relation for unit tests.

        Default: not implemented. Engines that support `fft utest` should override.
        """
        raise NotImplementedError(
            f"utest_load_relation_from_rows not implemented for engine '{self.engine_name}'."
        )

    def utest_read_relation(self, relation: str) -> _PDDataFrame:
        """
        Read a physical relation into a pandas.DataFrame for unit-test assertions.

        Default: not implemented. Engines that support `fft utest` should override.
        """
        raise NotImplementedError(
            f"utest_read_relation not implemented for engine '{self.engine_name}'."
        )

    def utest_clean_target(self, relation: str) -> None:
        """
        Best-effort cleanup hook before executing a unit-test model:

        - Drop tables/views with the target name so view<->table flips
          cannot fail (DuckDB, Postgres, ...).
        - This runs *only* in `fft utest`, and we already enforce that
          utest profiles use isolated DBs/schemas.

        Default: no-op.
        """
        return

    # ── Column schema introspection hook ────────────────────────────────
    def introspect_column_physical_type(self, table: str, column: str) -> str | None:
        """
        Return the engine's physical data type for `table.column`, or None
        if it cannot be determined.

        Subclasses should override this. Default implementation raises so
        callers can surface a clear "engine not supported" message.
        """
        raise NotImplementedError(
            f"Column physical type introspection is not implemented for "
            f"engine '{self.engine_name}'."
        )

    def normalize_physical_type(self, t: str | None) -> str:
        """
        Canonicalize a physical type string for comparisons (DQ + contracts).

        Default: just strip + lower.
        Engines may override to account for dialect quirks in information_schema
        (e.g. Postgres timestamp variants, Snowflake VARCHAR(…) / NUMBER(…)).
        """
        return (t or "").strip().lower()

    # ── Seed loading hook ───────────────────────────────────────────────
    def load_seed(
        self, table: str, df: Any, schema: str | None = None
    ) -> tuple[bool, str, bool]:  # pragma: no cover - interface
        """
        Materialize a seed DataFrame into the target engine. Executors that
        support seeds should override and return True when handled.
        """
        raise NotImplementedError(
            f"Seeding is not implemented for executor engine '{self.engine_name}'."
        )

    ENGINE_NAME: str = "generic"

    @property
    def engine_name(self) -> str:
        return getattr(self, "ENGINE_NAME", "generic")
