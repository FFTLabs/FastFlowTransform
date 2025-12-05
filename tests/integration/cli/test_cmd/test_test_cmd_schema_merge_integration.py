from pathlib import Path

import pytest

from fastflowtransform.cli.test_cmd import _apply_legacy_tag_filter, _run_dq_tests
from fastflowtransform.core import REGISTRY
from fastflowtransform.executors.duckdb import DuckExecutor
from fastflowtransform.schema_loader import load_schema_tests


@pytest.mark.integration
@pytest.mark.duckdb
def test_merge_project_yaml_and_schema_yaml(tmp_path: Path):
    # Projekt + Modell
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "project.yml").write_text(
        """
name: test_project
version: "0.1"

tests:
  - type: not_null
    table: users
    column: id
    tags: [legacy]
""",
        encoding="utf-8",
    )
    (tmp_path / "models" / "users.ff.sql").write_text(
        "create or replace table users as select 1 as id, 'x@example.com' as email",
        encoding="utf-8",
    )
    (tmp_path / "models" / "users.yml").write_text(
        """
version: 1
models:
  - name: users.ff
    tags: [schema]
    columns:
      - name: email
        tests:
          - accepted_values:
              values: ["x@example.com"]
              severity: warn
""",
        encoding="utf-8",
    )

    # Build
    REGISTRY.load_project(tmp_path)
    env = REGISTRY.get_env()
    ex = DuckExecutor(":memory:")
    ex.run_sql(REGISTRY.get_node("users.ff"), env)

    legacy = [
        {
            "type": "not_null",
            "table": "users",
            "column": "id",
            "severity": "error",
            "tags": ["legacy"],
        }
    ]
    schema_specs = load_schema_tests(tmp_path)

    legacy_only = _apply_legacy_tag_filter(legacy + schema_specs, ["legacy"], legacy_token=True)
    res_legacy = _run_dq_tests(ex, legacy_only)
    assert all(r.ok for r in res_legacy)

    schema_only = _apply_legacy_tag_filter(legacy + schema_specs, ["schema"], legacy_token=True)
    res_schema = _run_dq_tests(ex, schema_only)
    assert all(r.ok or r.severity == "warn" for r in res_schema)
