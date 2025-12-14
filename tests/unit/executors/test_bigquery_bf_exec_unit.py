# tests/unit/executors/test_bigquery_bf_exec_unit.py
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from tests.common.mock.bigquery import (
    FakeBadRequest,
    FakeClient,
    FakeField,
    FakeJob,
    FakeNotFound,
    install_fake_bigquery,
)

import fastflowtransform.executors.bigquery.base as bq_base_mod
import fastflowtransform.executors.bigquery.bigframes as bq_exec_mod
from fastflowtransform.core import Node
from fastflowtransform.executors.base import BaseExecutor

# ---------------------- BigFrames-Fakes ------------------------------------


class _FakeBigQueryOptions:
    def __init__(self, *a, **kw):
        self.kw = kw


class _FakeBFSession:
    def __init__(self, *a, **kw):
        self.created_with = (a, kw)
        self.read_calls: list[str] = []

    def read_gbq(self, table_id: str) -> Any:
        self.read_calls.append(table_id)
        return SimpleNamespace(
            columns=["id", "name"],
            to_gbq=None,
            materialize=None,
        )


@pytest.fixture
def bq_exec(monkeypatch):
    _ = install_fake_bigquery(monkeypatch, [bq_exec_mod, bq_base_mod])

    # Test-only shim: ensure the fake bigquery module has DatasetReference,
    # which BigQueryBaseExecutor._execute_sql now relies on.
    if not hasattr(bq_base_mod.bigquery, "DatasetReference"):

        class _DummyDatasetReference:
            def __init__(self, project: str, dataset_id: str):
                self.project = project
                self.dataset_id = dataset_id

        monkeypatch.setattr(
            bq_base_mod.bigquery,
            "DatasetReference",
            _DummyDatasetReference,
            raising=False,
        )

    fake_bigframes = types.ModuleType("bigframes")
    fake_conf = types.ModuleType("bigframes._config")
    fake_conf_bq = types.ModuleType("bigframes._config.bigquery_options")

    fake_conf_bq.BigQueryOptions = _FakeBigQueryOptions  # type: ignore[attr-defined]
    fake_bigframes.Session = _FakeBFSession  # type: ignore[attr-defined]

    sys.modules.setdefault("bigframes", fake_bigframes)
    sys.modules.setdefault("bigframes._config", fake_conf)
    sys.modules["bigframes._config.bigquery_options"] = fake_conf_bq

    monkeypatch.setattr(bq_exec_mod, "bigframes", fake_bigframes, raising=True)

    ex = bq_exec_mod.BigQueryBFExecutor(project="p1", dataset="ds1", location="EU")

    assert isinstance(ex.client, FakeClient)
    assert isinstance(ex.session, _FakeBFSession)

    ex.client.add_dataset("p1.ds1")

    return ex


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.bigquery
def test_read_relation_happy(bq_exec):
    df = bq_exec._read_relation("tbl", Node(name="n", kind="sql", path=Path(".")), deps=["x"])
    assert hasattr(df, "columns")
    assert df.columns == ["id", "name"]
    assert bq_exec.session.read_calls == ["p1.ds1.tbl"]


@pytest.mark.unit
@pytest.mark.bigquery
def test_read_relation_not_found_raises_nice_message(bq_exec):
    def boom(_table_id: str):
        raise FakeNotFound("nope")

    bq_exec.session.read_gbq = boom  # type: ignore[assignment]
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
def test_materialize_relation_prefers_to_gbq(bq_exec):
    called: dict[str, Any] = {}

    class DF:
        def to_gbq(self, table_id, if_exists="replace"):
            called["table_id"] = table_id
            called["if_exists"] = if_exists

    bq_exec._materialize_relation("out_tbl", DF(), Node(name="m", kind="python", path=Path(".")))
    assert called["table_id"] == "p1.ds1.out_tbl"
    assert called["if_exists"] == "replace"


@pytest.mark.unit
@pytest.mark.bigquery
def test_ensure_dataset_respects_flag(monkeypatch):
    _ = install_fake_bigquery(monkeypatch, [bq_exec_mod, bq_base_mod])

    fake_bigframes = types.ModuleType("bigframes")
    fake_conf = types.ModuleType("bigframes._config")
    fake_conf_bq = types.ModuleType("bigframes._config.bigquery_options")
    fake_conf_bq.BigQueryOptions = _FakeBigQueryOptions  # type: ignore[attr-defined]
    fake_bigframes.Session = _FakeBFSession  # type: ignore[attr-defined]
    sys.modules.setdefault("bigframes", fake_bigframes)
    sys.modules.setdefault("bigframes._config", fake_conf)
    sys.modules["bigframes._config.bigquery_options"] = fake_conf_bq
    monkeypatch.setattr(bq_exec_mod, "bigframes", fake_bigframes, raising=True)

    fake_client = FakeClient(project="p1", location="EU")
    ex = bq_exec_mod.BigQueryBFExecutor(
        project="p1",
        dataset="ds_missing",
        location="EU",
        allow_create_dataset=False,
    )
    # inject the fake client after construction (session uses fake bigframes)
    ex.client = cast(Any, fake_client)

    with pytest.raises(FakeNotFound):
        ex._ensure_dataset()


@pytest.mark.unit
@pytest.mark.bigquery
def test_ensure_dataset_creates_when_allowed(monkeypatch):
    _ = install_fake_bigquery(monkeypatch, [bq_exec_mod, bq_base_mod])

    fake_bigframes = types.ModuleType("bigframes")
    fake_conf = types.ModuleType("bigframes._config")
    fake_conf_bq = types.ModuleType("bigframes._config.bigquery_options")
    fake_conf_bq.BigQueryOptions = _FakeBigQueryOptions  # type: ignore[attr-defined]
    fake_bigframes.Session = _FakeBFSession  # type: ignore[attr-defined]
    sys.modules.setdefault("bigframes", fake_bigframes)
    sys.modules.setdefault("bigframes._config", fake_conf)
    sys.modules["bigframes._config.bigquery_options"] = fake_conf_bq
    monkeypatch.setattr(bq_exec_mod, "bigframes", fake_bigframes, raising=True)

    fake_client = FakeClient(project="p1", location="EU")
    ex = bq_exec_mod.BigQueryBFExecutor(
        project="p1",
        dataset="ds_new",
        location="EU",
        allow_create_dataset=True,
    )
    ex.client = cast(Any, fake_client)

    ex._ensure_dataset()
    ds_id = "p1.ds_new"
    assert ds_id in fake_client._datasets
    assert fake_client.get_dataset(ds_id).location == "EU"


@pytest.mark.unit
@pytest.mark.bigquery
def test_materialize_relation_fallback_to_materialize(bq_exec):
    called: dict[str, Any] = {}

    class DF:
        def materialize(self, table, mode="overwrite"):
            called["table"] = table
            called["mode"] = mode

    bq_exec._materialize_relation("out_tbl", DF(), Node(name="m", kind="python", path=Path(".")))
    assert called["table"] == "p1.ds1.out_tbl"
    assert called["mode"] == "overwrite"


@pytest.mark.unit
@pytest.mark.bigquery
def test_materialize_relation_raises_if_no_supported_method(bq_exec):
    class DF:
        columns: ClassVar[list[str]] = ["x"]

    with pytest.raises(RuntimeError):
        bq_exec._materialize_relation(
            "out_tbl", DF(), Node(name="m", kind="python", path=Path("."))
        )


@pytest.mark.unit
@pytest.mark.bigquery
def test_validate_required_single_frame_ok(bq_exec):
    frame = SimpleNamespace(columns=["a", "b", "c"])
    bq_exec._validate_required("m1", frame, {"ds1.tbl": {"a", "b"}})


@pytest.mark.unit
@pytest.mark.bigquery
def test_validate_required_single_frame_missing(bq_exec):
    frame = SimpleNamespace(columns=["a"])
    with pytest.raises(ValueError) as exc:
        bq_exec._validate_required("m1", frame, {"ds1.tbl": {"a", "b"}})
    assert "missing" in str(exc.value)


@pytest.mark.unit
@pytest.mark.bigquery
def test_validate_required_multi_input_ok(bq_exec):
    f1 = SimpleNamespace(columns=["id", "name"])
    f2 = SimpleNamespace(columns=["user_id", "order_id"])
    bq_exec._validate_required(
        "m1",
        {"ds1.users": f1, "ds1.orders": f2},
        {"ds1.users": {"id"}, "ds1.orders": {"order_id"}},
    )


@pytest.mark.unit
@pytest.mark.bigquery
def test_is_frame_detection(bq_exec):
    class WithToGbq:
        def to_gbq(self): ...

    class WithMaterialize:
        def materialize(self): ...

    class WithColumns:
        columns: ClassVar[list[str]] = ["x"]

    assert bq_exec._is_frame(WithToGbq()) is True
    assert bq_exec._is_frame(WithMaterialize()) is True
    assert bq_exec._is_frame(WithColumns()) is True
    assert bq_exec._is_frame(object()) is False


@pytest.mark.unit
@pytest.mark.bigquery
def test_columns_of_prefers_columns_attr(bq_exec):
    frame = SimpleNamespace(columns=["a", "b"])
    assert bq_exec._columns_of(frame) == ["a", "b"]


@pytest.mark.unit
@pytest.mark.bigquery
def test_columns_of_falls_back_to_schema_names(bq_exec):
    class F:
        schema = SimpleNamespace(names=["x", "y"])

    assert bq_exec._columns_of(F()) == ["x", "y"]


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
    cfg = {"identifier": "src_tbl", "project": "other_proj", "dataset": "raw"}
    ref = bq_exec._format_source_reference(cfg, "src", "tbl")
    assert "other_proj" in ref
    assert "raw" in ref
    assert "src_tbl" in ref


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

    ok = bq_exec.exists_relation("some_table")
    assert ok is True


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
def test_alter_table_sync_schema_adds_missing_columns(bq_exec):
    def fake_query(sql: str, location: str | None = None, job_config: Any | None = None):
        if "WHERE 1=0" in sql:
            return FakeJob(schema=[FakeField("id"), FakeField("new_col", "INT64")])
        if sql.startswith("ALTER TABLE"):
            bq_exec.client.queries.append((sql, location, job_config))
            return FakeJob()
        return FakeJob()

    bq_exec.client.query = fake_query  # type: ignore[assignment]
    bq_exec.client.get_table = lambda ref: SimpleNamespace(schema=[FakeField("id")])  # type: ignore[assignment]

    bq_exec.alter_table_sync_schema("existing", "SELECT 1 AS id, 2 AS new_col")
    assert any("ADD COLUMN new_col" in q[0] for q in bq_exec.client.queries)


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
    def bad_upsert(ex, name, rel, fp, eng):
        raise RuntimeError("nope")

    monkeypatch.setattr(bq_base_mod, "upsert_meta", bad_upsert)

    with pytest.raises(RuntimeError):
        bq_exec.on_node_built(Node(name="m", kind="sql", path=Path(".")), "p1.ds1.m", "fp123")


@pytest.mark.unit
@pytest.mark.bigquery
def test_bf_apply_sql_materialization_calls_super(monkeypatch, bq_exec):
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


@pytest.mark.unit
@pytest.mark.bigquery
def test_apply_sql_materialization_wraps_badrequest(monkeypatch, bq_exec):
    monkeypatch.setattr(
        BaseExecutor,
        "_apply_sql_materialization",
        lambda *a, **k: (_ for _ in ()).throw(FakeBadRequest("bad SQL")),
        raising=True,
    )

    node = Node(name="m_err", kind="sql", path=Path("."))

    with pytest.raises(RuntimeError) as exc:
        bq_exec._apply_sql_materialization(
            node,
            target_sql="`p1`.`ds1`.`broken`",
            select_body="SELECT bad",
            materialization="table",
        )
    msg = str(exc.value)
    assert "BigQuery SQL failed for" in msg
    assert "SELECT bad" in msg
    assert "broken" in msg
