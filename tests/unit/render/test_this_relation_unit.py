# tests/unit/render/test_this_relation_unit.py
import pytest
from jinja2 import Environment, FileSystemLoader

from fastflowtransform.core import Node
from fastflowtransform.executors.duckdb import DuckExecutor


def _env_for_tests() -> Environment:
    return Environment(
        loader=FileSystemLoader(["."]),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


@pytest.mark.unit
def test_this_renders_physical_relation(tmp_path):
    sql_path = tmp_path / "m.ff.sql"
    sql_path.write_text("select '{{ this }}' as rel\n", encoding="utf-8")

    node = Node(name="m.ff", kind="sql", path=sql_path)

    env = _env_for_tests()
    ex = DuckExecutor()

    # Act
    rendered = ex.render_sql(node, env).strip()

    # Assert
    assert rendered.lower() == "select '\"m\"' as rel"
