# fastflowtransform/cli/test_cmd.py
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from fastflowtransform.cli.bootstrap import _prepare_context, configure_executor_contracts
from fastflowtransform.cli.options import (
    EngineOpt,
    EnvOpt,
    ProjectArg,
    SelectOpt,
    SkipBuildOpt,
    VarsOpt,
)
from fastflowtransform.cli.selectors import _compile_selector
from fastflowtransform.config.project import (
    BaseProjectTestConfig,
    parse_project_yaml_config,
)
from fastflowtransform.contracts.core import load_contract_tests
from fastflowtransform.core import REGISTRY
from fastflowtransform.dag import topo_sort
from fastflowtransform.errors import ModelExecutionError
from fastflowtransform.executors.base import BaseExecutor
from fastflowtransform.logging import echo
from fastflowtransform.schema_loader import Severity, TestSpec, load_schema_tests
from fastflowtransform.testing.discovery import (
    discover_python_tests,
    discover_sql_tests,
)
from fastflowtransform.testing.registry import TESTS, Runner


@dataclass
class DQResult:
    kind: str
    table: str
    column: str | None
    ok: bool
    msg: str | None
    ms: int
    severity: Severity = "error"
    param_str: str = ""
    example_sql: str | None = None


_REF_CALL_RE = re.compile(r"^ref\(\s*(['\"])([^'\"]+)\1\s*\)$")


def _registry_env() -> Any | None:
    env = getattr(REGISTRY, "env", None)
    if env is not None:
        return env
    if hasattr(REGISTRY, "get_env"):
        try:
            return REGISTRY.get_env()
        except Exception:
            return None
    return None


def _is_snapshot_model(node: Any) -> bool:
    """
    Return True if this node is a snapshot model (materialized='snapshot').
    """
    meta = getattr(node, "meta", {}) or {}
    mat = str(meta.get("materialized") or "").lower()
    return mat == "snapshot"


def _print_model_error_block(node_name: str, relation: str, message: str, sql: str | None) -> None:
    header = "┌" + "─" * 70
    footer = "└" + "─" * 70
    echo(header)
    echo(f"│ Model: {node_name}  (relation: {relation})")
    echo(f"│ Error: {message}")
    if sql:
        echo("│ SQL (tail):")
        for line in sql.splitlines():
            echo("│   " + line)
    echo(footer)


def _execute_models(
    order: Iterable[str],
    run_sql: Callable[[Any], Any],
    run_py: Callable[[Any], Any],
    *,
    before: Callable[[str, Any], None] | None = None,
    on_error: Callable[[str, Any, Exception], None] | None = None,
) -> None:
    for name in order:
        node = REGISTRY.nodes[name]
        if before:
            before(name, node)
        try:
            (run_sql if node.kind == "sql" else run_py)(node)
        except Exception as exc:
            if on_error is None:
                # Convert known domain error to friendly output
                if isinstance(exc, ModelExecutionError):
                    _print_model_error_block(exc.node_name, exc.relation, str(exc), exc.sql_snippet)
                    raise typer.Exit(1) from exc
                raise Exception from exc
            on_error(name, node, exc)


def _run_models(
    pred: Callable[[Any], bool],
    run_sql: Callable[[Any], Any],
    run_py: Callable[[Any], Any],
    *,
    before: Callable[[str, Any], None] | None = None,
    on_error: Callable[[str, Any, Exception], None] | None = None,
) -> None:
    order = [
        n
        for n in topo_sort(REGISTRY.nodes)
        if pred(REGISTRY.nodes[n]) and not _is_snapshot_model(REGISTRY.nodes[n])
    ]
    _execute_models(order, run_sql, run_py, before=before, on_error=on_error)


def _load_tests(proj: Path) -> list[Any]:
    """
    Load project-level tests from project.yml and validate them via Pydantic
    (ProjectConfig.tests → list[ProjectTestConfig]).
    """
    cfg_path = proj / "project.yml"
    if not cfg_path.exists():
        return []
    proj_cfg = parse_project_yaml_config(proj)
    # proj_cfg.tests is already a list[ProjectTestConfig]
    return list(proj_cfg.tests or [])


def _is_legacy_test_token(tokens: list[str]) -> bool:
    return len(tokens) == 1 and not tokens[0].startswith(("tag:", "type:", "kind:"))


def _apply_legacy_tag_filter(
    tests: list[Any], tokens: list[str], *, legacy_token: bool
) -> list[Any]:
    if not legacy_token:
        return tests
    legacy_tag = tokens[0]

    def has_tag(t: Any) -> bool:
        # Dict (old format; kept for backwards compatibility)
        if isinstance(t, dict):
            tags = t.get("tags") or []
            return (legacy_tag in tags) if isinstance(tags, list) else (legacy_tag == tags)
        # Schema YAML tests
        if isinstance(t, TestSpec):
            return legacy_tag in (t.tags or [])
        # Project.yml tests validated via Pydantic
        if isinstance(t, BaseProjectTestConfig):
            return legacy_tag in (t.tags or [])
        return False

    return [t for t in tests if has_tag(t)]


def _fmt_table(value: Any, executor: Any) -> Any:
    if executor is None or not hasattr(executor, "_format_test_table"):
        return value
    return executor._format_test_table(value)


def _fmt_reconcile_side(side: Any, executor: Any) -> Any:
    if not isinstance(side, dict):
        return side
    side_fmt = dict(side)
    tbl = side_fmt.get("table")
    if tbl is not None:
        side_fmt["table"] = _fmt_table(tbl, executor)
    return side_fmt


def _resolve_relationship_target_table(value: Any, executor: Any) -> tuple[str | None, str | None]:
    """Return (display_value, table_for_exec) for relationships.to."""
    if value is None:
        return None, None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return "", ""
        m = _REF_CALL_RE.match(stripped)
        if m:
            env = _registry_env()
            if env is None:
                raise typer.BadParameter(
                    "ref('...') requires a loaded project/environment (registry env missing)."
                )
            if not hasattr(executor, "_resolve_ref"):
                raise typer.BadParameter("Current executor cannot resolve ref('...') in tests.")
            ref_target = m.group(2)
            try:
                resolved = executor._resolve_ref(ref_target, env)
            except Exception as exc:
                raise typer.BadParameter(f"Failed to resolve ref('{ref_target}'): {exc}") from exc
            return stripped, resolved
        return stripped, _fmt_table(stripped, executor)
    literal = str(value)
    return literal, _fmt_table(literal, executor)


def _prepare_relationship_params(
    params: dict[str, Any], executor: Any
) -> tuple[dict[str, Any], str | None]:
    """Normalize params for relationships tests; returns new params + formatted parent label."""
    normalized = dict(params or {})
    target_display: str | None = None
    if "to" in normalized:
        display, resolved = _resolve_relationship_target_table(normalized.get("to"), executor)
        if resolved:
            normalized["_to_relation"] = resolved
        if display or resolved:
            target_display = display or resolved
            normalized["_to_display"] = target_display
    return normalized, target_display


def _relationships_display(child: str, parent: str | None) -> str:
    parent_label = parent or "<target>"
    return f"{child} ⇒ {parent_label}"


def _prepare_test_from_spec(
    t: TestSpec, executor: Any
) -> tuple[str, Any, Severity, dict[str, Any], Any, Any]:
    """
    Normalize a TestSpec into (kind, column, severity, params, display_table, table_for_exec)
    """
    kind = t.type
    col = t.column
    severity: Severity = t.severity
    params: dict[str, Any] = dict(t.params or {})

    table_for_exec = _fmt_table(t.table, executor)
    if not isinstance(table_for_exec, str) or not table_for_exec:
        raise typer.BadParameter("Missing or invalid 'table' in test config")
    display_table = table_for_exec

    if kind.startswith("reconcile_"):
        params = dict(params)  # copy so we don't mutate original
        for key in ("left", "right", "source", "target"):
            side = params.get(key)
            if isinstance(side, dict):
                params[key] = _fmt_reconcile_side(side, executor)
    elif kind == "relationships":
        params, parent_display = _prepare_relationship_params(params, executor)
        display_table = _relationships_display(table_for_exec, parent_display)

    return kind, col, severity, params, display_table, table_for_exec


def _prepare_test_from_mapping(
    t: Mapping[str, Any], executor: Any
) -> tuple[str, Any, Severity, dict[str, Any], Any, Any]:
    """
    Normalize a dict-like test into (kind, column, severity, params, display_table, table_for_exec)
    """
    kind = t["type"]
    _sev = str(t.get("severity", "error")).lower()
    severity: Severity = "warn" if _sev == "warn" else "error"

    # Prefer nested `params:` block (for custom tests), fall back to flat fields
    if isinstance(t.get("params"), Mapping):
        params: dict[str, Any] = dict(t["params"])
    else:
        params = dict(t)

    # Strip meta fields that belong to the test spec, not the runner
    META_KEYS = {"type", "table", "column", "severity", "tags", "name"}
    for k in META_KEYS:
        params.pop(k, None)
    col = t.get("column")

    if kind.startswith("reconcile_"):
        if isinstance(t.get("left"), dict) and isinstance(t.get("right"), dict):
            lt = (t.get("left") or {}).get("table")
            rt = (t.get("right") or {}).get("table")
            display_table = f"{lt} ⇔ {rt}"
        elif isinstance(t.get("source"), dict) and isinstance(t.get("target"), dict):
            st = (t.get("source") or {}).get("table")
            tt = (t.get("target") or {}).get("table")
            display_table = f"{st} ⇒ {tt}"
        else:
            display_table = "<reconcile>"

        table_for_exec = _fmt_table(t.get("table"), executor)

        for key in ("left", "right", "source", "target"):
            side = params.get(key)
            if isinstance(side, dict):
                params[key] = _fmt_reconcile_side(side, executor)
    elif kind == "relationships":
        table_for_exec = _fmt_table(t.get("table"), executor)
        if not isinstance(table_for_exec, str) or not table_for_exec:
            raise typer.BadParameter("Missing or invalid 'table' in test config")
        params, parent_display = _prepare_relationship_params(params, executor)
        display_table = _relationships_display(table_for_exec, parent_display)
    else:
        table_for_exec = _fmt_table(t.get("table"), executor)
        if not isinstance(table_for_exec, str) or not table_for_exec:
            raise typer.BadParameter("Missing or invalid 'table' in test config")
        display_table = table_for_exec

    return kind, col, severity, params, display_table, table_for_exec


def _prepare_test(
    raw_test: Any, executor: Any
) -> tuple[str, Any, Severity, dict[str, Any], Any, Any]:
    """
    Dispatcher that normalizes:
      - TestSpec (schema.yml)
      - ProjectTestConfig (Pydantic from project.yml)
      - dict-like legacy tests
    """
    if isinstance(raw_test, TestSpec):
        return _prepare_test_from_spec(raw_test, executor)

    if isinstance(raw_test, BaseProjectTestConfig):
        # Convert to plain dict and reuse the existing mapping-based logic.
        data = raw_test.model_dump(exclude_none=True)
        return _prepare_test_from_mapping(data, executor)

    # Fallback: old dict-style tests (if any remain)
    return _prepare_test_from_mapping(raw_test, executor)


def _run_dq_tests(executor: BaseExecutor, tests: Iterable[Any]) -> list[DQResult]:
    results: list[DQResult] = []

    for raw_test in tests:
        (
            kind,
            col,
            severity,
            params,
            display_table,
            table_for_exec,
        ) = _prepare_test(raw_test, executor)

        t0 = time.perf_counter()

        runner: Runner | None = TESTS.get(kind)
        if runner is None:
            # Unknown test type → treat as configuration failure
            err_msg = (
                f"Unknown test type {kind!r}. "
                "Register a custom runner or fix the 'type' in project.yml/schema.yml."
            )
            ms = int((time.perf_counter() - t0) * 1000)
            param_str = _format_params_for_summary(kind, params)
            results.append(
                DQResult(
                    kind=kind,
                    table=str(display_table),
                    column=col,
                    ok=False,
                    msg=err_msg,
                    ms=ms,
                    severity=severity,
                    param_str=param_str,
                    example_sql=None,
                )
            )
            continue

        ok, msg, example = runner(executor, table_for_exec, col, params)
        ms = int((time.perf_counter() - t0) * 1000)
        param_str = _format_params_for_summary(kind, params)

        results.append(
            DQResult(
                kind=kind,
                table=str(display_table),
                column=col,
                ok=ok,
                msg=msg,
                ms=ms,
                severity=severity,
                param_str=param_str,
                example_sql=example,
            )
        )

    return results


def _print_summary(results: list[DQResult]) -> None:
    passed = sum(1 for r in results if r.ok)
    failed = sum((not r.ok) and (r.severity != "warn") for r in results)
    warned = sum((not r.ok) and (r.severity == "warn") for r in results)

    echo("\nData Quality Summary")
    echo("────────────────────")
    for r in results:
        mark = "✅" if r.ok else "❕" if r.severity == "warn" else "❌"
        scope = f"{r.table}" + (f".{r.column}" if r.column else "")
        kind_with_params = f"{r.kind}"
        if r.param_str:
            kind_with_params += f" {r.param_str}"
        echo(f"{mark} {kind_with_params:<28} {scope:<40} ({r.ms}ms)")
        if not r.ok and r.msg:
            echo(f"   ↳ {r.msg}")
        if not r.ok and r.example_sql:
            echo(f"   ↳ e.g. SQL: {r.example_sql}")

    echo("\nTotals")
    echo("──────")
    echo(f"✓ passed: {passed}")
    if warned:
        echo(f"! warned: {warned}")
    echo(f"✗ failed: {failed}")


def _format_params_for_summary(kind: str, params: dict[str, Any]) -> str:
    """Format a short, readable parameter snippet for the summary line."""
    if not params:
        return ""
    hidden_keys = {"type", "table", "severity", "tags", "name"}

    def _skip(key: str) -> bool:
        return key in hidden_keys or key.startswith("_")

    # Common keys first for stable display
    keys = []
    if "column" in params and not _skip("column"):
        keys.append("column")
    if "values" in params and not _skip("values"):
        keys.append("values")
    if "where" in params and not _skip("where"):
        keys.append("where")
    # Add remaining keys deterministically
    for k in sorted(params.keys()):
        if k not in keys and not _skip(k):
            keys.append(k)
    parts: list[str] = []
    for k in keys:
        v = params.get(k)
        preview_len = 4
        if k == "values" and isinstance(v, list):
            preview = v if len(v) <= preview_len else [*v[:3], "…"]
            parts.append(f"values={preview}")
        elif v is not None:
            parts.append(f"{k}={v}")
    return "(" + ", ".join(parts) + ")" if parts else ""


def test(
    project: ProjectArg = ".",
    env_name: EnvOpt = "dev",
    engine: EngineOpt = None,
    vars: VarsOpt = None,
    select: SelectOpt = None,
    skip_build: SkipBuildOpt = True,
) -> None:
    ctx = _prepare_context(project, env_name, engine, vars)
    tokens, pred = _compile_selector(select)
    has_model_matches = any(pred(node) for node in REGISTRY.nodes.values())
    legacy_tag_only = _is_legacy_test_token(tokens) and not has_model_matches
    execu, run_sql, run_py = ctx.make_executor()
    configure_executor_contracts(ctx.project, execu)

    model_pred = (lambda _n: True) if legacy_tag_only else pred
    # Run models; if a model fails, show friendly error then exit(1).
    if not skip_build:
        _run_models(model_pred, run_sql, run_py)

    # Discover custom DQ tests (SQL + Python) under project/tests/
    discover_sql_tests(ctx.project)
    discover_python_tests(ctx.project)

    # 1) project.yml tests
    tests: list[Any] = _load_tests(ctx.project)
    # 2) schema YAML tests
    tests.extend(load_schema_tests(ctx.project))
    # 2b) contracts tests (contracts/*.contracts.yml)
    tests.extend(load_contract_tests(ctx.project))
    # 3) optional legacy tagfilter (e.g., "batch")
    tests = _apply_legacy_tag_filter(tests, tokens, legacy_token=legacy_tag_only)
    if not tests:
        typer.secho("No tests configured.", fg="bright_black")
        raise typer.Exit(code=0)

    results = _run_dq_tests(execu, tests)
    _print_summary(results)

    # Exit code: count only ERROR fails
    failed = sum((not r.ok) and (r.severity != "warn") for r in results)
    raise typer.Exit(code=2 if failed > 0 else 0)


def register(app: typer.Typer) -> None:
    app.command(
        help=(
            "Materializes models and runs configured data-quality checks."
            "\n\nExample:\n  fft test . --env dev --select batch"
        )
    )(test)


__all__ = [
    "DQResult",
    "register",
    "test",
]
