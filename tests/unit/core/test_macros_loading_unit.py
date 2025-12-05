import textwrap
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from fastflowtransform.core import REGISTRY


# ----------------------- SQL Macros ---------------------------------
@pytest.mark.unit
def test_macros_are_loaded_and_callable(tmp_path: Path):
    models = tmp_path / "models" / "macros"
    models.mkdir(parents=True, exist_ok=True)
    (tmp_path / "sources.yml").write_text(
        "version: 1\nsources: []\n",
        encoding="utf-8",
    )

    # A tiny macro file
    (models / "utils.sql").write_text(
        "{% macro greet(name) -%}hello {{ name }}{%- endmacro %}\n",
        encoding="utf-8",
    )

    # A model that uses the macro directly
    user_sql = tmp_path / "models" / "m.ff.sql"
    user_sql.write_text(
        "select '{{ greet(\"world\") }}' as msg;",
        encoding="utf-8",
    )

    REGISTRY.load_project(tmp_path)
    # Macro should be globally available in REGISTRY.env
    env = REGISTRY.get_env()
    assert "greet" in env.globals

    # Render the model to verify the macro actually runs
    tmpl_env = Environment(
        loader=FileSystemLoader(str(tmp_path / "models")),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # ensure globals are visible if you ever use a fresh Environment:
    tmpl_env.globals.update(env.globals)
    sql = tmpl_env.get_template("m.ff.sql").render(ref=lambda n: n, source=lambda a, b: b)
    assert "hello world" in sql

    # Macro inventory available for docs
    assert "greet" in REGISTRY.macros
    assert REGISTRY.macros["greet"].name == "utils.sql"


# ----------------------- Python Macros ---------------------------------
def write(p: Path, s: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).strip() + "\n", encoding="utf-8")


@pytest.mark.unit
def test_python_macros_are_loaded_and_callable(tmp_path: Path):
    proj = tmp_path
    models = proj / "models"
    # Python helpers
    write(
        models / "macros_py" / "text.py",
        """
        def snake(s: str) -> str:
            import re
            return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    """,
    )
    # One trivial SQL model that uses the python macro
    write(
        models / "demo.ff.sql",
        """
        create or replace table demo as
        select '{{ snake("Hello World!") }}' as v
    """,
    )

    REGISTRY.load_project(proj)
    env = REGISTRY.get_env()

    # 1) macro is registered globally
    assert "snake" in env.globals
    assert "snake" in env.filters

    # 2) rendering test: macro call is expanded in template
    t = env.get_template("demo.ff.sql")
    sql = t.render(ref=lambda n: n, source=lambda s, t: t)
    assert "hello_world" in sql
