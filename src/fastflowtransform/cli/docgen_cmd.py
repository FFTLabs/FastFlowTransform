# fastflowtransform/cli/docgen_cmd.py
from __future__ import annotations

import json
import webbrowser
from contextlib import suppress
from pathlib import Path
from typing import Annotated

import typer

from fastflowtransform.cli.bootstrap import _prepare_context
from fastflowtransform.cli.docs_utils import _build_docs_manifest, _resolve_dag_out_dir
from fastflowtransform.cli.options import (
    EngineOpt,
    EnvOpt,
    OutOpt,
    ProjectArg,
    VarsOpt,
)
from fastflowtransform.core import REGISTRY
from fastflowtransform.docs import render_site
from fastflowtransform.logging import echo, echo_debug


def docgen(
    project: ProjectArg = ".",
    env_name: EnvOpt = "dev",
    engine: EngineOpt = None,
    vars: VarsOpt = None,
    out: OutOpt = None,
    emit_json: Annotated[
        Path | None, typer.Option("--emit-json", help="Write docs manifest JSON to this file")
    ] = None,
    open_source: Annotated[
        bool, typer.Option("--open-source", help="Open generated index.html in the default browser")
    ] = False,
) -> None:
    if out is not None:
        out = out.resolve()
        out.mkdir(parents=True, exist_ok=True)

    ctx = _prepare_context(project, env_name, engine, vars)
    if (ctx.artifacts_mode or "files").strip().lower() == "db":
        raise typer.BadParameter(
            "Docs generation is disabled when artifacts.mode=db (no asset files allowed)."
        )
    ex, *_ = ctx.make_executor()
    dag_out = _resolve_dag_out_dir(ctx.project, out)
    dag_out.mkdir(parents=True, exist_ok=True)

    render_site(dag_out, REGISTRY.nodes, executor=ex)
    echo(f"HTML docs written to {dag_out / 'index.html'}")

    if emit_json is not None:
        manifest = _build_docs_manifest(
            project_dir=ctx.project,
            nodes=REGISTRY.nodes,
            executor=ex,
            env_name=env_name,
        )
        emit_json = emit_json.resolve()
        emit_json.parent.mkdir(parents=True, exist_ok=True)
        emit_json.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        echo(f"Docs manifest JSON written to {emit_json}")

    if open_source:
        index_path = (dag_out / "index.html").as_uri()
        with suppress(Exception):
            webbrowser.open(index_path, new=2)

    echo_debug(f"Profile: {env_name} | Engine: {ctx.profile.engine}")


def register(app: typer.Typer) -> None:
    app.command(
        help=(
            "Generate documentation site (like 'dag --html') and optionally "
            "emit a JSON manifest.\n\n"
            "Examples:\n"
            "  fft docgen . --env dev --out site/docs\n"
            "  fft docgen . --env dev --out site/docs --emit-json docs_manifest.json "
            "  --open-source\n"
        )
    )(docgen)


__all__ = ["docgen", "register"]
