# fastflowtransform/cli/__init__.py
from __future__ import annotations

import typer

from fastflowtransform import __version__
from fastflowtransform.cache import FingerprintCache
from fastflowtransform.cli.bootstrap import (
    CLIContext,
    _die,
    _load_project_and_env,
    _make_executor,
    _parse_cli_vars,
    _prepare_context,
    _resolve_profile,
    _resolve_project_path,
)
from fastflowtransform.cli.ci_cmd import register as _register_ci
from fastflowtransform.cli.dag_cmd import dag, register as _register_dag
from fastflowtransform.cli.deps_cmd import register as _register_deps
from fastflowtransform.cli.docgen_cmd import docgen, register as _register_docgen
from fastflowtransform.cli.docs_utils import (
    _build_docs_manifest,
    _infer_sql_ref_aliases,
    _resolve_dag_out_dir,
    _strip_html,
)
from fastflowtransform.cli.init_cmd import init, register as _register_init
from fastflowtransform.cli.options import (
    CacheMode,
    CacheOpt,
    CaseOpt,
    EngineOpt,
    EnvOpt,
    ExcludeOpt,
    HtmlOpt,
    JobsOpt,
    KeepOpt,
    ModelOpt,
    NoCacheOpt,
    OutOpt,
    PathOpt,
    ProjectArg,
    RebuildAllOpt,
    RebuildOnlyOpt,
    ReuseMetaOpt,
    SelectOpt,
    UTestCacheMode,
    UTestCacheOpt,
    VarsOpt,
    WithSchemaOpt,
)
from fastflowtransform.cli.run import (
    _RunEngine,
    register as _register_run,
    run,
)
from fastflowtransform.cli.seed_cmd import register as _register_seed, seed
from fastflowtransform.cli.selectors import (
    _build_predicates,
    _compile_selector,
    _parse_select,
    _selected_subgraph_names,
    _selector,
)
from fastflowtransform.cli.snapshot_cmd import register as _register_snapshot, snapshot
from fastflowtransform.cli.source_cmd import register as _register_source
from fastflowtransform.cli.sync_db_comments_cmd import (
    _pg_fq_table,
    _pg_quote_ident,
    _sf_fq_table,
    _sql_literal,
    _strip_html_for_comment,
    _sync_comments_postgres,
    _sync_comments_snowflake,
    register as _register_sync_db_comments,
    sync_db_comments,
)
from fastflowtransform.cli.test_cmd import DQResult, register as _register_test, test
from fastflowtransform.cli.utest_cmd import register as _register_utest, utest
from fastflowtransform.dag import levels as dag_levels, topo_sort
from fastflowtransform.docs import render_site
from fastflowtransform.logging import echo, setup_from_cli_flags
from fastflowtransform.run_executor import ScheduleResult, schedule

app = typer.Typer(
    name="fft",
    help="FastFlowTransform - kleine ELT/DAG-Engine (SQL  Python)",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool | None) -> None:
    if value:
        echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Increase verbosity (-v: INFO, -vv: DEBUG)"
    ),
    quiet: int = typer.Option(0, "--quiet", "-q", count=True, help="Reduce verbosity (-q: ERROR)"),
    sql_debug: bool = typer.Option(False, "--sql-debug", help="Enable SQL debug logging"),
    log_json: bool = typer.Option(False, "--log-json", help="Emit logs as JSON"),
) -> None:
    setup_from_cli_flags(
        verbose=verbose,
        quiet=quiet,
        json=log_json,
        sql_debug=sql_debug,
        to_stderr=False,
    )


_register_run(app)
_register_dag(app)
_register_test(app)
_register_seed(app)
_register_utest(app)
_register_docgen(app)
_register_sync_db_comments(app)
_register_init(app)
_register_snapshot(app)
_register_source(app)
_register_ci(app)
_register_deps(app)


__all__ = [
    "CLIContext",
    "CacheMode",
    "CacheOpt",
    "CaseOpt",
    "DQResult",
    "EngineOpt",
    "EnvOpt",
    "ExcludeOpt",
    "FingerprintCache",
    "HtmlOpt",
    "JobsOpt",
    "KeepOpt",
    "ModelOpt",
    "NoCacheOpt",
    "OutOpt",
    "PathOpt",
    "ProjectArg",
    "RebuildAllOpt",
    "RebuildOnlyOpt",
    "ReuseMetaOpt",
    "ScheduleResult",
    "SelectOpt",
    "UTestCacheMode",
    "UTestCacheOpt",
    "VarsOpt",
    "WithSchemaOpt",
    "_RunEngine",
    "_build_docs_manifest",
    "_build_predicates",
    "_compile_selector",
    "_die",
    "_infer_sql_ref_aliases",
    "_load_project_and_env",
    "_make_executor",
    "_parse_cli_vars",
    "_parse_select",
    "_pg_fq_table",
    "_pg_quote_ident",
    "_prepare_context",
    "_resolve_dag_out_dir",
    "_resolve_profile",
    "_resolve_project_path",
    "_selected_subgraph_names",
    "_selector",
    "_sf_fq_table",
    "_sql_literal",
    "_strip_html",
    "_strip_html_for_comment",
    "_sync_comments_postgres",
    "_sync_comments_snowflake",
    "app",
    "dag",
    "dag_levels",
    "docgen",
    "init",
    "render_site",
    "run",
    "schedule",
    "seed",
    "snapshot",
    "sync_db_comments",
    "test",
    "topo_sort",
    "utest",
]
