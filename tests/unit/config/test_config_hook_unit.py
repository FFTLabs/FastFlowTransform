from pathlib import Path

import pytest

from fastflowtransform.core import REGISTRY


@pytest.mark.unit
def test_sql_model_config_materialized_view(tmp_path: Path):
    (tmp_path / "models").mkdir()
    (tmp_path / "sources.yml").write_text(
        "version: 1\nsources: []\n",
        encoding="utf-8",
    )
    (tmp_path / "models" / "users.ff.sql").write_text(
        "{{ config(materialized='view') }}\nselect 1 as id, 'x' as email;",
        encoding="utf-8",
    )
    REGISTRY.load_project(tmp_path)
    node = REGISTRY.get_node("users")
    assert node.meta.get("materialized") == "view"
