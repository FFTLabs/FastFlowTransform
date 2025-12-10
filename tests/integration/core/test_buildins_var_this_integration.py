# tests/test_builtins_var_this.py
from pathlib import Path

import pytest

from fastflowtransform.core import REGISTRY
from fastflowtransform.executors.duckdb import DuckExecutor


@pytest.mark.duckdb
@pytest.mark.integration
def test_var_overrides_and_this_object(tmp_path: Path):
    # Arrange: project with project.yml vars and a simple model using var() and this
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "project.yml").write_text(
        "name: test_var_this\nversion: '0.1'\nvars:\n  day: '2025-10-01'\n  limit: 5\n",
        encoding="utf-8",
    )
    # Model prints var('day') and this.name
    (tmp_path / "models" / "m.ff.sql").write_text(
        "select '{{ var(\"day\") }}' as d, '{{ this.name }}' as rel limit {{ var('limit', 10) }};",
        encoding="utf-8",
    )

    # Load and override CLI vars
    REGISTRY.load_project(tmp_path)
    REGISTRY.set_cli_vars({"day": "2099-01-01"})  # CLI override wins

    env = REGISTRY.get_env()
    ex = DuckExecutor(db_path=":memory:")
    node = REGISTRY.nodes["m.ff"]
    ex.run_sql(node, env)

    # Verify the rendered result used CLI override and this.name == physical relation
    row = ex.con.execute("select * from m").fetchone()
    assert row is not None
    assert row[0] == "2099-01-01"
    assert row[1] == '"m"'
