# tests/unit/executors/test_bigquery_exec_unit.py
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest
from tests.common.mock.bigquery import (
    FakeBadRequest,
    FakeClient,
    FakeField,
    FakeJob,
    FakeNotFound,
    FakeWriteDisposition,
    install_fake_bigquery,
)

import fastflowtransform.executors.bigquery.base as bq_base_mod
import fastflowtransform.executors.bigquery.pandas as bq_exec_mod
from fastflowtransform.core import Node
from fastflowtransform.executors.base import BaseExecutor


@pytest.fixture
def bq_exec(monkeypatch):
    _ = install_fake_bigquery(monkeypatch, [bq_exec_mod])

    fake_client = FakeClient(project="p1", location="EU")

    ex = bq_exec_mod.BigQueryExecutor(
        project="p1",
        dataset="ds1",
        location="EU",
        client=cast(Any, fake_client),
    )

    fake_client.add_dataset("p1.ds1")

    monkeypatch.setattr(bq_exec_mod, "NotFound", FakeNotFound, raising=True)
    monkeypatch.setattr(bq_exec_mod, "BadRequest", FakeBadRequest, raising=True)

    return ex


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.bigquery
def test_read_relation_happy(bq_exec, monkeypatch):
    def fake_query(sql: str, location: str | None = None, job_config: Any | None = None):
        return FakeJob(
            rows=[(1, "A")],
            schema=[FakeField("id"), FakeField("name")],
        )

    bq_exec.client.query = fake_query  # type: ignore[assignment]

    df = bq_exec._read_relation("tbl", Node(name="n", kind="sql", path=Path(".")), deps=["x"])
    assert list(df.columns) == ["id", "name"]
    assert df.to_dict(orient="records") == [{"id": 1, "name": "A"}]


@pytest.mark.unit
@pytest.mark.bigquery
def test_read_relation_not_found_raises_nice_message(bq_exec, monkeypatch):
    def boom(sql: str, location: str | None = None, job_config: Any | None = None):
        raise FakeNotFound("nope")

    bq_exec.client.query = boom  # type: ignore[assignment]

    bq_exec.client.add_table("p1.ds1", "existing1")

    with pytest.raises(RuntimeError) as exc:
        bq_exec._read_relation(
            "missing",
            Node(name="n", kind="sql", path=Path(".")),
            deps=["dep1"],
        )

    msg = str(exc.value)
    assert "Dependency table not found" in msg
    assert "dep1" in msg
    assert "existing1" in msg


@pytest.mark.unit
@pytest.mark.bigquery
def test_materialize_relation_happy(bq_exec, monkeypatch):
    df = pd.DataFrame({"id": [1], "name": ["A"]})

    called: dict[str, Any] = {}

    def fake_load(df_in, table_id, job_config=None, location=None):
        called["table_id"] = table_id
        called["location"] = location
        called["job_config"] = job_config
        return FakeJob()

    bq_exec.client.load_table_from_dataframe = fake_load  # type: ignore[assignment]

    bq_exec._materialize_relation("out_tbl", df, Node(name="m", kind="python", path=Path(".")))

    assert called["table_id"] == "p1.ds1.out_tbl"
    assert called["location"] == "EU"
    assert (
        getattr(called["job_config"], "write_disposition", None)
        == FakeWriteDisposition.WRITE_TRUNCATE
    )


@pytest.mark.unit
@pytest.mark.bigquery
def test_materialize_relation_wraps_badrequest(bq_exec, monkeypatch):
    df = pd.DataFrame({"id": [1]})

    def bad_load(*_a, **_k):
        raise FakeBadRequest("boom")

    bq_exec.client.load_table_from_dataframe = bad_load  # type: ignore[assignment]

    with pytest.raises(RuntimeError) as exc:
        bq_exec._materialize_relation("out_tbl", df, Node(name="m", kind="python", path=Path(".")))
    msg = str(exc.value)
    assert "BigQuery write failed" in msg
    assert "out_tbl" in msg


@pytest.mark.unit
@pytest.mark.bigquery
def test_create_view_over_table_calls_bq(bq_exec):
    bq_exec.client.queries.clear()
    node = Node(name="m", kind="python", path=Path("."))
    bq_exec._create_view_over_table("v_users", "ds1.users", node)
    assert any("CREATE OR REPLACE VIEW" in q[0] for q in bq_exec.client.queries)


@pytest.mark.unit
@pytest.mark.bigquery
def test_frame_name(bq_exec):
    assert bq_exec._frame_name() == "pandas"


@pytest.mark.unit
@pytest.mark.bigquery
def test_format_relation_for_ref(bq_exec):
    rel = bq_exec._format_relation_for_ref("m1")
    assert "p1" in rel
    assert "ds1" in rel
    assert "m1" in rel


@pytest.mark.unit
@pytest.mark.bigquery
def test_format_source_reference(bq_exec):
    cfg = {
        "identifier": "src_tbl",
        "project": "other_proj",
        "dataset": "raw",
    }
    ref = bq_exec._format_source_reference(cfg, "src", "tbl")
    assert "other_proj" in ref
    assert "raw" in ref
    assert "src_tbl" in ref


@pytest.mark.unit
@pytest.mark.bigquery
def test_ensure_dataset_respects_flag(monkeypatch):
    _ = install_fake_bigquery(monkeypatch, [bq_exec_mod])
    fake_client = FakeClient(project="p1", location="EU")

    ex = bq_exec_mod.BigQueryExecutor(
        project="p1",
        dataset="ds_missing",
        location="EU",
        client=cast(Any, fake_client),
        allow_create_dataset=False,
    )

    with pytest.raises(FakeNotFound):
        ex._ensure_dataset()


@pytest.mark.unit
@pytest.mark.bigquery
def test_ensure_dataset_creates_when_allowed(monkeypatch):
    _ = install_fake_bigquery(monkeypatch, [bq_exec_mod])
    fake_client = FakeClient(project="p1", location="EU")

    ex = bq_exec_mod.BigQueryExecutor(
        project="p1",
        dataset="ds_new",
        location="EU",
        client=cast(Any, fake_client),
        allow_create_dataset=True,
    )

    ex._ensure_dataset()
    ds_id = "p1.ds_new"
    assert ds_id in fake_client._datasets
    assert fake_client.get_dataset(ds_id).location == "EU"


@pytest.mark.unit
@pytest.mark.bigquery
def test_apply_sql_materialization_calls_super_and_ensures_dataset(monkeypatch, bq_exec):
    monkeypatch.setattr(bq_exec, "_ensure_dataset", lambda: None, raising=True)

    called: dict[str, str] = {}

    monkeypatch.setattr(
        BaseExecutor,
        "_apply_sql_materialization",
        lambda self, node, target_sql, select_body, materialization: called.update(
            {
                "node": node.name,
                "target_sql": target_sql,
                "select_body": select_body,
                "materialization": materialization,
            }
        ),
        raising=True,
    )

    node = Node(name="m_apply", kind="sql", path=Path("."))
    bq_exec._apply_sql_materialization(
        node,
        target_sql="`p1`.`ds1`.`t_out`",
        select_body="SELECT 1",
        materialization="table",
    )

    assert called["node"] == "m_apply"
    assert called["target_sql"] == "`p1`.`ds1`.`t_out`"
    assert called["select_body"] == "SELECT 1"
    assert called["materialization"] == "table"


@pytest.mark.unit
@pytest.mark.bigquery
def test_apply_sql_materialization_wraps_badrequest(monkeypatch, bq_exec):
    monkeypatch.setattr(
        BaseExecutor,
        "_apply_sql_materialization",
        lambda *a, **k: (_ for _ in ()).throw(FakeBadRequest("bq exploded")),
        raising=True,
    )

    node = Node(name="m_bad", kind="sql", path=Path("."))

    with pytest.raises(RuntimeError) as exc:
        bq_exec._apply_sql_materialization(
            node,
            target_sql="`p1`.`ds1`.`t_out`",
            select_body="SELECT 1",
            materialization="table",
        )
    msg = str(exc.value)
    assert "BigQuery SQL failed for" in msg
    assert "SELECT 1" in msg


@pytest.mark.unit
@pytest.mark.bigquery
def test_create_or_replace_view_calls_client_query(bq_exec):
    bq_exec.client.queries.clear()
    bq_exec._create_or_replace_view(
        "p1.ds1.v_view", "SELECT 1", Node(name="x", kind="sql", path=Path("."))
    )
    assert any("CREATE OR REPLACE VIEW" in q[0] for q in bq_exec.client.queries)


@pytest.mark.unit
@pytest.mark.bigquery
def test_create_or_replace_table_calls_client_query(bq_exec):
    bq_exec.client.queries.clear()
    bq_exec._create_or_replace_table(
        "p1.ds1.t_out", "SELECT 1", Node(name="x", kind="sql", path=Path("."))
    )
    assert any("CREATE OR REPLACE TABLE" in q[0] for q in bq_exec.client.queries)


@pytest.mark.unit
@pytest.mark.bigquery
def test_create_or_replace_view_from_table_calls_client_query(bq_exec):
    bq_exec.client.queries.clear()
    bq_exec._create_or_replace_view_from_table(
        "v_users", "ds1.users", Node(name="x", kind="python", path=Path("."))
    )
    assert any("CREATE OR REPLACE VIEW" in q[0] for q in bq_exec.client.queries)


@pytest.mark.unit
@pytest.mark.bigquery
def test_on_node_built_calls_meta(monkeypatch, bq_exec):
    called = {"ensure": 0, "upsert": 0}

    def fake_ensure(ex):
        called["ensure"] += 1

    def fake_upsert(ex, name, rel, fp, eng):
        called["upsert"] += 1

    monkeypatch.setattr(bq_base_mod, "ensure_meta_table", fake_ensure)
    monkeypatch.setattr(bq_base_mod, "upsert_meta", fake_upsert)

    bq_exec.on_node_built(Node(name="m", kind="sql", path=Path(".")), "p1.ds1.m", "fp123")

    assert called["ensure"] == 1
    assert called["upsert"] == 1


@pytest.mark.unit
@pytest.mark.bigquery
def test_on_node_built_raises_on_meta_failure(monkeypatch, bq_exec):
    def bad_ensure(ex):
        raise RuntimeError("boom")

    monkeypatch.setattr(bq_base_mod, "ensure_meta_table", bad_ensure)

    with pytest.raises(RuntimeError):
        bq_exec.on_node_built(Node(name="m", kind="sql", path=Path(".")), "p1.ds1.m", "fp123")


@pytest.mark.unit
@pytest.mark.bigquery
def test_exists_relation_true(bq_exec, monkeypatch):
    class _Job:
        def __init__(self):
            self._rows = [(1,)]

        def result(self):
            return self._rows

    monkeypatch.setattr(
        bq_exec.client,
        "query",
        lambda sql, location=None, job_config=None: _Job(),
        raising=True,
    )

    assert bq_exec.exists_relation("some_table") is True


@pytest.mark.unit
@pytest.mark.bigquery
def test_create_table_as_cleans_select(bq_exec):
    bq_exec.client.queries.clear()
    bq_exec.create_table_as("dst_tbl", "SELECT 1;")
    sql = bq_exec.client.queries[-1][0]
    assert "CREATE TABLE" in sql
    assert "SELECT 1" in sql


@pytest.mark.unit
@pytest.mark.bigquery
def test_incremental_insert_cleans_select(bq_exec):
    bq_exec.client.queries.clear()
    bq_exec.incremental_insert("dst_tbl", "SELECT 1;")
    sql = bq_exec.client.queries[-1][0]
    assert "INSERT INTO" in sql
    assert "SELECT 1" in sql


@pytest.mark.unit
@pytest.mark.bigquery
def test_incremental_merge_executes_two_statements(bq_exec):
    bq_exec.client.queries.clear()
    bq_exec.incremental_merge("dst_tbl", "SELECT 1 AS id", ["id"])
    assert len(bq_exec.client.queries) == 4
    assert "DELETE FROM" in bq_exec.client.queries[0][0]
    assert "INSERT INTO" in bq_exec.client.queries[2][0]


@pytest.mark.unit
@pytest.mark.bigquery
def test_alter_table_sync_schema_adds_missing_columns(bq_exec, monkeypatch):
    bq_exec.client.get_table = lambda ref: SimpleNamespace(schema=[FakeField("id")])  # type: ignore[assignment]

    bq_exec.client.queries.clear()
    bq_exec.alter_table_sync_schema("existing", "SELECT 1 AS id, 2 AS new_col")

    assert any("ADD COLUMN new_col" in q[0] for q in bq_exec.client.queries)
