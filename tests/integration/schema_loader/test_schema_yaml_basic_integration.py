from pathlib import Path

import pytest

from fastflowtransform.cli.test_cmd import _apply_legacy_tag_filter, _run_dq_tests
from fastflowtransform.core import REGISTRY
from fastflowtransform.executors.duckdb import DuckExecutor
from fastflowtransform.schema_loader import load_schema_tests


@pytest.mark.integration
@pytest.mark.duckdb
def test_schema_yaml_runs_basic_checks(tmp_path: Path):
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "users.ff.sql").write_text(
        "create or replace table users as select 1 as id, 'a@example.com' as email",
        encoding="utf-8",
    )
    (tmp_path / "models" / "users.yml").write_text(
        """
version: 1
models:
  - name: users.ff
    tags: [batch]
    columns:
      - name: id
        tests:
          - not_null
          - unique
      - name: email
        tests:
          - accepted_values:
              values: ["a@example.com","b@example.com"]
              severity: warn
""",
        encoding="utf-8",
    )

    REGISTRY.load_project(tmp_path)
    env = REGISTRY.get_env()
    ex = DuckExecutor(":memory:")
    ex.run_sql(REGISTRY.get_node("users.ff"), env)

    specs = load_schema_tests(tmp_path)
    specs = _apply_legacy_tag_filter(specs, ["batch"], legacy_token=True)

    results = _run_dq_tests(ex, specs)

    error_fails = [r for r in results if (not r.ok) and r.severity != "warn"]
    assert error_fails == []
