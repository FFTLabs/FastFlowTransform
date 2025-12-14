# fastflowtransform/incremental.py
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from fastflowtransform.core import relation_for
from fastflowtransform.errors import ModelExecutionError


def _normalize_unique_key(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, (list, tuple)):
        out: list[str] = []
        for x in val:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    return []


def _get_schema_sync_policy(meta: dict | None) -> str:
    """
    Resolve schema sync / on_schema_change policy with backwards compatibility.

    Priority:
      1) top-level schema_sync
      2) incremental.schema_sync (if present as nested config)
      3) legacy on_schema_change

    Normalizes:
      - "none" -> "ignore"
      - invalid values -> "ignore"
    """
    data = meta or {}

    raw = data.get("schema_sync")
    if raw is None:
        incr = data.get("incremental")
        if isinstance(incr, dict):
            raw = incr.get("schema_sync")
    if raw is None:
        raw = data.get("on_schema_change")

    v = str(raw or "ignore").strip().lower()

    if v in {"none", "ignore"}:
        return "ignore"
    if v in {"append_new_columns", "sync_all_columns"}:
        return v
    return "ignore"


def _is_merge_not_supported_error(exc: Exception) -> bool:
    """
    Detect engine messages where MERGE is simply not supported for the target table/catalog.
    In those cases we want to gracefully fall back to a full refresh, instead of failing.
    """
    msg = str(exc)
    text = msg.lower()
    # Databricks / Spark-style messages
    return "merge into table is not supported" in text or "merge into is not supported" in text


# ---------- Helper ----------


def _apply_runtime_contracts_after_incremental(executor: Any, node: Any, relation: str) -> None:
    """
    After an incremental model has been materialized (via create_table_as /
    incremental_insert / incremental_merge), run runtime contracts in
    verify/cast mode if the executor supports them.

    This is intentionally generic and works for any executor that exposes:
      - runtime_contracts
      - _ff_contracts
      - _ff_project_contracts
      - _format_relation_for_ref(name: str) -> str
    """
    runtime = getattr(executor, "runtime_contracts", None)
    if runtime is None:
        return

    contracts = getattr(executor, "_ff_contracts", {}) or {}
    project_contracts = getattr(executor, "_ff_project_contracts", None)

    # How you key contracts may vary slightly; common patterns:
    #   - contracts["customers"]
    #   - contracts[relation_for(node.name)]
    logical = relation_for(node.name)
    contract = contracts.get(logical) or contracts.get(node.name)

    # If there is no per-table contract and no project-level enforcement,
    # there's nothing to do.
    if contract is None and project_contracts is None:
        return

    try:
        physical = executor._format_relation_for_ref(node.name)
    except AttributeError:
        # Fallback: use the logical relation if the executor does not
        # implement the more specific formatting hook.
        physical = relation

    ctx = runtime.build_context(
        node=node,
        relation=logical,
        physical_table=physical,
        contract=contract,
        project_contracts=project_contracts,
        is_incremental=True,
    )

    runtime.verify_after_materialization(ctx=ctx)


def _safe_exists(executor: Any, relation: Any) -> bool:
    try:
        return bool(executor.exists_relation(relation))
    except Exception:
        return False


def _env_with_incremental(jenv: Any, is_incr: bool) -> Any:
    _overlay = getattr(jenv, "overlay", None)
    env = _overlay() if callable(_overlay) else None
    if env is None:

        class _EnvShim:
            def __init__(self, base):
                self._base = base
                self.globals = dict(getattr(base, "globals", {}))

            def __getattr__(self, name):
                return getattr(self._base, name)

        env = _EnvShim(jenv)
    getattr(env, "globals", {}).update({"is_incremental": lambda: is_incr})
    return env


def _render_sql_safe(executor: Any, node: Any, env: Any) -> str:
    try:
        return executor.render_sql(
            node,
            env,
            ref_resolver=lambda nm: executor._resolve_ref(nm, env),
            source_resolver=executor._resolve_source,
        )
    except Exception:
        return executor.render_sql(node, env)


def _wrap_and_raise_factory(node_name: str, relation: Any, rendered_sql: str | None) -> Callable:
    def _wrap_and_raise(e: Exception) -> None:
        tail = rendered_sql[-600:].strip() if rendered_sql else None
        msg = f"{e.__class__.__name__}: {e}"
        raise ModelExecutionError(node_name, relation, msg, sql_snippet=tail) from e

    return _wrap_and_raise


def _maybe_schema_sync(executor: Any, relation: Any, rendered_sql: str, policy: str) -> None:
    if policy in {"append_new_columns", "sync_all_columns"} and hasattr(
        executor, "alter_table_sync_schema"
    ):
        executor.alter_table_sync_schema(relation, rendered_sql, mode=policy)


def _create_table_as_or_replace(executor: Any, relation: Any, rendered_sql: str) -> None:
    _full_refresh_table(executor, relation, rendered_sql)


def _full_refresh_table(executor: Any, relation: Any, rendered_sql: str) -> None:
    """
    Engine-agnostic full refresh:
    - If the executor has a `full_refresh_table(...)` method, it is used.
    - Otherwise: first try `create_table_as`; on failure, fall back to raw SQL
    'create or replace table ... as ...' (for DuckDB, Postgres, Snowflake, etc.).
    """
    full_refresh = getattr(executor, "full_refresh_table", None)
    if callable(full_refresh):
        full_refresh(relation, rendered_sql)
        return

    # Best-effort qualified identifier for engines that expose it (e.g. BigQuery)
    target = relation
    qualify = getattr(executor, "_qualified_identifier", None)
    if callable(qualify):
        try:
            proj = getattr(executor, "project", None)
            dset = getattr(executor, "dataset", None) or getattr(executor, "schema", None)
            if proj is not None or dset is not None:
                target = qualify(relation, project=proj, dataset=dset)
            else:
                target = qualify(relation)
        except Exception:
            target = relation

    try:
        executor.create_table_as(relation, rendered_sql)
    except Exception:
        executor._execute_sql(f"create or replace table {target} as {rendered_sql}")


UniqueKey = str | Sequence[str] | None


def _merge_or_insert_with_fallback(
    executor: Any,
    relation: Any,
    rendered_sql: str,
    unique_key: UniqueKey,
    *,
    fallback_sql: str | None = None,
    on_full_refresh_error: Callable[[Exception], None] | None = None,
) -> None:
    fallback_sql = fallback_sql or rendered_sql

    def _run_full_refresh() -> None:
        try:
            _full_refresh_table(executor, relation, fallback_sql)
        except Exception as exc:
            if on_full_refresh_error is not None:
                on_full_refresh_error(exc)
            else:
                raise

    if unique_key:
        if isinstance(unique_key, str):
            keys: list[str] = [unique_key]
        else:
            keys = list(unique_key)

        try:
            executor.incremental_merge(relation, rendered_sql, keys)
            return
        except Exception as exc:
            # 🔑 Only treat "MERGE not supported" as a soft error → fallback
            if _is_merge_not_supported_error(exc):
                _run_full_refresh()
                return
            # Any other error (e.g. UNRESOLVED_COLUMN, syntax, etc.) is a *real* failure
            raise

    # no unique_key -> insert-only
    try:
        executor.incremental_insert(relation, rendered_sql)
    except Exception:
        _run_full_refresh()


# ---------- Run or dispatch ----------


def run_or_dispatch(executor: Any, node: Any, jenv: Any) -> None:
    """
    Incremental materialization for materialized='incremental'.
    Method called from BaseExecutor.run_sql(...).
    """
    meta = getattr(node, "meta", {}) or {}

    materialized = meta.get("materialized")
    incr_cfg = meta.get("incremental")

    # Determine if incremental is enabled
    incr_enabled = False
    if isinstance(incr_cfg, bool):
        incr_enabled = incr_cfg
    elif isinstance(incr_cfg, dict):
        # default enabled=True if a dict is present unless explicitly disabled
        incr_enabled = incr_cfg.get("enabled", True)

    # Decide whether to treat this as an incremental model
    is_incremental_model = False
    if materialized == "incremental":
        # respect enabled flag if present
        is_incremental_model = incr_enabled if incr_cfg is not None else True
    elif materialized is None and incr_enabled:
        # legacy: "incremental: true" without explicit materialized
        is_incremental_model = True

    relation = relation_for(node.name)

    if not is_incremental_model:
        rendered_sql = _render_sql_safe(executor, node, jenv)
        wrap_and_raise = _wrap_and_raise_factory(node.name, relation, rendered_sql)
        try:
            _create_table_as_or_replace(executor, relation, rendered_sql)
        except Exception as e:
            wrap_and_raise(e)
        return

    exists = _safe_exists(executor, relation)
    env = _env_with_incremental(jenv, exists)

    base_sql = _render_sql_safe(executor, node, env)

    delta_sql = meta.get("delta_sql")
    if exists and isinstance(delta_sql, str) and delta_sql.strip():
        rendered_sql = delta_sql.strip()
    else:
        rendered_sql = base_sql

    fallback_sql = rendered_sql
    if exists:
        non_incr_env = _env_with_incremental(jenv, False)
        fallback_sql = _render_sql_safe(executor, node, non_incr_env)

    wrap_incremental = _wrap_and_raise_factory(node.name, relation, rendered_sql)
    wrap_full_refresh = _wrap_and_raise_factory(node.name, relation, fallback_sql)

    unique_key = _normalize_unique_key(meta.get("unique_key"))
    schema_policy = _get_schema_sync_policy(meta)

    if not exists:
        try:
            _create_table_as_or_replace(executor, relation, fallback_sql)
            # Contracts: first incremental run creates the table → verify schema
            _apply_runtime_contracts_after_incremental(executor, node, relation)
        except Exception as e:
            wrap_full_refresh(e)
        return

    _maybe_schema_sync(executor, relation, rendered_sql, schema_policy)
    try:
        _merge_or_insert_with_fallback(
            executor,
            relation,
            rendered_sql,
            unique_key,
            fallback_sql=fallback_sql,
            on_full_refresh_error=wrap_full_refresh,
        )
        # Contracts: after merge/insert/full-refresh fallback, verify schema
        _apply_runtime_contracts_after_incremental(executor, node, relation)
    except ModelExecutionError:
        # already wrapped; propagate
        raise
    except Exception as e:
        wrap_incremental(e)
