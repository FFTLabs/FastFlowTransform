# fastflowtransform/cli/utest_cmd.py
from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime

import typer

from fastflowtransform.artifacts import UTestResult, write_utest_results
from fastflowtransform.cli.bootstrap import _prepare_context
from fastflowtransform.cli.options import (
    CaseOpt,
    EngineOpt,
    EnvOpt,
    ModelOpt,
    PathOpt,
    ProjectArg,
    ReuseMetaOpt,
    UTestCacheMode,
    UTestCacheOpt,
    VarsOpt,
)
from fastflowtransform.logging import echo
from fastflowtransform.utest import discover_unit_specs, run_unit_specs


def utest(
    project: ProjectArg = ".",
    model: ModelOpt = None,
    case: CaseOpt = None,
    env_name: EnvOpt = "dev",
    engine: EngineOpt = None,
    path: PathOpt = None,
    vars: VarsOpt = None,
    cache: UTestCacheOpt = UTestCacheMode.OFF,
    reuse_meta: ReuseMetaOpt = False,
) -> None:
    ctx = _prepare_context(project, env_name, engine, vars, utest=True)
    ex, _, _ = ctx.make_executor()

    specs = discover_unit_specs(ctx.project, path=path, only_model=model)
    if not specs:
        echo("ℹ️  No unit tests found (tests/unit/*.yml).")  # noqa: RUF001
        raise typer.Exit(0)

    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    collected: list[dict] = []

    failures = run_unit_specs(
        specs,
        ex,
        ctx.jinja_env,
        only_case=case,
        cache_mode=getattr(cache, "value", str(cache)) if cache is not None else "off",
        reuse_meta=bool(reuse_meta),
        results_out=collected,
    )
    finished_at = datetime.now(UTC).isoformat(timespec="seconds")

    # Write artifact for docs (best-effort; never block exit)
    with suppress(Exception):
        write_utest_results(
            ctx.project,
            started_at=started_at,
            finished_at=finished_at,
            failures=failures,
            engine=getattr(ex, "engine_name", None),
            results=[
                UTestResult(
                    model=str(r.get("model") or ""),
                    case=str(r.get("case") or ""),
                    status=str(r.get("status") or ""),
                    duration_ms=int(r.get("duration_ms") or 0),
                    cache_hit=bool(r.get("cache_hit")),
                    message=(str(r.get("message")) if r.get("message") else None),
                    target_relation=(
                        str(r.get("target_relation")) if r.get("target_relation") else None
                    ),
                    spec_path=str(r.get("spec_path") or ""),
                )
                for r in collected
                if (r.get("model") and r.get("case"))
            ],
        )

    raise typer.Exit(code=2 if failures > 0 else 0)


def register(app: typer.Typer) -> None:
    app.command()(utest)


__all__ = ["register", "utest"]
