from pathlib import Path

import pytest

from fastflowtransform.cli.test_cmd import _run_dq_tests
from fastflowtransform.core import REGISTRY
from fastflowtransform.executors.duckdb import DuckExecutor
from fastflowtransform.schema_loader import load_schema_tests


@pytest.mark.integration
@pytest.mark.duckdb
def test_mix_multiple_tests_per_column(tmp_path: Path):
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "u.ff.sql").write_text(
        "create or replace table u as select 1 as id, "
        "'x@example.com' as email union all select 1, 'bad@x.com'",
        encoding="utf-8",
    )
    (tmp_path / "models" / "u.yml").write_text(
        """
version: 1
models:
  - name: u.ff
    columns:
      - name: id
        tests: [unique]        # will fail
      - name: email
        tests:
          - not_null
          - accepted_values:   # will fail
              values: ["x@example.com"]
              severity: error
        """,
        encoding="utf-8",
    )
    REGISTRY.load_project(tmp_path)
    env = REGISTRY.get_env()
    ex = DuckExecutor(":memory:")
    ex.run_sql(REGISTRY.get_node("u.ff"), env)
    specs = load_schema_tests(tmp_path)
    res = _run_dq_tests(ex, specs)
    # Both should fail with error severity
    assert any((not r.ok) and r.kind == "unique" for r in res)
    assert any((not r.ok) and r.kind == "accepted_values" for r in res)
