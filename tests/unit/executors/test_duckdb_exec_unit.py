from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from fastflowtransform.core import Node
from fastflowtransform.executors.duckdb import DuckExecutor, _q


@pytest.fixture
def duck_exec() -> DuckExecutor:
    # real in-memory db is fine for unit tests
    return DuckExecutor(":memory:")


@pytest.fixture
def duck_exec_schema() -> DuckExecutor:
    return DuckExecutor(":memory:", schema="demo_schema")


def _node(name: str = "m", kind: str = "python") -> Node:
    return Node(name=name, kind=kind, path=Path("."))


# ---------------------------------------------------------------------------
# _read_relation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_read_relation_happy(duck_exec: DuckExecutor):
    duck_exec.con.execute("create table my_tbl (id int, name varchar)")
    duck_exec.con.execute("insert into my_tbl values (1, 'A'), (2, 'B')")

    df = duck_exec._read_relation("my_tbl", _node(), deps=["up1"])
    assert list(df.columns) == ["id", "name"]
    assert df.to_dict(orient="records") == [
        {"id": 1, "name": "A"},
        {"id": 2, "name": "B"},
    ]


@pytest.mark.unit
@pytest.mark.duckdb
def test_read_relation_missing_raises_nice_error(duck_exec: DuckExecutor):
    # no table created
    with pytest.raises(RuntimeError) as exc:
        duck_exec._read_relation("does_not_exist", _node(), deps=["m1", "m2"])
    msg = str(exc.value)
    assert "Dependency table not found" in msg
    assert "m1" in msg
    assert "Existing tables" in msg  # from the executor


# ---------------------------------------------------------------------------
# _materialize_relation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_materialize_relation_registers_and_creates_table(duck_exec: DuckExecutor):
    df = pd.DataFrame({"id": [1, 2], "name": ["A", "B"]})

    duck_exec._materialize_relation("out_tbl", df, _node())

    out = duck_exec.con.execute("select * from out_tbl order by id").fetchall()
    assert out == [(1, "A"), (2, "B")]

    # temp table should be gone / unregistered - we just assert that running again doesn't error
    duck_exec._materialize_relation("out_tbl", df, _node())


# ---------------------------------------------------------------------------
# view over table
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_create_or_replace_view_from_table(duck_exec: DuckExecutor):
    duck_exec.con.execute("create table src_tbl (id int)")
    duck_exec.con.execute("insert into src_tbl values (10)")
    duck_exec._create_or_replace_view_from_table("v_src", "src_tbl", _node())

    rows = duck_exec.con.execute("select * from v_src").fetchall()
    assert rows == [(10,)]


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_format_relation_for_ref(duck_exec: DuckExecutor):
    rel = duck_exec._format_relation_for_ref("my_model")
    # relation_for("my_model") → "my_model"
    assert rel == _q("my_model")


@pytest.mark.unit
@pytest.mark.duckdb
def test_format_relation_for_ref_with_schema(duck_exec_schema: DuckExecutor):
    rel = duck_exec_schema._format_relation_for_ref("my_model")
    assert rel == f'"demo_schema".{_q("my_model")}'


@pytest.mark.unit
@pytest.mark.duckdb
def test_format_relation_for_ref_with_catalog_collision():
    exec_catalog = DuckExecutor(":memory:", schema="memory")
    rel = exec_catalog._format_relation_for_ref("foo")
    assert rel == '"memory"."memory"."foo"'


@pytest.mark.unit
@pytest.mark.duckdb
def test_format_source_reference_ok(duck_exec: DuckExecutor):
    cfg = {
        "catalog": "c1",
        "schema": "s1",
        "identifier": "src_tbl",
    }
    ref = duck_exec._format_source_reference(cfg, "src", "tbl")
    # should be quoted catalog.schema.identifier
    assert ref == '"c1"."s1"."src_tbl"'


@pytest.mark.unit
@pytest.mark.duckdb
def test_format_source_reference_path_not_supported(duck_exec: DuckExecutor):
    cfg = {"location": "/some/path.csv"}
    with pytest.raises(NotImplementedError):
        duck_exec._format_source_reference(cfg, "src", "tbl")


@pytest.mark.unit
@pytest.mark.duckdb
def test_format_source_reference_injects_executor_schema():
    exec_schema = DuckExecutor(":memory:", schema="demo_schema")
    ref = exec_schema._format_source_reference({"identifier": "src_tbl"}, "src", "tbl")
    assert ref == '"demo_schema"."src_tbl"'


@pytest.mark.unit
@pytest.mark.duckdb
def test_format_source_reference_injects_catalog_when_matches_schema():
    exec_catalog = DuckExecutor(":memory:", schema="memory")
    ref = exec_catalog._format_source_reference({"identifier": "src_tbl"}, "src", "tbl")
    assert ref == '"memory"."memory"."src_tbl"'


# ---------------------------------------------------------------------------
# on_node_built
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_on_node_built_invokes_meta_helpers(
    duck_exec: DuckExecutor, monkeypatch: pytest.MonkeyPatch
):
    called = {"ensure": 0, "upsert": 0}

    def fake_ensure(ex: Any) -> None:
        called["ensure"] += 1

    def fake_upsert(ex: Any, name: str, rel: str, fp: str, eng: str) -> None:
        called["upsert"] += 1

    import fastflowtransform.executors.duckdb as duck_mod  # noqa: PLC0415

    monkeypatch.setattr(duck_mod, "ensure_meta_table", fake_ensure, raising=True)
    monkeypatch.setattr(duck_mod, "upsert_meta", fake_upsert, raising=True)

    duck_exec.on_node_built(_node("m1"), "out_tbl", "fp123")

    assert called["ensure"] == 1
    assert called["upsert"] == 1


@pytest.mark.unit
@pytest.mark.duckdb
def test_on_node_built_propagates_meta_errors(
    duck_exec: DuckExecutor, monkeypatch: pytest.MonkeyPatch
):
    import fastflowtransform.executors.duckdb as duck_mod  # noqa: PLC0415

    def bad_ensure(ex: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(duck_mod, "ensure_meta_table", bad_ensure, raising=True)

    with pytest.raises(RuntimeError):
        duck_exec.on_node_built(_node("m1"), "out_tbl", "fp123")


# ---------------------------------------------------------------------------
# exists_relation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_exists_relation_true(duck_exec: DuckExecutor):
    duck_exec.con.execute("create table t1 (id int)")
    assert duck_exec.exists_relation("t1") is True


@pytest.mark.unit
@pytest.mark.duckdb
def test_exists_relation_false(duck_exec: DuckExecutor):
    assert duck_exec.exists_relation("nope") is False


@pytest.mark.unit
@pytest.mark.duckdb
def test_exists_relation_with_schema(duck_exec_schema: DuckExecutor):
    duck_exec_schema.con.execute('create table "demo_schema"."t_s" (id int)')
    assert duck_exec_schema.exists_relation("t_s") is True


# ---------------------------------------------------------------------------
# create_table_as / incremental_insert / incremental_merge
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_create_table_as_cleans_select(duck_exec: DuckExecutor):
    duck_exec.create_table_as("t_created", "select 1 as id;")
    rows = duck_exec.con.execute("select * from t_created").fetchall()
    assert rows == [(1,)]


@pytest.mark.unit
@pytest.mark.duckdb
def test_incremental_insert_appends_rows(duck_exec: DuckExecutor):
    duck_exec.con.execute("create table tgt (id int)")
    duck_exec.incremental_insert("tgt", "select 1 as id;")
    duck_exec.incremental_insert("tgt", "select 2 as id;")
    rows = duck_exec.con.execute("select * from tgt order by id").fetchall()
    assert rows == [(1,), (2,)]


@pytest.mark.unit
@pytest.mark.duckdb
def test_incremental_merge_deletes_then_inserts(duck_exec: DuckExecutor):
    # target with PK-like col
    duck_exec.con.execute("create table tgt (id int, val varchar)")
    duck_exec.con.execute("insert into tgt values (1, 'old'), (2, 'keep')")

    # src produces (1, 'new'), (3, 'new3')
    duck_exec.incremental_merge(
        "tgt",
        "select 1 as id, 'new' as val union all select 3 as id, 'new3' as val",
        unique_key=["id"],
    )

    rows = duck_exec.con.execute("select id, val from tgt order by id").fetchall()
    # id=1 should be updated to 'new' (via delete+insert)
    # id=2 stays
    # id=3 inserted
    assert rows == [(1, "new"), (2, "keep"), (3, "new3")]


# ---------------------------------------------------------------------------
# alter_table_sync_schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.duckdb
def test_alter_table_sync_schema_adds_missing_columns(duck_exec: DuckExecutor):
    # create target with only "id"
    duck_exec.con.execute("create table my_tbl (id int)")

    # select has id + new_col
    duck_exec.alter_table_sync_schema("my_tbl", "select 1 as id, 2 as new_col")

    # inspect schema
    info = duck_exec.con.execute("pragma table_info('my_tbl')").fetchall()
    # format: (cid, name, type, notnull, dflt_value, pk)
    col_names = [r[1] for r in info]
    assert "id" in col_names
    assert "new_col" in col_names


@pytest.mark.unit
@pytest.mark.duckdb
def test_read_relation_respects_schema(duck_exec_schema: DuckExecutor):
    duck_exec_schema.con.execute('create table "demo_schema"."t_in_schema" (id int)')
    duck_exec_schema.con.execute('insert into "demo_schema"."t_in_schema" values (5)')
    df = duck_exec_schema._read_relation("t_in_schema", _node(), deps=[])
    assert df.to_dict(orient="records") == [{"id": 5}]
