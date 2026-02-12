from __future__ import annotations

import typer

from fastflowtransform.cli.bootstrap import _prepare_context
from fastflowtransform.cli.docs_utils import _resolve_dag_out_dir
from fastflowtransform.cli.options import (
    EngineOpt,
    EnvOpt,
    HtmlOpt,
    OutOpt,
    ProjectArg,
    SelectOpt,
    VarsOpt,
    WithSchemaOpt,
)
from fastflowtransform.cli.selectors import _compile_selector
from fastflowtransform.core import REGISTRY
from fastflowtransform.dag import mermaid
from fastflowtransform.docs import render_site
from fastflowtransform.logging import echo, echo_debug


def dag(
    project: ProjectArg = ".",
    env_name: EnvOpt = "dev",
    html: HtmlOpt = False,
    engine: EngineOpt = None,
    out: OutOpt = None,
    vars: VarsOpt = None,
    select: SelectOpt = None,
    with_schema: WithSchemaOpt = True,
) -> None:
    if out is not None:
        out = out.resolve()
        out.mkdir(parents=True, exist_ok=True)

    ctx = _prepare_context(project, env_name, engine, vars)
    dag_out = _resolve_dag_out_dir(ctx.project, out)
    dag_out.mkdir(parents=True, exist_ok=True)

    _, pred = _compile_selector(select)
    filtered_nodes = {k: v for k, v in REGISTRY.nodes.items() if pred(v)}

    if html:
        if (ctx.artifacts_mode or "files").strip().lower() == "db":
            raise typer.BadParameter(
                "HTML DAG generation is disabled when artifacts.mode=db (no asset files allowed)."
            )
        ex, *_ = ctx.make_executor()
        try:
            render_site(dag_out, filtered_nodes, executor=ex, with_schema=with_schema)
        except TypeError:
            render_site(dag_out, filtered_nodes, executor=ex)
        echo(f"HTML-DAG written to {dag_out / 'index.html'}")
    else:
        mm = mermaid(filtered_nodes)
        mmd = dag_out / "dag.mmd"
        mmd.write_text(mm, encoding="utf-8")
        echo(f"Mermaid DAG written to {dag_out}")

    echo_debug(f"Profile: {env_name} | Engine: {ctx.profile.engine}")


def register(app: typer.Typer) -> None:
    app.command(
        help=(
            "Outputs the DAG as Mermaid text or generates an HTML page.\n\nExamples:\n  "
            "fft dag .\n  fft dag . --env dev --html"
        )
    )(dag)


__all__ = ["dag", "register"]
