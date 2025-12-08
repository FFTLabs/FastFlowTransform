# tests/unit/test_seeding_unit.py
from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from fastflowtransform import seeding

# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_seed_file_csv(tmp_path: Path):
    p = tmp_path / "users.csv"
    p.write_text("id,name\n1,A\n2,B\n", encoding="utf-8")

    df = seeding._read_seed_file(p)
    assert list(df.columns) == ["id", "name"]
    expected_row_count = 2
    assert len(df) == expected_row_count


@pytest.mark.unit
def test_read_seed_file_unsupported(tmp_path: Path):
    p = tmp_path / "users.txt"
    p.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError):
        seeding._read_seed_file(p)


@pytest.mark.unit
def test_apply_schema_happy():
    df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"], "age": [10, 20]})
    cfg_raw = {
        "dtypes": {
            "users": {
                "name": "string",
                "age": "int64",
            }
        }
    }
    schema_cfg = seeding.SeedsSchemaConfig.model_validate(cfg_raw)

    out = seeding._apply_schema(df, "users", schema_cfg, seed_id="users")
    assert str(out.dtypes["name"]).startswith("string")
    assert str(out.dtypes["age"]) in ("int64", "Int64")


@pytest.mark.unit
def test_apply_schema_ignores_missing_table_key():
    df = pd.DataFrame({"id": [1]})
    cfg_raw = {"dtypes": {"users": {"id": "int64"}}}
    schema_cfg = seeding.SeedsSchemaConfig.model_validate(cfg_raw)
    out = seeding._apply_schema(df, "other", schema_cfg, seed_id="other")
    assert out.equals(df)


@pytest.mark.unit
def test_apply_schema_soft_fails_on_bad_cast():
    df = pd.DataFrame({"id": ["x"]})
    # force bad cast
    cfg_raw = {"dtypes": {"t": {"id": "int64"}}}
    schema_cfg = seeding.SeedsSchemaConfig.model_validate(cfg_raw)
    out = seeding._apply_schema(df, "t", schema_cfg, seed_id="t")
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Pretty helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_human_int_formats_with_spaces():
    assert seeding._human_int(1234567) == "1 234 567"
    assert seeding._human_int(0) == "0"


@pytest.mark.unit
def test_human_bytes_formats_reasonably():
    assert seeding._human_bytes(512) == "512 B"
    # just smoke tests
    assert "KB" in seeding._human_bytes(2_000)
    assert "MB" in seeding._human_bytes(2_000_000)


@pytest.mark.unit
def test_echo_seed_line(monkeypatch):
    lines: list[str] = []

    def fake_echo(msg: str) -> None:
        lines.append(msg)

    monkeypatch.setattr(seeding, "echo", fake_echo)

    seeding._echo_seed_line(
        full_name="raw.users",
        rows=1234,
        cols=5,
        engine="duckdb",
        ms=42,
        created_schema=True,
        extra="reset location",
    )

    assert len(lines) == 1
    out = lines[0]
    assert "raw.users" in out
    assert "1 234×5" in out  # noqa RUF001
    assert "[duckdb]" in out
    assert "(+schema)" in out
    assert "reset location" in out


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_seed_id_simple(tmp_path: Path):
    seeds_dir = tmp_path / "seeds"
    seeds_dir.mkdir()
    p = seeds_dir / "users.csv"
    p.write_text("id\n1\n", encoding="utf-8")
    assert seeding._seed_id(seeds_dir, p) == "users"


@pytest.mark.unit
def test_seed_id_nested(tmp_path: Path):
    seeds_dir = tmp_path / "seeds"
    (seeds_dir / "raw").mkdir(parents=True)
    p = seeds_dir / "raw" / "users.csv"
    p.write_text("id\n1\n", encoding="utf-8")
    assert seeding._seed_id(seeds_dir, p) == "raw/users"


@pytest.mark.unit
def test_resolve_schema_and_table_by_cfg_priority_engine_override():
    schema_cfg_raw = {
        "targets": {
            "raw/users": {
                "schema": "raw",
                "table": "users_final",
                "schema_by_engine": {
                    "postgres": "pg_raw",
                    "duckdb": "main",
                },
            }
        }
    }
    # executor pretending to be postgres
    ex = SimpleNamespace(
        engine_name="postgres", engine=SimpleNamespace(dialect=SimpleNamespace(name="postgres"))
    )

    schema_cfg = seeding.SeedsSchemaConfig.model_validate(schema_cfg_raw)

    schema, table = seeding._resolve_schema_and_table_by_cfg(
        seed_id="raw/users",
        stem="users",
        schema_cfg=schema_cfg,
        executor=ex,
        default_schema="public",
    )

    assert schema == "pg_raw"
    assert table == "users_final"


@pytest.mark.unit
def test_resolve_schema_and_table_falls_back_to_default_schema():
    ex = SimpleNamespace(engine=None, con=None)
    schema, table = seeding._resolve_schema_and_table_by_cfg(
        seed_id="raw/users",
        stem="users",
        schema_cfg=None,
        executor=ex,
        default_schema="public",
    )
    assert schema == "public"
    assert table == "users"


# ---------------------------------------------------------------------------
# seed_project
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_seed_project_no_seeds_dir(tmp_path: Path):
    executor = SimpleNamespace()
    count = seeding.seed_project(tmp_path, executor, default_schema=None)
    assert count == 0


@pytest.mark.unit
def test_seed_project_ambiguous_stems_raises(tmp_path: Path):
    seeds_dir = tmp_path / "seeds"
    (seeds_dir / "a").mkdir(parents=True)
    (seeds_dir / "b").mkdir(parents=True)
    (seeds_dir / "a" / "users.csv").write_text("id\n1\n", encoding="utf-8")
    (seeds_dir / "b" / "users.csv").write_text("id\n2\n", encoding="utf-8")

    # schema.yml that uses bare "users"
    (seeds_dir / "schema.yml").write_text(
        textwrap.dedent(
            """
            targets:
              users:
                schema: raw
            """
        ),
        encoding="utf-8",
    )

    executor = SimpleNamespace(schema="public", engine_name="duckdb")

    with pytest.raises(ValueError) as exc:
        seeding.seed_project(tmp_path, executor, default_schema=None)

    assert "appears multiple times" in str(exc.value)
    assert "Please configure using the path-based seed ID" in str(exc.value)
