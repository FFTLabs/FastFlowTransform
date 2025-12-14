from pathlib import Path

import pytest

from fastflowtransform.schema_loader import load_schema_tests


@pytest.mark.unit
def test_parse_schema_yaml_column_tests(tmp_path: Path):
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "users_enriched.yml").write_text(
        """
version: 1
models:
  - name: users_enriched
    tags: [batch]
    description: "Adds gmail flag"
    columns:
      - name: id
        tests:
          - not_null: { severity: error }
          - unique
      - name: email
        tests:
          - not_null
          - accepted_values:
              values: ["a@example.com", "b@example.com"]
              severity: warn
""",
        encoding="utf-8",
    )
    specs = load_schema_tests(tmp_path)
    kinds = [(s.type, s.column, s.severity, tuple(sorted(s.tags))) for s in specs]
    assert ("not_null", "id", "error", ("batch",)) in kinds
    assert ("unique", "id", "error", ("batch",)) in kinds
    assert ("not_null", "email", "error", ("batch",)) in kinds
    assert ("accepted_values", "email", "warn", ("batch",)) in kinds
    assert all(s.table == "users_enriched" for s in specs)
