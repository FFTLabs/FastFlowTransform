# fastflowtransform/cli/source_cmd.py
from __future__ import annotations

import typer

from fastflowtransform.cli.bootstrap import _prepare_context
from fastflowtransform.cli.options import EngineOpt, EnvOpt, ProjectArg, VarsOpt
from fastflowtransform.logging import bind_context, clear_context, echo
from fastflowtransform.source_freshness import SourceFreshnessResult, run_source_freshness
from fastflowtransform.utils.timefmt import format_duration_minutes


def freshness(
    project: ProjectArg = ".",
    env_name: EnvOpt = "dev",
    engine: EngineOpt = None,
    vars: VarsOpt = None,
) -> None:
    """
    Check source-table freshness as configured in sources.yml.

    Usage:
        fft source freshness .
        fft source freshness . --env prod --engine duckdb
    """
    # Build CLIContext (loads project, registry, env, profile, etc.)
    ctx = _prepare_context(project, env_name, engine, vars)
    bind_context(engine=ctx.profile.engine, env=env_name)

    echo(f"[FFT] Profile: {env_name} | Engine: {ctx.profile.engine}")

    # Get a live connection / executor from the context
    execu, _run_sql, _run_py = ctx.make_executor()

    # Run freshness checks over all sources with a configured freshness block
    results: list[SourceFreshnessResult] = run_source_freshness(
        execu,
        engine=ctx.profile.engine,
    )

    if not results:
        echo("No sources with freshness configuration found in sources.yml.")
        clear_context()
        return

    # Sort for stable output
    results = sorted(results, key=lambda r: (r.source_name, r.table_name))

    echo("")
    echo("Source Freshness Summary")
    echo("───────────────────────")

    header = (
        f"{'source.table':<30}  {'status':<7}  {'delay_min':>9}  {'warn_min':>9}  {'error_min':>9}"
    )
    echo(header)

    any_error = False

    for r in results:
        key = f"{r.source_name}.{r.table_name}"
        sym = {"pass": "✓", "warn": "!", "error": "✖"}.get(r.status, "?")
        delay = format_duration_minutes(r.delay_minutes)
        warn_after = "-" if r.warn_after_minutes is None else str(r.warn_after_minutes)
        err_after = "-" if r.error_after_minutes is None else str(r.error_after_minutes)

        echo(f"{key:<30}  {sym + ' ' + r.status:<7}  {delay:>9}  {warn_after:>9}  {err_after:>9}")

        if r.error:
            # Indent the error line for readability
            echo(f"    ↳ {r.error}")

        if r.status == "error":
            any_error = True

    clear_context()

    if any_error:
        # Non-zero exit code if any source is in ERROR state
        raise typer.Exit(1)
    return


def register(app: typer.Typer) -> None:
    """
    Attach 'source freshness' as a sub-command, analogous to how run/test are registered.
    """
    source_app = typer.Typer(
        name="source",
        help="Source metadata utilities (freshness checks, etc.).",
        no_args_is_help=True,
        add_completion=False,
    )

    source_app.command(
        "freshness",
        help="Run freshness checks defined in sources.yml.",
    )(freshness)

    app.add_typer(source_app, name="source")


__all__ = ["freshness", "register"]
