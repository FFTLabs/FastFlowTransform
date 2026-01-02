# tests/unit/test_utest_unit.py
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pandas as pd
import pytest
import yaml

from fastflowtransform import utest
from fastflowtransform.cache import FingerprintCache
from fastflowtransform.core import REGISTRY, Node
from fastflowtransform.utest import (
    EnvCtx,
    UnitCase,
    UnitDefaults,
    UnitExpect,
    UnitSpec,
    UtestCtx,
    _make_env_ctx,
    _maybe_skip_by_cache,
    _project_root_for_spec,
)


def make_fake_cache() -> FingerprintCache:
    fake = SimpleNamespace(
        load=lambda: None,
        save=lambda: None,
        update_many=lambda d: None,
        get=lambda *a, **k: None,
    )
    return cast(FingerprintCache, fake)


# ------------------------------------------------------------
# _fingerprint_case_inputs
# ------------------------------------------------------------


@pytest.mark.unit
def test_fingerprint_case_inputs_merges_defaults_and_case(tmp_path, monkeypatch):
    csv_file = tmp_path / "seed.csv"
    csv_file.write_text("id,name\n1,A\n", encoding="utf-8")

    spec = utest.UnitSpec(
        model="dummy",
        engine="duckdb",
        defaults=utest.UnitDefaults(
            inputs={
                "src": utest.UnitInput(rows=[{"id": 1}]),
            },
            expect=utest.UnitExpect(),
        ),
        cases=[],
        path=tmp_path / "ut.yml",
        project_dir=tmp_path,
    )

    case = utest.UnitCase(
        name="c1",
        inputs={
            "src": utest.UnitInput(rows=[{"id": 2}]),
            "dim": utest.UnitInput(csv="seed.csv"),
        },
        expect=utest.UnitExpect(),
    )

    fp = utest._fingerprint_case_inputs(spec, case)
    expected_fp_len = 64
    assert isinstance(fp, str)
    assert len(fp) == expected_fp_len
    assert all(ch in "0123456789abcdef" for ch in fp)


# ------------------------------------------------------------
# _resolve_csv_path
# ------------------------------------------------------------


@pytest.mark.unit
def test_resolve_csv_path_prefers_yaml_dir(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    tests_dir = proj / "tests" / "unit"
    tests_dir.mkdir(parents=True)

    csv_in_yaml_dir = tests_dir / "my.csv"
    csv_in_yaml_dir.write_text("id\n1\n", encoding="utf-8")

    spec = SimpleNamespace(
        path=tests_dir / "case.yml",
        project_dir=proj,
    )

    out = utest._resolve_csv_path(spec, "my.csv")
    assert out == csv_in_yaml_dir.resolve()


@pytest.mark.unit
def test_resolve_csv_path_falls_back_to_project_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    csv_in_proj = proj / "my.csv"
    csv_in_proj.write_text("id\n1\n", encoding="utf-8")

    # spec liegt woanders, aber project_dir zeigt auf proj
    spec = SimpleNamespace(
        path=tmp_path / "some" / "other.yml",
        project_dir=proj,
    )

    out = utest._resolve_csv_path(spec, "my.csv")
    assert out == csv_in_proj.resolve()


# ------------------------------------------------------------
# Assertions: assert_rows_equal und Helfer
# ------------------------------------------------------------


@pytest.mark.unit
def test_assert_rows_equal_exact_ok():
    actual = pd.DataFrame([{"id": 1, "name": "a"}])
    expect = [{"id": 1, "name": "a"}]

    # no raise
    utest.assert_rows_equal(actual, expect)


@pytest.mark.unit
def test_assert_rows_equal_missing_col_raises():
    actual = pd.DataFrame([{"id": 1}])
    expect = [{"id": 1, "name": "a"}]

    with pytest.raises(utest.UnitAssertionFailure) as exc:
        utest.assert_rows_equal(actual, expect)

    assert "Missing columns in actual" in str(exc.value)


@pytest.mark.unit
def test_assert_rows_equal_ignore_columns():
    actual = pd.DataFrame([{"id": 1, "ts": "2025-01-01"}])
    expect = [{"id": 1}]

    utest.assert_rows_equal(actual, expect, ignore_columns=["ts"])


@pytest.mark.unit
def test_assert_rows_equal_any_order():
    actual = pd.DataFrame([{"id": 2}, {"id": 1}])
    expect = [{"id": 1}, {"id": 2}]

    utest.assert_rows_equal(actual, expect, any_order=True)


@pytest.mark.unit
def test_assert_rows_equal_subset_mode():
    actual = pd.DataFrame(
        [
            {"id": 1, "val": "x"},
            {"id": 2, "val": "y"},
        ]
    )
    expect = [{"id": 2, "val": "y"}]

    # subset -> ok
    utest.assert_rows_equal(actual, expect, subset=True)


@pytest.mark.unit
def test_assert_rows_equal_subset_missing_row():
    actual = pd.DataFrame([{"id": 1, "val": "x"}])
    expect = [{"id": 2, "val": "y"}]

    with pytest.raises(utest.UnitAssertionFailure) as exc:
        utest.assert_rows_equal(actual, expect, subset=True)

    assert "Expected row" in str(exc.value)


@pytest.mark.unit
def test_assert_rows_equal_approx_numeric():
    actual = pd.DataFrame([{"id": 1, "score": 1.005}])
    expect = [{"id": 1, "score": 1.0}]

    # tolerance 0.01 -> ok
    utest.assert_rows_equal(actual, expect, approx={"score": 0.01})


@pytest.mark.unit
def test_apply_approx_equalization_bad_tolerance_raises():
    actual = pd.DataFrame([{"x": 1}])
    expect = pd.DataFrame([{"x": 1}])

    with pytest.raises(utest.UnitAssertionFailure):
        utest._apply_approx_equalization(
            actual,
            expect,
            {"x": "not-a-number"},  # type: ignore[arg-type]
        )


# ------------------------------------------------------------
# validate_inputs_cover_deps
# ------------------------------------------------------------


@pytest.mark.unit
def test_validate_inputs_cover_deps_detects_missing():
    node = Node(name="m", kind="sql", path=Path("."), deps=["src_a", "src_b"])
    expected, missing = utest.validate_inputs_cover_deps(
        node,
        inputs={"src_a": {"rows": []}},
    )

    assert expected == ["src_a", "src_b"]
    assert missing == ["src_b"]


# ------------------------------------------------------------
# _normalize_cache_mode
# ------------------------------------------------------------


@pytest.mark.unit
def test_normalize_cache_mode_accepts_strings():
    assert utest._normalize_cache_mode("off") == "off"
    assert utest._normalize_cache_mode("RO") == "ro"
    assert utest._normalize_cache_mode("Rw") == "rw"


@pytest.mark.unit
def test_normalize_cache_mode_rejects_unknown():
    with pytest.raises(ValueError):
        utest._normalize_cache_mode("something-else")


# ------------------------------------------------------------
# _detect_engine_name
# ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_detect_engine_name_duckdb_like():
    execu = SimpleNamespace(con=object())
    assert utest._detect_engine_name(execu) == "duckdb"


@pytest.mark.unit
@pytest.mark.postgres
def test_detect_engine_name_postgres_like():
    execu = SimpleNamespace(engine=object())
    assert utest._detect_engine_name(execu) == "postgres"


@pytest.mark.unit
def test_detect_engine_name_unknown():
    execu = SimpleNamespace()
    assert utest._detect_engine_name(execu) == "unknown"


@pytest.mark.unit
def test_discover_unit_specs_basic(tmp_path, fake_registry):
    tests_dir = tmp_path / "tests" / "unit"
    tests_dir.mkdir(parents=True)
    spec_path = tests_dir / "a.yml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "model": "model_a",
                "engine": "duckdb",
                "defaults": {
                    "inputs": {"src1": {"rows": [{"id": 1}]}},
                    "expect": {"rows": [{"id": 1}]},
                },
                "cases": [
                    {
                        "name": "c1",
                        "inputs": {"src1": {"rows": [{"id": 2}]}},
                        "expect": {"rows": [{"id": 2}]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    specs = utest.discover_unit_specs(tmp_path)
    assert len(specs) == 1
    s = specs[0]
    assert s.model == "model_a"
    assert len(s.cases) == 1
    # expect is now a UnitExpect pydantic model, so use attributes
    assert s.cases[0].expect.rows == [{"id": 2}]


@pytest.mark.unit
def test_discover_unit_specs_only_model_filter(tmp_path, fake_registry):
    tests_dir = tmp_path / "tests" / "unit"
    tests_dir.mkdir(parents=True)
    (tests_dir / "a.yml").write_text(
        yaml.safe_dump({"model": "model_a", "cases": [{"name": "x"}]}), encoding="utf-8"
    )
    (tests_dir / "b.yml").write_text(
        yaml.safe_dump({"model": "other_model", "cases": [{"name": "x"}]}), encoding="utf-8"
    )

    specs = utest.discover_unit_specs(tmp_path, only_model="model_a")
    assert len(specs) == 1
    assert specs[0].model == "model_a"


# ---------------------------------------------------------------------------
# _load_relation_from_rows  (duckdb path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_load_relation_from_rows_duckdb(duckdbutor):
    rows = [{"id": 1}, {"id": 2}]

    # Provide / spy on the new utest helper on our fake executor
    duckdbutor.utest_load_relation_from_rows = MagicMock()

    utest._load_relation_from_rows(duckdbutor, "tmp_tbl", rows)

    duckdbutor.utest_load_relation_from_rows.assert_called_once_with("tmp_tbl", rows)


# ---------------------------------------------------------------------------
# _load_relation_from_csv
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_load_relation_from_csv_calls_rows(monkeypatch, tmp_path, duckdbutor):
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("id,value\n1,a\n2,b\n", encoding="utf-8")

    called = {}

    def fake_rows(executor, rel, rows):
        called["rel"] = rel
        called["rows"] = rows

    monkeypatch.setattr(utest, "_load_relation_from_rows", fake_rows)

    utest._load_relation_from_csv(duckdbutor, "my_rel", csv_path)

    assert called["rel"] == "my_rel"
    expected_row_count = 2
    assert len(called["rows"]) == expected_row_count
    assert called["rows"][0]["id"] == 1


# ---------------------------------------------------------------------------
# _read_result
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_read_result_duckdb(duckdbutor):
    df = utest._read_result(duckdbutor, "some_table")
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["id"]


@pytest.mark.unit
@pytest.mark.postgres
def test_read_result_postgres(monkeypatch, postgresutor):
    # wir patchen pandas.read_sql_query, damit er keine DB braucht
    fake_df = pd.DataFrame([{"x": 1}])

    def fake_read_sql(query, conn):
        return fake_df

    monkeypatch.setattr(utest.pd, "read_sql_query", fake_read_sql)

    df = utest._read_result(postgresutor, "target_table")
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["x"]


# ---------------------------------------------------------------------------
# _project_root_for_spec  (fallback)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_root_for_spec_fallback(tmp_path, monkeypatch):
    # make registry neutral
    monkeypatch.setattr(REGISTRY, "project_dir", None, raising=False)
    monkeypatch.setattr(REGISTRY, "get_project_dir", lambda: None, raising=False)

    spec_dir = tmp_path / "tests" / "unit"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "x.yml"
    spec_path.write_text("model: m1\n", encoding="utf-8")

    spec = UnitSpec(
        model="m1",
        engine=None,
        defaults=UnitDefaults(),
        cases=[UnitCase(name="c1", inputs={}, expect=UnitExpect())],
        path=spec_path,
        project_dir=tmp_path,
    )

    root = _project_root_for_spec(spec)

    assert root == spec_path.parent


# ---------------------------------------------------------------------------
# _make_env_ctx, _make_cache, _get_project_dir_safe
# ---------------------------------------------------------------------------


def test_make_env_ctx_uses_registry_sources(monkeypatch):
    # fake registry sources
    fake_sources = {"src1": {"tables": ["t1"]}}
    monkeypatch.setattr(REGISTRY, "sources", fake_sources, raising=False)

    ctx = _make_env_ctx("duckdb")

    # jetzt auf sources_json gehen
    data = json.loads(ctx.sources_json)
    assert data == fake_sources
    assert ctx.engine == "duckdb"
    assert ctx.profile == "utest"


@pytest.mark.unit
def test_make_cache_none_engine():
    c = utest._make_cache(None, "duckdb")
    assert c is None


@pytest.mark.unit
def test_make_cache_real(tmp_path):
    c = utest._make_cache(tmp_path, "duckdb")
    assert c is not None


@pytest.mark.unit
def test_get_project_dir_safe_ok(tmp_path, monkeypatch):
    reg = SimpleNamespace(get_project_dir=lambda: tmp_path)
    monkeypatch.setattr(utest, "REGISTRY", reg)
    assert utest._get_project_dir_safe() == tmp_path


@pytest.mark.unit
def test_get_project_dir_safe_error(monkeypatch):
    reg = SimpleNamespace(get_project_dir=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(utest, "REGISTRY", reg)
    assert utest._get_project_dir_safe() is None


# ---------------------------------------------------------------------------
# _fingerprint_case + _maybe_skip_by_cache
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fingerprint_case_and_maybe_skip(monkeypatch, tmp_path):
    # force cache hit
    monkeypatch.setattr(
        "fastflowtransform.utest.can_skip_node",
        lambda **_: True,
    )

    env_ctx = EnvCtx(
        engine="duckdb",
        profile="utest",
        env_vars={},
        sources_json="{}",
    )

    fake_exec = SimpleNamespace()

    ctx = UtestCtx(
        executor=fake_exec,
        jenv=object(),
        engine_name="duckdb",
        env_ctx=env_ctx,
        cache=make_fake_cache(),
        cache_mode="rw",  # we want to test the RW branch
    )

    node = SimpleNamespace(name="m1", meta={})
    cand_fp = "fp-123"

    skipped = _maybe_skip_by_cache(node, cand_fp, ctx)

    assert skipped is True
    assert ctx.computed_fps == {"m1": "fp-123"}


# ---------------------------------------------------------------------------
# _execute_and_update_cache
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_execute_and_update_cache_success(fake_registry, duckdbutor):
    env_ctx = utest._make_env_ctx("duckdb")
    cache = MagicMock()
    ctx = utest.UtestCtx(
        executor=duckdbutor,
        jenv=MagicMock(),
        engine_name="duckdb",
        env_ctx=env_ctx,
        cache=cache,
        cache_mode="rw",
    )
    node = fake_registry.nodes["model_a"]
    ok, msg = utest._execute_and_update_cache(node, "abc123", ctx)
    assert ok is True
    assert msg is None
    assert ctx.computed_fps["model_a"] == "abc123"


@pytest.mark.unit
@pytest.mark.duckdb
def test_execute_and_update_cache_failure(fake_registry, duckdbutor):
    # wir machen executor kaputt
    duckdbutor.run_sql = MagicMock(side_effect=RuntimeError("boom"))
    env_ctx = utest._make_env_ctx("duckdb")
    ctx = utest.UtestCtx(
        executor=duckdbutor,
        jenv=MagicMock(),
        engine_name="duckdb",
        env_ctx=env_ctx,
        cache=None,
        cache_mode="off",
    )
    node = fake_registry.nodes["model_a"]
    ok, msg = utest._execute_and_update_cache(node, None, ctx)
    assert ok is False
    assert msg is not None
    assert ctx.failures == 1


# ---------------------------------------------------------------------------
# _read_and_assert
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_read_and_assert_ok(fake_registry, duckdbutor):
    env_ctx = utest._make_env_ctx("duckdb")
    ctx = utest.UtestCtx(
        executor=duckdbutor,
        jenv=MagicMock(),
        engine_name="duckdb",
        env_ctx=env_ctx,
        cache=None,
        cache_mode="off",
    )
    spec = SimpleNamespace(model="model_a")
    case = SimpleNamespace(expect={"rows": [{"id": 1}]})
    utest._read_and_assert(spec, case, ctx)
    assert ctx.failures == 0


@pytest.mark.unit
@pytest.mark.duckdb
def test_read_and_assert_mismatch(fake_registry, duckdbutor, monkeypatch):
    # actual ist id=1, expected ist id=2 -> mismatch
    env_ctx = utest._make_env_ctx("duckdb")
    ctx = utest.UtestCtx(
        executor=duckdbutor,
        jenv=MagicMock(),
        engine_name="duckdb",
        env_ctx=env_ctx,
        cache=None,
        cache_mode="off",
    )
    spec = SimpleNamespace(model="model_a")
    case = SimpleNamespace(expect={"rows": [{"id": 2}]})
    utest._read_and_assert(spec, case, ctx)
    assert ctx.failures == 1


# ---------------------------------------------------------------------------
# run_unit_specs (kleiner happy path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_run_unit_specs_happy(tmp_path, fake_registry, duckdbutor, monkeypatch):
    # wir bauen uns per Hand einen spec
    spec = utest.UnitSpec(
        model="model_a",
        engine="duckdb",
        defaults=utest.UnitDefaults(
            inputs={
                "src1": utest.UnitInput(
                    rows=[{"id": 1}],
                )
            }
        ),
        cases=[
            utest.UnitCase(
                name="c1",
                inputs={
                    "src1": utest.UnitInput(
                        rows=[{"id": 1}],
                    )
                },
                expect=utest.UnitExpect(
                    rows=[{"id": 1}],
                ),
            )
        ],
        path=tmp_path / "tests" / "unit" / "x.yml",
        project_dir=tmp_path,
    )
    # jenv ist hier egal
    failures = utest.run_unit_specs([spec], duckdbutor, jenv=MagicMock(), cache_mode="off")
    assert failures == 0
