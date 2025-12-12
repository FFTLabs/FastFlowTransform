# fastflowtransform/cli/run.py
from __future__ import annotations

import os
import textwrap
import threading
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import typer

from fastflowtransform.artifacts import (
    RunNodeResult,
    load_last_run_durations,
    write_catalog,
    write_manifest,
    write_run_results,
)
from fastflowtransform.cache import FingerprintCache, can_skip_node
from fastflowtransform.ci.changed_since import (
    compute_affected_models,
    get_changed_models,
)
from fastflowtransform.cli.bootstrap import (
    CLIContext,
    _prepare_context,
    configure_executor_contracts,
)
from fastflowtransform.cli.options import (
    CacheMode,
    CacheOpt,
    ChangedSinceOpt,
    EngineOpt,
    EnvOpt,
    ExcludeOpt,
    HttpCacheOpt,
    JobsOpt,
    KeepOpt,
    NoCacheOpt,
    OfflineOpt,
    ProjectArg,
    RebuildAllOpt,
    RebuildOnlyOpt,
    SelectOpt,
    VarsOpt,
)
from fastflowtransform.cli.selectors import (
    _compile_selector,
    _parse_select,
    _selected_subgraph_names,
    augment_with_state_modified,
)
from fastflowtransform.config.budgets import (
    BudgetLimit,
    BudgetsConfig,
    load_budgets_config,
)
from fastflowtransform.config.project import HookSpec
from fastflowtransform.core import REGISTRY, Node, relation_for
from fastflowtransform.dag import levels as dag_levels
from fastflowtransform.executors.base import BaseExecutor
from fastflowtransform.executors.budget import format_bytes
from fastflowtransform.fingerprint import (
    EnvCtx,
    build_env_ctx,
    fingerprint_py,
    fingerprint_sql,
    get_function_source,
)
from fastflowtransform.hooks.registry import load_project_hooks, resolve_hook
from fastflowtransform.hooks.types import (
    HookContext,
    ModelContext,
    ModelStatsContext,
    RunContext,
    RunStatsContext,
)
from fastflowtransform.log_queue import LogQueue
from fastflowtransform.logging import (
    bind_context,
    bound_context,
    clear_context,
    echo,
    echo_debug,
    error,
    warn,
)
from fastflowtransform.meta import ensure_meta_table
from fastflowtransform.run_executor import ScheduleResult, schedule
from fastflowtransform.utils.timefmt import _format_duration_ms


class _HookThis:
    """
    Lightweight proxy for {{ this }} in hooks:

      - str(this) -> relation name (without .ff)
      - this.name / this.relation
      - this.materialized (table|view|incremental|...)
    """

    def __init__(self, relation: str, materialized: str):
        self.name = relation
        self.relation = relation
        self.materialized = materialized
        self.schema = None
        self.database = None

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"_HookThis(name={self.name!r})"


HookWhen = Literal["on_run_start", "on_run_end", "before_model", "after_model"]


@dataclass
class _RunEngine:
    ctx: Any
    env_name: str
    pred: Callable[[Any], bool] | None
    cache_mode: CacheMode
    force_rebuild: set[str] = field(default_factory=set)
    shared: tuple[Any, Callable, Callable] = field(init=False)
    tls: threading.local = field(default_factory=threading.local, init=False)
    cache: FingerprintCache = field(init=False)
    env_ctx: EnvCtx = field(init=False)
    computed_fps: dict[str, str] = field(default_factory=dict, init=False)
    fps_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    http_snaps: dict[str, dict] = field(default_factory=dict, init=False)
    budgets_cfg: BudgetsConfig | None = None

    # per-node query stats (aggregated across all queries in that node)
    query_stats: dict[str, dict[str, int]] = field(default_factory=dict, init=False)
    stats_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # run metadata for hooks
    invocation_id: str | None = None
    run_started_at: str | None = None

    def __post_init__(self) -> None:
        echo(f"Profile: {self.env_name} | Engine: {self.ctx.profile.engine}")
        self.shared = self.ctx.make_executor()
        self._configure_budget_limit(self.shared[0])
        with suppress(Exception):
            ensure_meta_table(self.shared[0])
        relevant_env = [k for k in os.environ if k.startswith("FF_")]
        self.env_ctx = build_env_ctx(
            engine=self.ctx.profile.engine,
            profile_name=self.env_name,
            relevant_env_keys=relevant_env,
            sources=getattr(REGISTRY, "sources", {}),
        )
        self.cache = FingerprintCache(
            self.ctx.project, profile=self.env_name, engine=self.ctx.profile.engine
        )
        self.cache.load()

    def _get_runner(self) -> tuple[Any, Callable, Callable]:
        if getattr(self.tls, "runner", None) is None:
            ex, run_sql_shared, run_py_shared = self.shared
            run_sql_wrapped, run_py_wrapped = run_sql_shared, run_py_shared
            if self.ctx.profile.engine == "duckdb" and hasattr(ex, "clone"):
                try:
                    db_path = getattr(ex, "db_path", None)
                    clone_needed = not (isinstance(db_path, str) and db_path.strip() == ":memory:")
                    if clone_needed:
                        ex = ex.clone()

                        def run_sql_wrapped(node, _env=self.ctx.jinja_env, _ex=ex):
                            return _ex.run_sql(node, _env)

                        run_py_wrapped = ex.run_python
                except Exception:
                    pass
            self._configure_budget_limit(ex)
            self.tls.runner = (ex, run_sql_wrapped, run_py_wrapped)
        return self.tls.runner

    def _maybe_fingerprint(self, node: Any, ex: Any) -> str | None:
        supports_sql_fp = all(
            hasattr(ex, a) for a in ("render_sql", "_resolve_ref", "_resolve_source")
        )
        if not (supports_sql_fp or node.kind == "python"):
            return None
        with self.fps_lock:
            dep_fps = {
                d: self.computed_fps.get(d) or self.cache.get(d) or "" for d in (node.deps or [])
            }
        try:
            if node.kind == "sql" and supports_sql_fp:
                rendered = ex.render_sql(
                    node,
                    self.ctx.jinja_env,
                    ref_resolver=lambda nm: ex._resolve_ref(nm, self.ctx.jinja_env),
                    source_resolver=ex._resolve_source,
                )
                return fingerprint_sql(
                    node=node, rendered_sql=rendered, env_ctx=self.env_ctx, dep_fps=dep_fps
                )
            if node.kind == "python":
                func = REGISTRY.py_funcs[node.name]
                src = get_function_source(func)
                return fingerprint_py(
                    node=node, func_src=src, env_ctx=self.env_ctx, dep_fps=dep_fps
                )
        except Exception:
            return None
        return None

    def _executor_namespace(self) -> str | None:
        """
        Best-effort namespace (catalog/database/schema/dataset) to enrich log output.
        """
        if not isinstance(self.shared, tuple) or not self.shared:
            return None
        executor = self.shared[0]
        if executor is None:
            return None
        parts: list[str] = []
        for attr in ("catalog", "database"):
            val = getattr(executor, attr, None)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        for attr in ("dataset", "schema"):
            val = getattr(executor, attr, None)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        return ".".join(parts) if parts else None

    def _qualified_target(self, name: str) -> str | None:
        namespace = self._executor_namespace()
        if not namespace:
            return None
        rel = relation_for(name)
        if not rel:
            return None
        return f"{namespace}.{rel}"

    def _configure_budget_limit(self, executor: Any) -> None:
        if executor is None or not hasattr(executor, "configure_query_budget_limit"):
            return
        engine_name = (self.ctx.profile.engine or "").lower()
        limit = None
        if self.budgets_cfg and engine_name:
            entry = self.budgets_cfg.query_limits.get(engine_name)
            if entry:
                limit = entry.max_bytes
        executor.configure_query_budget_limit(limit)

    def format_run_label(self, name: str) -> str:
        """
        Build the human-facing label for run logs, e.g.:
          fct_events_sql_inline.ff [delta] (catalog.schema.fct_events_sql_inline)

        The storage format is resolved from:
          1) per-model storage config (project.yml → models.storage / meta.storage),
          2) engine defaults (e.g. Databricks/Spark table_format) as a fallback.

        For database engines like DuckDB/Postgres we intentionally hide the
        underlying storage format (e.g. 'parquet') to avoid confusing output.
        """
        qualified = self._qualified_target(name)
        engine = (self.ctx.profile.engine or "").lower()

        # 1) per-model storage.format from meta (preferred)
        fmt_from_meta: str | None = None
        try:
            node = REGISTRY.get_node(name)
            meta = getattr(node, "meta", {}) or {}
            storage_cfg = meta.get("storage") or {}
            if isinstance(storage_cfg, dict):
                val = storage_cfg.get("format")
                if isinstance(val, str) and val.strip():
                    fmt_from_meta = val.strip()
        except Exception:
            fmt_from_meta = None

        fmt: str | None = fmt_from_meta

        # 2) engine-level default format (e.g. Spark table_format) as fallback.
        # Only meaningful for Spark-like engines.
        if fmt is None and engine in {"databricks_spark", "spark"}:
            try:
                executor, _, _ = self.shared
                default_fmt = getattr(executor, "spark_table_format", None)
                if isinstance(default_fmt, str) and default_fmt.strip():
                    fmt = default_fmt.strip()
            except Exception:
                fmt = None

        # For database engines (DuckDB/Postgres/BigQuery), we do not show a format suffix
        # at all to avoid misleading '[parquet]' labels (these engines don't expose
        # a user-selectable table file format in FFT).
        if engine in {"duckdb", "postgres", "postgresql", "bigquery"}:
            fmt_suffix = ""
        else:
            fmt_suffix = f" [{fmt}]" if fmt else ""

        if qualified:
            return f"{name}{fmt_suffix} ({qualified})"
        return f"{name}{fmt_suffix}"

    def run_node(self, name: str) -> None:
        node = REGISTRY.nodes[name]
        ex, run_sql_fn, run_py_fn = self._get_runner()

        self._reset_executor_node_stats(ex)

        meta = getattr(node, "meta", {}) or {}

        pre_hooks, post_hooks = self._get_model_hooks(node)

        # --- force rebuild path -------------------------------------------------
        if name in self.force_rebuild:
            self._run_node_force_rebuild(
                name=name,
                node=node,
                ex=ex,
                run_sql_fn=run_sql_fn,
                run_py_fn=run_py_fn,
                pre_hooks=pre_hooks,
                post_hooks=post_hooks,
            )
            return

        # --- fingerprint + cache skip path --------------------------------------
        cand_fp = self._maybe_fingerprint(node, ex)
        if self._should_skip_node(
            name=name,
            node=node,
            ex=ex,
            cand_fp=cand_fp,
            meta=meta,
        ):
            return

        # --- normal run ---------------------------------------------------------
        self._run_model_with_hooks(
            name=name,
            node=node,
            ex=ex,
            run_sql_fn=run_sql_fn,
            run_py_fn=run_py_fn,
            pre_hooks=pre_hooks,
            post_hooks=post_hooks,
        )

        self._finalize_node_run(
            name=name,
            node=node,
            ex=ex,
            cand_fp=cand_fp,
        )

    # ---------------------------------------------------------------------------
    # Helpers for run_node
    # ---------------------------------------------------------------------------

    def _reset_executor_node_stats(self, ex: BaseExecutor) -> None:
        # Reset per-node stats if the executor supports it
        with suppress(Exception):
            reset = getattr(ex, "reset_node_stats", None)
            if callable(reset):
                reset()

    def _get_model_hooks(
        self,
        node: Node,
    ) -> tuple[Sequence[HookSpec] | None, Sequence[HookSpec] | None]:
        pre_hooks = self._model_hooks_for_when("before_model", node)
        post_hooks = self._model_hooks_for_when("after_model", node)
        return pre_hooks, post_hooks

    def _run_model_with_hooks(
        self,
        *,
        name: str,
        node: Node,
        ex: BaseExecutor,
        run_sql_fn: Callable[[Node], None],
        run_py_fn: Callable[[Node], None],
        pre_hooks: Sequence[HookSpec] | None,
        post_hooks: Sequence[HookSpec] | None,
    ) -> ModelStatsContext | None:
        # pre-hook
        self._run_hooks(pre_hooks, node=node, when="before_model", ex=ex)

        # actual model
        (run_sql_fn if node.kind == "sql" else run_py_fn)(node)

        # capture per-node stats *now* so after_model hooks can use them
        model_stats = self._collect_model_stats_for_hooks(ex=ex, name=name)

        # post-hook
        self._run_hooks(
            post_hooks,
            node=node,
            when="after_model",
            ex=ex,
            model_stats=model_stats,
        )

        return model_stats

    def _collect_model_stats_for_hooks(
        self,
        *,
        ex: BaseExecutor,
        name: str,
    ) -> ModelStatsContext | None:
        model_stats: ModelStatsContext | None = None
        with suppress(Exception):
            raw_getter = getattr(ex, "get_node_stats", None)
            if callable(raw_getter):
                getter = cast(Callable[[], dict[str, int] | None], raw_getter)
                s = getter()
                if s:
                    rows = int(s.get("rows", 0) or 0)
                    bytes_scanned = int(s.get("bytes_scanned", 0) or 0)
                    query_duration_ms = int(s.get("query_duration_ms", 0) or 0)

                    model_stats = ModelStatsContext(
                        rows=rows,
                        bytes_scanned=bytes_scanned,
                        query_duration_ms=query_duration_ms,
                    )

                    # keep a plain dict[str, int] copy for budgets / run_results
                    with self.stats_lock:
                        self.query_stats[name] = {
                            "rows": rows,
                            "bytes_scanned": bytes_scanned,
                            "query_duration_ms": query_duration_ms,
                        }

        return model_stats

    def _run_node_force_rebuild(
        self,
        *,
        name: str,
        node: Node,
        ex: BaseExecutor,
        run_sql_fn: Callable[[Node], None],
        run_py_fn: Callable[[Node], None],
        pre_hooks: Sequence[HookSpec] | None,
        post_hooks: Sequence[HookSpec] | None,
    ) -> None:
        # Run model (with hooks + early stats)
        self._run_model_with_hooks(
            name=name,
            node=node,
            ex=ex,
            run_sql_fn=run_sql_fn,
            run_py_fn=run_py_fn,
            pre_hooks=pre_hooks,
            post_hooks=post_hooks,
        )

        # fingerprint + on_node_built
        cand_fp = self._maybe_fingerprint(node, ex)
        if cand_fp:
            self._store_fingerprint(name, cand_fp)
            self._notify_executor_node_built(node=node, ex=ex, name=name, cand_fp=cand_fp)

        # HTTP snapshot
        self._capture_http_snapshot(node=node, name=name)

        # capture per-node stats after successful run
        self._capture_final_stats(ex=ex, name=name)

    def _should_skip_node(
        self,
        *,
        name: str,
        node: Node,
        ex: BaseExecutor,
        cand_fp: str | None,
        meta: dict[str, Any] | None,
    ) -> bool:
        if cand_fp is None:
            return False

        materialized = (meta or {}).get("materialized", "table")
        may_skip = self.cache_mode in (CacheMode.RW, CacheMode.RO)
        if not may_skip:
            return False

        if not can_skip_node(
            node_name=name,
            new_fp=cand_fp,
            cache=self.cache,
            executor=ex,
            materialized=materialized,
        ):
            return False

        # we're skipping: still record fingerprint + zero stats
        self._store_fingerprint(name, cand_fp)
        with self.stats_lock:
            self.query_stats[name] = {
                "bytes_scanned": 0,
                "rows": 0,
                "query_duration_ms": 0,
                "cached": True,
            }
        return True

    def _finalize_node_run(
        self,
        *,
        name: str,
        node: Node,
        ex: BaseExecutor,
        cand_fp: str | None,
    ) -> None:
        if cand_fp is not None:
            self._store_fingerprint(name, cand_fp)
            self._notify_executor_node_built(node=node, ex=ex, name=name, cand_fp=cand_fp)

        # HTTP snapshot (stored in node.meta by the executor)
        self._capture_http_snapshot(node=node, name=name)

        # capture per-node stats after successful run
        self._capture_final_stats(ex=ex, name=name)

    def _store_fingerprint(self, name: str, cand_fp: str) -> None:
        with self.fps_lock:
            self.computed_fps[name] = cand_fp

    def _notify_executor_node_built(
        self,
        *,
        node: Node,
        ex: BaseExecutor,
        name: str,
        cand_fp: str,
    ) -> None:
        with suppress(Exception):
            ex.on_node_built(node, relation_for(name), cand_fp)

    def _capture_http_snapshot(self, *, node: Node, name: str) -> None:
        with suppress(Exception):
            snap = (getattr(node, "meta", {}) or {}).get("_http_snapshot")
            if snap:
                self.http_snaps[name] = snap

    def _capture_final_stats(self, *, ex: BaseExecutor, name: str) -> None:
        with suppress(Exception):
            raw_getter = getattr(ex, "get_node_stats", None)
            if callable(raw_getter):
                getter = cast(Callable[[], dict[str, int] | None], raw_getter)
                stats = getter()
                if stats:
                    with self.stats_lock:
                        self.query_stats[name] = stats

    # ---------- Hook helpers ----------

    @staticmethod
    def _normalize_hooks(hooks_raw: Any) -> list[str]:
        """
        Accept str | list[str] | None and return a clean list[str].
        """
        if hooks_raw is None:
            return []
        if isinstance(hooks_raw, str):
            text = hooks_raw.strip()
            return [text] if text else []
        if isinstance(hooks_raw, Iterable) and not isinstance(hooks_raw, (str, bytes, Mapping)):
            out: list[str] = []
            for item in hooks_raw:
                if item is None:
                    continue
                s = str(item).strip()
                if s:
                    out.append(s)
            return out
        # Be permissive: anything else → single string repr
        s = str(hooks_raw).strip()
        return [s] if s else []

    def _render_hook_sql(self, template_text: str, node: Any | None, ex: Any) -> str:
        """
        Render a single hook expression into SQL using the project's Jinja env.
        """
        env = self.ctx.jinja_env
        tmpl = env.from_string(template_text)

        run_started = self.run_started_at
        inv_id = self.invocation_id

        this_obj = None
        target = None

        if node is not None:
            meta = getattr(node, "meta", {}) or {}
            relation = relation_for(node.name)
            mat = str(meta.get("materialized") or "table")
            this_obj = _HookThis(relation, mat)
            target = self._qualified_target(node.name) or relation

        def _hook_ref(name: str) -> str:
            # Use executor's resolution if available
            try:
                return ex._format_relation_for_ref(name)
            except Exception:
                return relation_for(name)

        def _hook_source(source_name: str, table_name: str) -> str:
            try:
                return ex._resolve_source(source_name, table_name)
            except Exception as exc:
                raise KeyError(
                    f"Error resolving source('{source_name}', '{table_name}') in hook: {exc}"
                ) from exc

        return tmpl.render(
            # hook-specific context
            this=this_obj,
            target=target,
            run_started_at=run_started,
            invocation_id=inv_id,
            # resolution helpers
            ref=_hook_ref,
            source=_hook_source,
        )

    def _execute_hook_sql(self, sql: str, ex: Any) -> None:
        """
        Execute one or more SQL statements for a hook.

        We normalize away semicolons in full-line comments so that naive
        ';'-based splitters in executors don't produce bogus statements like:

            "-- comment; with semicolon"  ->  ["-- comment", " with semicolon"]
        """
        if not sql or not sql.strip():
            return

        # Trim outer whitespace but preserve inner newlines
        sql = sql.strip()

        # --- Normalize comment lines: drop ';' inside "-- ..." lines -----------
        cleaned_lines: list[str] = []
        for line in sql.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("--"):
                # Keep comment but remove semicolons so ';' splitters don't see them
                prefix_len = len(line) - len(stripped)
                comment = stripped.replace(";", "")
                cleaned_lines.append(" " * prefix_len + comment)
            else:
                cleaned_lines.append(line)

        normalized_sql = "\n".join(cleaned_lines)

        # --- Delegate to executor if it has a hook-aware API -------------------
        # Let the executor decide how to split multi-statement SQL
        ex.execute_hook_sql(normalized_sql)
        return

    def _load_sql_hook_body(self, spec: HookSpec) -> str:
        """
        Resolve the SQL text for a SQL hook.

        Resolution order:
        1) Inline `spec.sql`
        2) Any `<project_dir>/hooks/**/*.sql` whose *stem* matches `spec.name`.
            (first match wins)
        """
        # 1) Inline SQL wins if present
        if spec.sql and spec.sql.strip():
            return spec.sql

        name = spec.name
        if name is None:
            raise ValueError("SQL HookSpec must have a non-empty 'name' for file-based hooks")

        project_dir = Path(self.ctx.project)
        hooks_dir = project_dir / "hooks"

        if not hooks_dir.is_dir():
            raise FileNotFoundError(
                f"SQL hook {spec.name!r} has no inline `sql` and no 'hooks/' directory exists "
                f"under project: {project_dir}"
            )

        # 2) Build (or reuse) cache: stem -> Path
        cache_attr = "_sql_hook_files"
        mapping: dict[str, Path]

        if hasattr(self, cache_attr):
            mapping = getattr(self, cache_attr)
        else:
            mapping = {}
            for _path in hooks_dir.rglob("*.sql"):
                # stem is the filename without suffix, e.g. "audit_run_end"
                stem = _path.stem
                # last-one-wins is fine; or keep first with `if stem not in mapping:`
                mapping[stem] = _path
            setattr(self, cache_attr, mapping)

        path: Path | None = mapping.get(name)
        if not path:
            raise FileNotFoundError(
                f"SQL hook {name!r} has no inline `sql` and no matching "
                f"file '<project>/hooks/**/{name}.sql' was found."
            )

        return path.read_text(encoding="utf-8")

    def _model_hooks_for_when(self, when: HookWhen, node: Node) -> list[HookSpec]:
        """
        Return all model-level HookSpecs from project.yml that apply to this node
        for the given lifecycle event (before_model / after_model), based on
        their `select` expression.

        Global run-level hooks (on_run_start / on_run_end) are handled elsewhere.
        """
        if when == "before_model":
            all_specs: Sequence[HookSpec] = getattr(REGISTRY, "before_model_hooks", []) or []
        elif when == "after_model":
            all_specs = getattr(REGISTRY, "after_model_hooks", []) or []
        else:
            # Only model-level lifecycles are handled here
            return []

        applicable: list[HookSpec] = []

        for spec in all_specs:
            sel = (spec.select or "").strip()
            if not sel:
                # No selector → applies to all models
                applicable.append(spec)
                continue

            try:
                # Reuse the same selector compiler as the CLI
                tokens = _parse_select([sel])
                _, pred = _compile_selector(tokens)
                if pred(node):
                    applicable.append(spec)
            except Exception as exc:
                warn(
                    f"[hooks] invalid select={sel!r} for hook {spec.name!r} "
                    f"on when={when}: {exc}; skipping"
                )

        return applicable

    def _hook_matches_current_env(self, spec: HookSpec) -> bool:
        """
        Decide whether a hook should run in the current engine/env.

        - If spec.engines is set, the active engine must be in that list.
        - If spec.envs is set, the active env_name must be in that list.
        - If a field is None/empty, it does not restrict execution.
        """
        engine_name = (self.ctx.profile.engine or "").lower()
        env_name = self.env_name

        # engines filter
        if spec.engines:
            allowed_engines = [e.lower() for e in spec.engines if isinstance(e, str)]
            if engine_name not in allowed_engines:
                return False

        # envs filter
        return not (spec.envs and env_name not in spec.envs)

    def _run_hooks(
        self,
        hooks_raw: Sequence[HookSpec] | None,
        node: Node | None,
        when: HookWhen,
        ex: BaseExecutor,
        *,
        run_status: str | None = None,
        run_stats: RunStatsContext | None = None,
        model_stats: ModelStatsContext | None = None,
    ) -> None:
        """
        Execute a list of hooks.

        New model:
        - Only HookSpec objects (no string hooks).
        - kind: "sql" or "python".
        - Python hooks are resolved from the decorator registry
        by (when, spec.name) and receive a single `context` dict.
        """
        self._validate_hook_when(when)

        label = node.name if node is not None else "<run>"

        if not hooks_raw:
            echo(f"[hooks] when={when} node={label}: no hooks registered")
            return

        jenv = self.ctx.jinja_env

        # Ensure all hooks/*.py are loaded and their @fft_hook decorators executed
        project_dir = str(self.ctx.project)
        load_project_hooks(project_dir)

        run_ctx_py = self._build_run_context(run_status=run_status, run_stats=run_stats)
        model_ctx = self._build_model_context(
            node=node,
            model_stats=model_stats,
            when=when,
        )
        env_vars = self._snapshot_env_vars()

        active_specs = self._filter_active_specs(hooks_raw)
        if not active_specs:
            echo(f"[hooks] when={when} node={label}: no hooks after engine/env filtering")
            return

        self._log_active_specs(active_specs, when, label)

        for idx, spec in enumerate(active_specs, start=1):
            self._execute_single_hook(
                spec=spec,
                idx=idx,
                when=when,
                node_label=label,
                run_ctx_py=run_ctx_py,
                model_ctx=model_ctx,
                env_vars=env_vars,
                run_stats=run_stats,
                model_stats=model_stats,
                ex=ex,
                jenv=jenv,
            )

    # ----------------- helpers for _run_hook -----------------

    def _validate_hook_when(self, when: HookWhen) -> None:
        allowed = ("on_run_start", "on_run_end", "before_model", "after_model")
        if when not in allowed:
            raise ValueError(f"Unsupported hook 'when' value: {when!r}")

    def _build_run_context(
        self,
        *,
        run_status: str | None,
        run_stats: RunStatsContext | None,
    ) -> RunContext:
        row_count: int | None = None
        if run_stats is not None:
            rc = run_stats.get("rows_total")
            if rc is not None:
                row_count = int(rc)

        return {
            "run_id": str(self.invocation_id),
            "env_name": self.env_name,
            "engine_name": (self.ctx.profile.engine or "").lower(),
            "started_at": str(self.run_started_at),
            "status": run_status,
            "row_count": row_count,
            "error": None,
        }

    def _build_model_context(
        self,
        *,
        node: Node | None,
        model_stats: ModelStatsContext | None,
        when: HookWhen,
    ) -> ModelContext | None:
        if node is None:
            return None

        meta = getattr(node, "meta", {}) or {}

        raw_tags = meta.get("tags")
        if isinstance(raw_tags, (list, tuple, set)):
            tags_list = [str(t) for t in raw_tags]
        elif isinstance(raw_tags, str):
            tags_list = [raw_tags]
        else:
            tags_list = []

        model_ctx = cast(
            ModelContext,
            {
                "name": str(node.name),
                "path": node.path,
                "tags": sorted(tags_list),
                "meta": meta,
                "status": None,
                "rows_affected": None,
                "elapsed_ms": None,
                "error": None,
            },
        )

        if model_stats is not None:
            model_ctx["rows_affected"] = model_stats.get("rows")
            model_ctx["elapsed_ms"] = model_stats.get("query_duration_ms")
            if when == "after_model" and model_ctx.get("status") is None:
                model_ctx["status"] = "success"

        return model_ctx

    def _snapshot_env_vars(self) -> dict[str, str]:
        # Snapshot env vars as a plain dict[str, str]
        return dict(getattr(self.env_ctx, "env_vars", {}) or {})

    def _filter_active_specs(
        self,
        hooks_raw: Sequence[HookSpec] | None,
    ) -> list[HookSpec]:
        if not hooks_raw:
            return []
        return [s for s in hooks_raw if self._hook_matches_current_env(s)]

    def _log_active_specs(
        self,
        active_specs: Sequence[HookSpec],
        when: HookWhen,
        label: str,
    ) -> None:
        summary = ", ".join(f"{spec.kind}:{(spec.name or '<unnamed>')}" for spec in active_specs)
        echo(f"[hooks] when={when} node={label}: executing {len(active_specs)} hook(s): {summary}")

    def _execute_single_hook(
        self,
        *,
        spec: HookSpec,
        idx: int,
        when: HookWhen,
        node_label: str,
        run_ctx_py: RunContext,
        model_ctx: ModelContext | None,
        env_vars: dict[str, str],
        run_stats: RunStatsContext | None,
        model_stats: ModelStatsContext | None,
        ex: BaseExecutor,
        jenv: Any,
    ) -> None:
        hook_name = spec.name or "<unnamed>"

        try:
            if not isinstance(spec, HookSpec):
                raise TypeError(
                    f"Hooks must be HookSpec instances; got {type(spec)!r} at index {idx}"
                )

            # Engine/env filter (kept for safety even after pre-filtering)
            if not self._hook_matches_current_env(spec):
                echo_debug(
                    f"[hooks] when={when} node={node_label} hook#{idx} "
                    f"name={hook_name!r} - skipped (engine/env mismatch)"
                )
                return

            if spec.kind == "sql":
                self._execute_sql_hook(
                    spec=spec,
                    idx=idx,
                    when=when,
                    node_label=node_label,
                    run_ctx_py=run_ctx_py,
                    model_ctx=model_ctx,
                    ex=ex,
                    jenv=jenv,
                )
                return

            if spec.kind == "python":
                self._execute_python_hook(
                    spec=spec,
                    idx=idx,
                    when=when,
                    node_label=node_label,
                    run_ctx_py=run_ctx_py,
                    model_ctx=model_ctx,
                    env_vars=env_vars,
                    run_stats=run_stats,
                    model_stats=model_stats,
                )
                return

            raise ValueError(f"Unknown hook kind {spec.kind!r} for hook #{idx}")

        except Exception as exc:
            error(
                f"[hooks] ERROR when={when} node={node_label} hook#{idx} "
                f"kind={spec.kind!r} name={(spec.name or '<unnamed>')!r}: {exc}"
            )
            raise RuntimeError(
                f"Failed to execute {when} hook #{idx} for {node_label}: {exc}"
            ) from exc

    def _execute_sql_hook(
        self,
        *,
        spec: HookSpec,
        idx: int,
        when: HookWhen,
        node_label: str,
        run_ctx_py: RunContext,
        model_ctx: ModelContext | None,
        ex: BaseExecutor,
        jenv: Any,
    ) -> None:
        hook_name = spec.name or "<unnamed>"

        echo_debug(
            f"[hooks] when={when} node={node_label} hook#{idx} "
            f"kind=sql name={hook_name!r} - rendering SQL"
        )

        sql_body = self._load_sql_hook_body(spec)
        sql_tmpl = sql_body.strip()
        if not sql_tmpl:
            warn(
                f"[hooks] when={when} node={node_label} hook#{idx} "
                f"name={hook_name!r} has empty SQL, skipping"
            )
            return

        tmpl = jenv.from_string(sql_tmpl)

        run_ctx_sql = dict(run_ctx_py)

        if model_ctx is None:
            model_ctx_render: dict[str, Any] | None = None
        else:
            model_ctx_render = dict(model_ctx)
            if when in ("before_model", "after_model"):
                model_ctx_render.setdefault(
                    "status",
                    "running" if when == "before_model" else "success",
                )
                model_ctx_render.setdefault("rows_affected", None)
                model_ctx_render.setdefault("elapsed_ms", None)
                model_ctx_render.setdefault("error", None)

        sql = tmpl.render(
            run=run_ctx_sql,
            model=model_ctx_render,
            node=model_ctx_render,
        )

        if not sql.strip():
            warn(
                f"[hooks] when={when} node={node_label} hook#{idx} "
                f"name={hook_name!r} rendered empty SQL, skipping"
            )
            return

        echo_debug(
            f"[hooks] when={when} node={node_label} hook#{idx} "
            f"name={hook_name!r} executing SQL:\n{sql}"
        )
        self._execute_hook_sql(sql, ex)

    def _execute_python_hook(
        self,
        *,
        spec: HookSpec,
        idx: int,
        when: HookWhen,
        node_label: str,
        run_ctx_py: RunContext,
        model_ctx: ModelContext | None,
        env_vars: dict[str, str],
        run_stats: RunStatsContext | None,
        model_stats: ModelStatsContext | None,
    ) -> None:
        if not spec.name:
            raise ValueError(
                "Python HookSpec must have a 'name' set; "
                "this is used to resolve the hook from the registry."
            )

        hook_name = spec.name

        echo(
            f"[hooks] when={when} node={node_label} hook#{idx} "
            f"kind=python name={hook_name!r} - resolving from registry"
        )

        fn = resolve_hook(when=when, name=spec.name)

        context: HookContext = {
            "when": when,
            "run": run_ctx_py,
            "model": model_ctx,
            "env": env_vars,
        }

        if run_stats is not None:
            context["run_stats"] = run_stats
        if model_stats is not None:
            context["model_stats"] = model_stats

        if spec.params:
            context["params"] = dict(spec.params)

        echo(
            f"[hooks] when={when} node={node_label} hook#{idx} "
            f"name={hook_name!r} - invoking python hook"
        )

        fn(context)

    @staticmethod
    def before(_name: str, lvl_idx: int | None = None) -> None:
        return

    @staticmethod
    def on_error(name: str, err: BaseException) -> None:
        _node = REGISTRY.get_node(name)
        if isinstance(err, KeyError):
            echo(
                _error_block(
                    f"Model failed: {name} (KeyError)",
                    _pretty_exc(err),
                    "• Check column names in your upstream tables (seeds/SQL).\n"
                    "• For >1 deps: dict keys are physical relations (relation_for), "
                    "e.g. 'orders'.\n"
                    "• (Optional) Log input columns in the executor before the call.",
                )
            )
            raise typer.Exit(1) from err
        body = _pretty_exc(err)
        if os.getenv("FFT_TRACE") == "1":
            body += "\n\n" + traceback.format_exc()
        echo(_error_block(f"Model failed: {name}", body, "• See cause above."))
        raise typer.Exit(1) from err

    def persist_on_success(self, result: ScheduleResult) -> None:
        if not result.failed and (self.cache_mode in (CacheMode.RW, CacheMode.WO)):
            self.cache.update_many(self.computed_fps)
            self.cache.save()

    @staticmethod
    def print_timings(result: ScheduleResult) -> None:
        if not result.per_node_s:
            return
        echo("\nRuntime per model")
        echo("─────────────────")
        for name in sorted(result.per_node_s, key=lambda k: k):
            ms = int(result.per_node_s[name] * 1000)
            echo(f"• {name:<30} {ms:>6} ms")
        echo(f"\nTotal runtime: {result.total_s:.3f}s")


def _pretty_exc(e: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(e), e)).strip()


def _error_block(title: str, body: str, hint: str | None = None) -> str:
    border = "─" * 70
    lines = [f"✖ {title}", "", textwrap.dedent(body).rstrip()]
    if hint:
        lines += ["", "Hints:", textwrap.dedent(hint).rstrip()]
    text = "│ \n│ ".join("\n".join(lines).splitlines())
    return f"\n┌{border}\n{text}\n└{border}\n"


def _normalize_node_names_or_warn(names: list[str] | None) -> set[str]:
    out: set[str] = set()
    for tok in _parse_select(names or []):
        try:
            node = REGISTRY.get_node(tok)
        except KeyError:
            warn(f"Unknown model in --rebuild: {tok}")
            continue

        if _is_snapshot_model(node):
            warn(
                f"Ignoring snapshot model in --rebuild: {tok} "
                "(snapshot models are not executed via 'fft run'; "
                "use 'fft snapshot run' instead)."
            )
            continue

        out.add(node.name)

    return out


def _abbr(e: str) -> str:
    mapping = {
        "duckdb": "DUCK",
        "postgres": "PG",
        "bigquery": "BQ",
        "databricks_spark": "SPK",
        "snowflake_snowpark": "SNOW",
    }
    return mapping.get(e, e.upper()[:4])


def _is_snapshot_model(node: Any) -> bool:
    """
    Return True if this node is a snapshot model (materialized='snapshot').
    """
    meta = getattr(node, "meta", {}) or {}
    mat = str(meta.get("materialized") or "").lower()
    return mat == "snapshot"


def _check_metric_limits(
    *,
    scope: str,
    metric_name: str,
    value: int,
    limits: BudgetLimit,
) -> tuple[bool, bool]:
    """
    Check a single metric against warn/error thresholds.

    Returns (warn_triggered, error_triggered).
    """
    if value <= 0:
        return False, False

    # Decide how to render the metric & thresholds
    if metric_name == "bytes_scanned":
        value_str = format_bytes(value)
        warn_str = format_bytes(limits.warn) if limits.warn else None
        err_str = format_bytes(limits.error) if limits.error else None
        unit_label = "bytes_scanned"
    elif metric_name == "rows":
        value_str = f"{value:,} rows"
        warn_str = f"{limits.warn:,} rows" if limits.warn else None
        err_str = f"{limits.error:,} rows" if limits.error else None
        unit_label = "rows"
    else:  # "query_duration_ms"
        value_str = _format_duration_ms(value)
        warn_str = _format_duration_ms(limits.warn) if limits.warn else None
        err_str = _format_duration_ms(limits.error) if limits.error else None
        unit_label = "query_duration_ms"

    # Prefer error over warn (avoid double-logging)
    if limits.error and value > limits.error:
        error(
            f"[BUDGET] {scope}: {unit_label} {value_str} exceeds "
            f"error limit {err_str} (budgets.yml)."
        )
        return False, True

    if limits.warn and value > limits.warn:
        warn(
            f"[BUDGET] {scope}: {unit_label} {value_str} exceeds "
            f"warn limit {warn_str} (budgets.yml)."
        )
        return True, False

    return False, False


def _value_and_limits_str(
    metric_name: str,
    value: int,
    limits: BudgetLimit,
) -> tuple[str, str | None, str | None]:
    if metric_name == "bytes_scanned":
        v_str = format_bytes(value)
        w_str = format_bytes(limits.warn) if limits.warn else None
        e_str = format_bytes(limits.error) if limits.error else None
    elif metric_name == "rows":
        v_str = f"{value:,} rows"
        w_str = f"{limits.warn:,} rows" if limits.warn else None
        e_str = f"{limits.error:,} rows" if limits.error else None
    else:  # query_duration_ms
        v_str = _format_duration_ms(value)
        w_str = _format_duration_ms(limits.warn) if limits.warn else None
        e_str = _format_duration_ms(limits.error) if limits.error else None
    return v_str, w_str, e_str


def _eval_metric(scope: str, metric_name: str, value: int, limits: BudgetLimit) -> str:
    """
    Evaluate {warn,error} thresholds for a single metric.

    Returns status: "ok" | "warn" | "error".
    Emits log lines for warn/error.
    """
    if value <= 0:
        return "ok"
    if not limits.warn and not limits.error:
        return "ok"

    v_str, w_str, e_str = _value_and_limits_str(metric_name, value, limits)
    unit_label = metric_name

    # Prefer error over warn
    if limits.error and value > limits.error:
        error(f"[BUDGET] {scope}: {unit_label} {v_str} exceeds error limit {e_str} (budgets.yml).")
        return "error"

    if limits.warn and value > limits.warn:
        warn(f"[BUDGET] {scope}: {unit_label} {v_str} exceeds warn limit {w_str} (budgets.yml).")
        return "warn"

    return "ok"


def _aggregate_totals(stats_by_model: dict[str, dict[str, Any]]) -> dict[str, int]:
    totals = {"bytes_scanned": 0, "rows": 0, "query_duration_ms": 0}
    for s in stats_by_model.values():
        totals["bytes_scanned"] += int(s.get("bytes_scanned", 0) or 0)
        totals["rows"] += int(s.get("rows", 0) or 0)
        totals["query_duration_ms"] += int(s.get("query_duration_ms", 0) or 0)
    return totals


def _evaluate_total_budgets(
    cfg: Any,
    totals: dict[str, int],
    budgets_summary: dict[str, Any],
) -> bool:
    had_error = False
    if not cfg.total:
        return had_error

    for metric_name in ("bytes_scanned", "rows", "query_duration_ms"):
        limits: BudgetLimit | None = getattr(cfg.total, metric_name)
        if not limits:
            continue
        value = totals.get(metric_name, 0)
        status = _eval_metric("total (all models)", metric_name, value, limits)
        if status != "ok" or (limits.warn or limits.error):
            budgets_summary["total"][metric_name] = {
                "value": value,
                "warn": limits.warn,
                "error": limits.error,
                "status": status,
            }
        if status == "error":
            had_error = True

    return had_error


def _evaluate_model_budgets(
    cfg: Any,
    stats_by_model: dict[str, dict[str, Any]],
    budgets_summary: dict[str, Any],
) -> bool:
    had_error = False

    for model_name, metrics in (cfg.models or {}).items():
        s = stats_by_model.get(model_name)
        if not s:
            continue

        model_summary: dict[str, Any] = {}
        for metric_name in ("bytes_scanned", "rows", "query_duration_ms"):
            limits: BudgetLimit | None = getattr(metrics, metric_name)
            if not limits:
                continue
            value = int(s.get(metric_name, 0) or 0)
            status = _eval_metric(f"model '{model_name}'", metric_name, value, limits)
            if status != "ok" or (limits.warn or limits.error):
                model_summary[metric_name] = {
                    "value": value,
                    "warn": limits.warn,
                    "error": limits.error,
                    "status": status,
                }
            if status == "error":
                had_error = True

        if model_summary:
            budgets_summary["models"][model_name] = model_summary

    return had_error


def _aggregate_tag_totals(
    cfg: Any,
    stats_by_model: dict[str, dict[str, Any]],
) -> dict[str, dict[str, int]]:
    tag_totals: dict[str, dict[str, int]] = {
        tag: {"bytes_scanned": 0, "rows": 0, "query_duration_ms": 0} for tag in (cfg.tags or {})
    }

    for model_name, s in stats_by_model.items():
        try:
            node = REGISTRY.get_node(model_name)
        except KeyError:
            continue

        meta = getattr(node, "meta", {}) or {}
        tags = meta.get("tags") or []
        tags_str = [str(t) for t in tags]

        for tag in tags_str:
            if tag not in tag_totals:
                continue
            aggr = tag_totals[tag]
            aggr["bytes_scanned"] += int(s.get("bytes_scanned", 0) or 0)
            aggr["rows"] += int(s.get("rows", 0) or 0)
            aggr["query_duration_ms"] += int(s.get("query_duration_ms", 0) or 0)

    return tag_totals


def _evaluate_tag_budgets(
    cfg: Any,
    tag_totals: dict[str, dict[str, int]],
    budgets_summary: dict[str, Any],
) -> bool:
    had_error = False
    if not cfg.tags:
        return had_error

    for tag, metrics in (cfg.tags or {}).items():
        aggr = tag_totals.get(tag) or {}
        tag_summary: dict[str, Any] = {}
        for metric_name in ("bytes_scanned", "rows", "query_duration_ms"):
            limits: BudgetLimit | None = getattr(metrics, metric_name)
            if not limits:
                continue
            value = int(aggr.get(metric_name, 0) or 0)
            status = _eval_metric(
                f"tag '{tag}' (all models with this tag)", metric_name, value, limits
            )
            if status != "ok" or (limits.warn or limits.error):
                tag_summary[metric_name] = {
                    "value": value,
                    "warn": limits.warn,
                    "error": limits.error,
                    "status": status,
                }
            if status == "error":
                had_error = True

        if tag_summary:
            budgets_summary["tags"][tag] = tag_summary

    return had_error


def _resolve_budgets_cfg(project_dir: Path, engine_: _RunEngine) -> Any | None:
    cfg = engine_.budgets_cfg
    if cfg is not None:
        return cfg

    try:
        cfg = load_budgets_config(project_dir)
    except Exception as exc:  # pragma: no cover - CLI error path
        # Parsing error is considered fatal: surface a clear message and fail the run.
        error(f"Failed to parse budgets.yml: {exc}")
        raise typer.Exit(1) from exc

    engine_.budgets_cfg = cfg
    return cfg


def _evaluate_budgets(
    project_dir: Path,
    engine_: _RunEngine,
) -> tuple[bool, dict[str, Any] | None]:
    """
    Enforce budgets.yml against collected query_stats.

    Returns:
      (had_error_budget: bool, budgets_summary: dict | None)
    """
    cfg = _resolve_budgets_cfg(project_dir, engine_)

    if cfg is None:
        # No budgets.yml → nothing to enforce
        return False, None

    stats_by_model: dict[str, dict[str, Any]] = getattr(engine_, "query_stats", {}) or {}
    if not stats_by_model:
        # No stats collected (e.g. purely Python models) → nothing to enforce
        return False, None

    totals = _aggregate_totals(stats_by_model)

    # Summary structure for run_results.json
    budgets_summary: dict[str, Any] = {
        "total": {},
        "models": {},
        "tags": {},
    }

    had_error = False
    had_error |= _evaluate_total_budgets(cfg, totals, budgets_summary)
    had_error |= _evaluate_model_budgets(cfg, stats_by_model, budgets_summary)

    if cfg.tags:
        tag_totals = _aggregate_tag_totals(cfg, stats_by_model)
        had_error |= _evaluate_tag_budgets(cfg, tag_totals, budgets_summary)

    # If budgets_summary is entirely empty, return None for clarity
    if not any(budgets_summary[section] for section in ("total", "models", "tags")):
        return had_error, None

    return had_error, budgets_summary


# ----------------- helpers (run function) -----------------


def _build_engine_ctx(project, env_name, engine, vars, cache, no_cache):
    ctx = _prepare_context(project, env_name, engine, vars)
    cache_mode = CacheMode.OFF if no_cache else cache
    engine_ = _RunEngine(
        ctx=ctx,
        pred=None,
        env_name=env_name,
        cache_mode=cache_mode,
        force_rebuild=set(),
        budgets_cfg=ctx.budgets_cfg,
    )
    return ctx, engine_


def _select_predicate_and_raw(
    executor_engine: _RunEngine,
    ctx: CLIContext,
    select: SelectOpt,
    *,
    include_snapshots: bool = False,
) -> tuple[list[str], Callable[[Any], bool], list[str]]:
    select_tokens = _parse_select(select or [])
    base_tokens = [t for t in select_tokens if not t.startswith("state:modified")]
    _, base_pred = _compile_selector(base_tokens)

    select_pred = base_pred
    if select_tokens and any(t.startswith("state:modified") for t in select_tokens):
        executor = executor_engine.shared[0]
        modified_set = executor_engine.cache.modified_set(ctx.jinja_env, executor)
        select_pred = augment_with_state_modified(select_tokens, base_pred, modified_set)

    raw_selected = []
    for k, v in REGISTRY.nodes.items():
        if not select_pred(v):
            continue
        if not include_snapshots and _is_snapshot_model(v):
            continue
        raw_selected.append(k)
    return select_tokens, select_pred, raw_selected


def _wanted_names(
    select_tokens: list[str], exclude: ExcludeOpt, raw_selected: list[str]
) -> set[str]:
    return _selected_subgraph_names(
        REGISTRY.nodes,
        select_tokens=select_tokens,
        exclude_tokens=exclude,
        seed_names=set(raw_selected),
    )


def _apply_changed_since_filter(
    ctx: CLIContext,
    wanted: set[str],
    select: SelectOpt,
    exclude: ExcludeOpt,
    changed_since: str | None,
) -> set[str]:
    """
    If --changed-since is provided, restrict the selection to models whose
    files changed since the given git ref PLUS their upstream/downstream
    neighbors.

    Semantics:
      - Without --select/--exclude:
          wanted = affected_models
      - With --select or --exclude:
          wanted = wanted ∩ affected_models

      (So you can combine tag/namespace selectors with --changed-since.)
    """
    if not changed_since:
        return wanted

    project_dir = ctx.project
    if not isinstance(project_dir, Path):
        project_dir = Path(project_dir)

    changed = get_changed_models(project_dir, changed_since)
    affected = compute_affected_models(changed, REGISTRY.nodes)

    if not affected:
        # Nothing affected by changes → nothing to run
        return set()

    # If user also provided selectors/excludes, intersect with those.
    if (select and len(select) > 0) or (exclude and len(exclude) > 0):
        return wanted & affected

    # No further selectors → affected models define the universe
    return affected


def _explicit_targets(
    rebuild_only: RebuildOnlyOpt, rebuild: bool, select: SelectOpt, raw_selected: list[str]
) -> list[str]:
    rebuild_only_names = _normalize_node_names_or_warn(rebuild_only)
    if rebuild_only_names:
        return [n for n in (rebuild_only or []) if n in REGISTRY.nodes]
    if rebuild and select:
        return raw_selected
    return []


def _maybe_exit_if_empty(wanted: set[str], explicit_targets: list[str]) -> None:
    if not wanted and not explicit_targets:
        typer.secho(
            "Nothing to run (empty selection after applying --select/--exclude).", fg="yellow"
        )
        raise typer.Exit(0)


def _compute_force_rebuild(
    explicit_targets: list[str], rebuild: bool, wanted: set[str]
) -> set[str]:
    if explicit_targets:
        return set(explicit_targets)
    if rebuild:
        return set(wanted)
    return set()


def _levels_for_run(explicit_targets: list[str], wanted: set[str]) -> list[list[str]]:
    if explicit_targets:
        return [explicit_targets]
    sub_nodes = {k: v for k, v in REGISTRY.nodes.items() if k in wanted}
    return dag_levels(sub_nodes)


def _run_schedule(
    engine_: _RunEngine, lvls: list[list[str]], jobs: int | str, keep_going: bool, ctx: CLIContext
) -> tuple[ScheduleResult, LogQueue, str, str]:
    logq = LogQueue()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    bind_context(run_id=started_at)

    # Best-effort: use previous run timings to batch small models per worker.
    try:
        prev_durations_s = load_last_run_durations(ctx.project)
    except Exception:
        prev_durations_s = {}

    def _run_node_with_ctx(name: str) -> None:
        with bound_context(node=name):
            engine_.run_node(name)

    result = schedule(
        lvls,
        jobs=jobs,
        fail_policy="keep_going" if keep_going else "fail_fast",
        run_node=_run_node_with_ctx,
        before=engine_.before,
        on_error=None,
        logger=logq,
        engine_abbr=_abbr(ctx.profile.engine),
        name_width=100,
        name_formatter=engine_.format_run_label,
        durations_s=prev_durations_s,
    )

    finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    return result, logq, started_at, finished_at


def _write_artifacts(
    ctx: CLIContext,
    result: ScheduleResult,
    started_at: str,
    finished_at: str,
    engine_: _RunEngine,
    budgets: dict[str, Any] | None,
) -> None:
    write_manifest(ctx.project)

    node_results: list[RunNodeResult] = []
    failed = result.failed or {}
    all_names = set(result.per_node_s.keys()) | set(failed.keys())

    # stats accumulated by the executor per node (if available)
    per_node_stats = getattr(engine_, "query_stats", {}) or {}

    for name in sorted(all_names):
        dur_s = float(result.per_node_s.get(name, 0.0))
        status = "error" if name in failed else "success"
        msg = str(failed.get(name)) if name in failed else None

        stats = per_node_stats.get(name) or {}
        bytes_scanned = int(stats.get("bytes_scanned", 0))
        rows = int(stats.get("rows", 0))
        q_ms = int(stats.get("query_duration_ms", 0))

        node_results.append(
            RunNodeResult(
                name=name,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=int(dur_s * 1000),
                message=msg,
                http=engine_.http_snaps.get(name),
                bytes_scanned=bytes_scanned,
                rows=rows,
                query_duration_ms=q_ms,
            )
        )

    write_run_results(
        ctx.project,
        started_at=started_at,
        finished_at=finished_at,
        node_results=node_results,
        budgets=budgets,
    )


def _attempt_catalog(ctx: CLIContext) -> None:
    try:
        execu, _, _ = ctx.make_executor()
        write_catalog(ctx.project, execu)
    except Exception:
        pass


def _emit_logs_and_errors(logq: LogQueue, result: ScheduleResult, engine_: _RunEngine) -> None:
    for line in logq.drain():
        echo(line)
    if result.failed:
        for name, err in result.failed.items():
            engine_.on_error(name, err)


# ----------------- run function -----------------


def _run_global_hooks(
    engine_: _RunEngine,
    when: HookWhen,
    *,
    run_status: str | None = None,
    run_stats: RunStatsContext | None = None,
) -> None:
    """
    Execute project-level hooks.on_run_start / hooks.on_run_end.
    """
    if when not in ("on_run_start", "on_run_end"):
        # Safety: global hooks are only defined for these two events
        return

    if when == "on_run_start":
        hooks = getattr(REGISTRY, "on_run_start_hooks", []) or []
    elif when == "on_run_end":
        hooks = getattr(REGISTRY, "on_run_end_hooks", []) or []
    else:
        return

    if not hooks:
        return

    ex, _, _ = engine_._get_runner()
    engine_._run_hooks(
        hooks,
        node=None,
        when=when,
        ex=ex,
        run_status=run_status,
        run_stats=run_stats,
    )


def run(
    project: ProjectArg = ".",
    env_name: EnvOpt = "dev",
    engine: EngineOpt = None,
    vars: VarsOpt = None,
    select: SelectOpt = None,
    exclude: ExcludeOpt = None,
    jobs: JobsOpt = "1",
    keep_going: KeepOpt = False,
    cache: CacheOpt = CacheMode.RW,
    no_cache: NoCacheOpt = False,
    rebuild: RebuildAllOpt = False,
    rebuild_only: RebuildOnlyOpt = None,
    offline: OfflineOpt = False,
    http_cache: HttpCacheOpt = None,
    changed_since: ChangedSinceOpt = None,
) -> None:
    # HTTP/API-Flags → ENV, damit fastflowtransform.api.http sie liest
    if offline:
        os.environ["FF_HTTP_OFFLINE"] = "1"
    if http_cache:
        os.environ["FF_HTTP_CACHE_MODE"] = str(
            http_cache.value if hasattr(http_cache, "value") else http_cache
        )

    ctx, engine_ = _build_engine_ctx(project, env_name, engine, vars, cache, no_cache)

    # Run metadata for hooks
    engine_.invocation_id = uuid4().hex
    engine_.run_started_at = datetime.now(UTC).isoformat(timespec="seconds")

    # ---------- Runtime contracts: load + configure executor ----------
    project_dir = Path(ctx.project)

    # engine_.shared is (executor, run_sql_fn, run_py_fn)
    try:
        executor, _, _ = engine_.shared
    except Exception:
        executor = None

    configure_executor_contracts(project_dir, executor)

    bind_context(
        engine=ctx.profile.engine,
        env=env_name,
        run_id=engine_.run_started_at,
        invocation_id=engine_.invocation_id,
    )

    # Load python hooks from hooks/ directory
    project_dir = ctx.project
    load_project_hooks(project_dir)

    # Global on_run_start hooks
    _run_global_hooks(engine_, when="on_run_start")

    select_tokens, _, raw_selected = _select_predicate_and_raw(engine_, ctx, select)
    wanted = _wanted_names(select_tokens=select_tokens, exclude=exclude, raw_selected=raw_selected)

    wanted = _apply_changed_since_filter(
        ctx=ctx,
        wanted=wanted,
        select=select,
        exclude=exclude,
        changed_since=changed_since,
    )

    explicit_targets = _explicit_targets(rebuild_only, rebuild, select, raw_selected)
    _maybe_exit_if_empty(wanted, explicit_targets)

    engine_.force_rebuild = _compute_force_rebuild(explicit_targets, rebuild, wanted)
    lvls = _levels_for_run(explicit_targets, wanted)

    result, logq, started_at, finished_at = _run_schedule(engine_, lvls, jobs, keep_going, ctx)

    # Evaluate budgets.yml based on collected query stats
    budget_error, budgets_summary = _evaluate_budgets(ctx.project, engine_)

    _write_artifacts(ctx, result, started_at, finished_at, engine_, budgets_summary)

    _attempt_catalog(ctx)
    _emit_logs_and_errors(logq, result, engine_)

    # Compute aggregated row + time totals for hooks
    totals = _aggregate_totals(getattr(engine_, "query_stats", {}) or {})
    rows_total = totals.get("rows", 0)
    elapsed_ms_total = totals.get("query_duration_ms", 0)

    # Global on_run_end hooks (only reached if no model raised fatal error inside schedule)
    has_failures = bool(result.failed)
    run_status = "error" if has_failures or budget_error else "success"

    run_stats: RunStatsContext = {
        "models_built": len(result.per_node_s) - len(result.failed),
        "models_failed": len(result.failed),
        "models_skipped": 0,
        "rows_total": rows_total,
        "elapsed_ms_total": elapsed_ms_total,
        "run_status": run_status,
    }

    _run_global_hooks(
        engine_,
        when="on_run_end",
        run_status=run_status,
        run_stats=run_stats,
    )

    if result.failed or budget_error:
        raise typer.Exit(1)

    engine_.persist_on_success(result)
    engine_.print_timings(result)
    echo("✓ Done")
    clear_context()


def register(app: typer.Typer) -> None:
    app.command(
        help=(
            "Loads the project, builds the DAG, and runs every model."
            "\n\nExample:\n  fft run . --env dev"
        )
    )(run)


__all__ = [
    "CacheMode",
    "register",
    "run",
]
