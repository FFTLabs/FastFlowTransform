# tests/unit/executors/test_snowflake_snowpark_unit.py
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import fastflowtransform.executors.snowflake_snowpark as sf_mod

# ---------------------------------------------------------------------------
# 1) Install a fake snowflake.snowpark BEFORE importing the executor module
# ---------------------------------------------------------------------------

fake_sf_pkg = sys.modules.setdefault("snowflake", types.ModuleType("snowflake"))
fake_sf_snowpark_mod = types.ModuleType("snowflake.snowpark")


class _FakeSchema:
    """Minimal schema object exposing .names like real Snowpark dataframes do."""

    def __init__(self, names: list[str]):
        self.names = names


class FakeSnowparkDataFrame:
    """
    Very small stand-in for snowflake.snowpark.DataFrame.
    It needs:
      - .schema.names
      - .collect()
      - .toDF()
      - .write.save_as_table(...)
    """

    def __init__(self, session: FakeSession, sql: str | None = None, cols: list[str] | None = None):
        self._session = session
        self._sql = sql
        self.schema = _FakeSchema(cols or [])
        self._rows: list[dict[str, Any]] = []

    # allow tests to inject rows
    def _with_rows(self, rows: list[dict[str, Any]]) -> FakeSnowparkDataFrame:
        self._rows = rows
        return self

    def collect(self) -> list[dict[str, Any]]:
        # return rows that were set, otherwise empty
        return self._rows

    class _Writer:
        def __init__(self, outer: FakeSnowparkDataFrame):
            self._outer = outer

        def save_as_table(self, name: str, mode: str = "overwrite") -> None:
            # just record the call on the session
            self._outer._session.write_calls.append(("save_as_table", name, mode))

    @property
    def write(self) -> FakeSnowparkDataFrame._Writer:  # type: ignore[name-defined]
        return FakeSnowparkDataFrame._Writer(self)

    def toDF(self, *cols: str) -> FakeSnowparkDataFrame:
        return FakeSnowparkDataFrame(self._session, self._sql, list(cols))


# Make sure the executor treats FakeSnowparkDataFrame as SNDF when optional deps are missing.
sf_mod.SNDF = FakeSnowparkDataFrame  # type: ignore[attr-defined]


class FakeSession:
    """
    Minimal session mock:
      - .sql(sql) -> FakeSnowparkDataFrame
      - .table(name) -> FakeSnowparkDataFrame
      - remembers SQL calls
    """

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.sql_calls: list[str] = []
        self.table_calls: list[str] = []
        self.write_calls: list[tuple[str, str, str]] = []

    # --- behaviours that depend on the SQL string ---
    def _df_for_sql(self, sql: str) -> FakeSnowparkDataFrame:
        """
        Create a DF whose columns/rows depend on the SQL text.
        We only simulate the bits the executor actually uses.
        """
        # 1) exists_relation → information_schema.tables → return 1 row
        if "information_schema.tables" in sql.lower():
            return FakeSnowparkDataFrame(self, sql, [])._with_rows([{"1": 1}])

        # 2) alter_table_sync_schema → information_schema.columns
        if "information_schema.columns" in sql.lower():
            # Pretend there is already an "id" column
            return FakeSnowparkDataFrame(self, sql, [])._with_rows([{"COLUMN_NAME": "ID"}])

        # 3) probe SELECT ... WHERE 1=0 → provide schema names
        if "where 1=0" in sql.lower():
            # executor expects to read probe.schema.names
            return FakeSnowparkDataFrame(self, sql, ["ID", "NEW_COL"])

        # 4) everything else → empty df, no rows
        return FakeSnowparkDataFrame(self, sql, [])

    def sql(self, sql: str) -> FakeSnowparkDataFrame:
        self.sql_calls.append(sql)
        return self._df_for_sql(sql)

    def table(self, name: str) -> FakeSnowparkDataFrame:
        self.table_calls.append(name)
        # pretend table has 2 columns
        return FakeSnowparkDataFrame(self, name, ["ID", "NAME"])


class _FakeSessionBuilder:
    def __init__(self):
        self._cfg: dict[str, Any] = {}

    def configs(self, cfg: dict[str, Any]) -> _FakeSessionBuilder:
        self._cfg = cfg
        return self

    def create(self) -> FakeSession:
        return FakeSession(self._cfg)


# expose as snowflake.snowpark.Session
fake_sf_snowpark_mod.Session = SimpleNamespace(builder=_FakeSessionBuilder())  # type: ignore[attr-defined]
# expose DataFrame type so isinstance(..., SNDF) works
fake_sf_snowpark_mod.DataFrame = FakeSnowparkDataFrame  # type: ignore[attr-defined]

fake_sf_snowpark_mod.DataFrame = FakeSnowparkDataFrame  # type: ignore[attr-defined]
sys.modules["snowflake.snowpark"] = fake_sf_snowpark_mod
sf = fake_sf_snowpark_mod

# ---------------------------------------------------------------------------
# 2) Now we can safely import the module under test
# ---------------------------------------------------------------------------
import fastflowtransform.executors.snowflake_snowpark as sf_exec_mod  # noqa: E402
from fastflowtransform.core import Node  # noqa: E402
from fastflowtransform.executors.snowflake_snowpark import (  # noqa: E402
    SnowflakeSnowparkExecutor,
)


@pytest.fixture
def sf_exec(monkeypatch):
    """
    Build an executor with a fake Snowpark session.
    """
    # make sure the module uses our just-registered fake
    monkeypatch.setattr(sf_exec_mod, "Session", fake_sf_snowpark_mod.Session, raising=True)

    cfg = {
        "account": "acc",
        "user": "usr",
        "password": "pwd",
        "warehouse": "wh",
        "database": "DB1",
        "schema": "SC1",
        "allow_create_schema": True,
    }
    ex = SnowflakeSnowparkExecutor(cfg)
    # sanity: we actually got our fake session
    assert isinstance(ex.session, FakeSession)
    return ex


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_init_sets_db_schema_and_con(sf_exec):
    assert sf_exec.database == "DB1"
    assert sf_exec.schema == "SC1"


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_q_and_qualified(sf_exec):
    assert sf_exec._qualified("TBL") == "DB1.SC1.TBL"


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_read_relation_calls_session_table(sf_exec):
    node = Node(name="n", kind="sql", path=Path("."))
    df = sf_exec._read_relation("MY_TBL", node, deps=[])
    assert isinstance(df, FakeSnowparkDataFrame)
    # session should have been asked for the fully qualified name
    assert sf_exec.session.table_calls == ["DB1.SC1.MY_TBL"]


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_materialize_relation_happy(sf_exec, monkeypatch):
    called: dict[str, str] = {}

    fake_df = SimpleNamespace(
        write=SimpleNamespace(
            save_as_table=lambda tbl, mode="overwrite": (
                called.setdefault("table", tbl),
                called.setdefault("mode", mode),
            )
        )
    )
    fake_df.schema = SimpleNamespace(names=["col1"])

    def _fake_to_df(*cols: str):
        return SimpleNamespace(
            write=fake_df.write,
            schema=SimpleNamespace(names=list(cols)),
            toDF=_fake_to_df,
        )

    fake_df.toDF = _fake_to_df

    monkeypatch.setattr(
        sf_exec,
        "_is_frame",
        lambda obj: obj is fake_df,
        raising=True,
    )
    fake_df.schema = SimpleNamespace(names=["col1"])

    node = Node(name="m", kind="python", path=Path("."))

    # ACT
    sf_exec._materialize_relation("OUT_TBL", fake_df, node)

    # ASSERT
    assert called["table"] == "DB1.SC1.OUT_TBL"
    assert called["mode"] == "overwrite"


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_materialize_relation_raises_on_non_frame(sf_exec):
    node = Node(name="m", kind="python", path=Path("."))
    with pytest.raises(TypeError):
        sf_exec._materialize_relation("OUT_TBL", object(), node)


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_create_view_over_table_issues_sql(sf_exec):
    node = Node(name="x", kind="sql", path=Path("."))
    sf_exec._create_view_over_table("V_USERS", "USERS", node)
    assert any("CREATE OR REPLACE VIEW" in s for s in sf_exec.session.sql_calls)
    sql = sf_exec.session.sql_calls[-1]
    assert "DB1.SC1.V_USERS" in sql
    assert "DB1.SC1.USERS" in sql


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_validate_required_single_df_ok(sf_exec):
    df = FakeSnowparkDataFrame(sf_exec.session, cols=["ID", "NAME", "AGE"])

    df.schema = SimpleNamespace(names=["ID", "NAME", "AGE"])  # type: ignore[attr-defined]

    sf_exec._validate_required(
        "model1",
        df,
        {"DB1.SC1.USERS": {"ID", "NAME"}},
    )


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_validate_required_single_df_missing(sf_exec):
    df = FakeSnowparkDataFrame(sf_exec.session, cols=["ID", "NAME", "AGE"])

    df.schema = SimpleNamespace(names=["ID"])  # type: ignore[attr-defined]

    with pytest.raises(ValueError) as exc:
        sf_exec._validate_required(
            "model1",
            df,
            {"DB1.SC1.USERS": {"ID", "NAME"}},
        )

    msg = str(exc.value)
    assert "missing columns" in msg
    assert "name" in msg.lower()
    assert "id" in msg.lower()


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_validate_required_multi_input_ok(sf_exec):
    df1 = FakeSnowparkDataFrame(sf_exec.session, cols=["ID", "NAME"])
    df2 = FakeSnowparkDataFrame(sf_exec.session, cols=["USER_ID", "ORDER_ID"])
    sf_exec._validate_required(
        "model2",
        {"DB1.SC1.USERS": df1, "DB1.SC1.ORDERS": df2},
        {"DB1.SC1.USERS": {"ID"}, "DB1.SC1.ORDERS": {"ORDER_ID"}},
    )  # no raise


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_validate_required_multi_input_missing_key(sf_exec):
    df1 = FakeSnowparkDataFrame(sf_exec.session, cols=["ID", "NAME"])
    with pytest.raises(ValueError) as exc:
        sf_exec._validate_required(
            "model2",
            {"DB1.SC1.USERS": df1},
            {"DB1.SC1.USERS": {"ID"}, "DB1.SC1.ORDERS": {"ORDER_ID"}},
        )
    assert "missing dependency key 'DB1.SC1.ORDERS'" in str(exc.value)


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_validate_required_is_case_insensitive(sf_exec):
    df = FakeSnowparkDataFrame(sf_exec.session, cols=["ID", "NAME", "AGE"])

    # Snowflake-style upper-case physical columns
    df.schema = SimpleNamespace(names=["USER_ID", "EMAIL"])  # type: ignore[attr-defined]

    # Logical requirements in lower-case should still pass
    sf_exec._validate_required(
        "model_ci",
        df,
        {"DB1.SC1.USERS": {"user_id", "email"}},
    )


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_columns_of(sf_exec):
    df = FakeSnowparkDataFrame(sf_exec.session, cols=["A", "B"])
    assert sf_exec._columns_of(df) == ["A", "B"]


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_is_frame(sf_exec):
    df = sf.DataFrame(sf_exec.session)
    df.schema = SimpleNamespace(names=["ID"])  # type: ignore[attr-defined]

    assert sf_exec._is_frame(df) is True
    assert sf_exec._is_frame(object()) is False


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_frame_name(sf_exec):
    assert sf_exec._frame_name() == "Snowpark"


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_format_relation_for_ref(sf_exec):
    r = sf_exec._format_relation_for_ref("my_model")
    assert r == "DB1.SC1.my_model"


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_format_source_reference_happy(sf_exec):
    cfg = {"identifier": "SRC_TBL", "database": "DBX", "schema": "RAW"}
    ref = sf_exec._format_source_reference(cfg, "src", "tbl")
    assert ref == "DBX.RAW.SRC_TBL"


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_format_source_reference_raises_on_location(sf_exec):
    cfg = {"identifier": "X", "location": "s3://foo"}
    with pytest.raises(NotImplementedError):
        sf_exec._format_source_reference(cfg, "src", "tbl")


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_format_source_reference_raises_on_missing_identifier(sf_exec):
    with pytest.raises(KeyError):
        sf_exec._format_source_reference({}, "src", "tbl")


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_create_or_replace_view_calls_session_sql(sf_exec):
    node = Node(name="x", kind="sql", path=Path("."))
    sf_exec._create_or_replace_view('"DB1"."SC1"."V1"', "SELECT 1", node)
    assert any("CREATE OR REPLACE VIEW" in s for s in sf_exec.session.sql_calls)


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_create_or_replace_table_calls_session_sql(sf_exec):
    node = Node(name="x", kind="sql", path=Path("."))
    sf_exec._create_or_replace_table('"DB1"."SC1"."T1"', "SELECT 1", node)
    assert any("CREATE OR REPLACE TABLE" in s for s in sf_exec.session.sql_calls)


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_create_or_replace_view_from_table_calls_session_sql(sf_exec):
    node = Node(name="x", kind="sql", path=Path("."))
    sf_exec._create_or_replace_view_from_table("V1", "T1", node)
    sql = sf_exec.session.sql_calls[-1]
    assert "CREATE OR REPLACE VIEW" in sql
    assert "DB1.SC1.V1" in sql
    assert "DB1.SC1.T1" in sql


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_on_node_built_calls_meta(monkeypatch, sf_exec):
    called = {"ensure": 0, "upsert": 0}

    def fake_ensure(ex):
        called["ensure"] += 1

    def fake_upsert(ex, name, rel, fp, eng):
        called["upsert"] += 1

    monkeypatch.setattr(sf_exec_mod, "ensure_meta_table", fake_ensure)
    monkeypatch.setattr(sf_exec_mod, "upsert_meta", fake_upsert)

    sf_exec.on_node_built(Node(name="m", kind="sql", path=Path(".")), '"DB1"."SC1"."M"', "fp123")

    assert called["ensure"] == 1
    assert called["upsert"] == 1


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_on_node_built_raises_on_meta_error(monkeypatch, sf_exec):
    def bad_upsert(ex, name, rel, fp, eng):
        raise RuntimeError("meta boom")

    monkeypatch.setattr(sf_exec_mod, "upsert_meta", bad_upsert)

    with pytest.raises(RuntimeError):
        sf_exec.on_node_built(
            Node(name="m", kind="sql", path=Path(".")), '"DB1"."SC1"."M"', "fp123"
        )


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_exists_relation_true(sf_exec, monkeypatch):
    # our fake session already returns one row for information_schema.tables
    ok = sf_exec.exists_relation("SOME_TBL")
    assert ok is True


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_exists_relation_false_on_error(sf_exec, monkeypatch):
    def boom(sql: str):
        raise RuntimeError("bad")

    monkeypatch.setattr(sf_exec.session, "sql", boom, raising=True)
    ok = sf_exec.exists_relation("SOME_TBL")
    assert ok is False


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_create_table_as_strips_semicolon(sf_exec):
    sf_exec.session.sql_calls.clear()
    sf_exec.create_table_as("DST", "SELECT 1;")
    sql = sf_exec.session.sql_calls[-1]
    assert "CREATE OR REPLACE TABLE" in sql
    assert "SELECT 1" in sql
    assert not sql.strip().endswith(";")


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_incremental_insert_strips_semicolon(sf_exec):
    sf_exec.session.sql_calls.clear()
    sf_exec.incremental_insert("DST", "SELECT 1;")
    sql = sf_exec.session.sql_calls[-1]
    assert "INSERT INTO" in sql
    assert "SELECT 1" in sql


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_incremental_merge_builds_two_statements(sf_exec):
    sf_exec.session.sql_calls.clear()

    sf_exec.incremental_merge("DST", "SELECT 1 AS id", ["id"])

    # Each statement incurs an EXPLAIN + actual execution → 4 SQL calls total.
    assert len(sf_exec.session.sql_calls) == 4
    dml_calls = [
        sql
        for sql in sf_exec.session.sql_calls
        if sql.strip().upper().startswith("DELETE") or sql.strip().upper().startswith("INSERT")
    ]
    assert len(dml_calls) == 2
    delete_sql, insert_sql = dml_calls

    # First: DELETE FROM ... USING (<body>) AS s WHERE ...
    assert "DELETE FROM" in delete_sql
    assert "USING (" in delete_sql

    # Second: INSERT INTO ... SELECT * FROM (<body>)
    assert "INSERT INTO" in insert_sql
    assert "SELECT * FROM (" in insert_sql


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_alter_table_sync_schema_adds_missing(sf_exec):
    sf_exec.session.sql_calls.clear()
    sf_exec.alter_table_sync_schema("EXISTING", "SELECT 1 AS ID, 2 AS NEW_COL")
    # last sql should be ALTER TABLE ... ADD COLUMN ...
    assert any("ALTER TABLE" in s and "ADD COLUMN" in s for s in sf_exec.session.sql_calls)


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_alter_table_sync_schema_noop_on_unknown_mode(sf_exec):
    sf_exec.session.sql_calls.clear()
    sf_exec.alter_table_sync_schema("EXISTING", "SELECT 1", mode="replace_all")  # unknown
    # no new calls
    assert sf_exec.session.sql_calls == []


@pytest.mark.unit
@pytest.mark.snowflake_snowpark
def test_sfcursorshim_execute_returns_rows(sf_exec):
    class FakeRow:
        """Mimic Snowpark Row: has attributes *and* asDict()."""

        def __init__(self, data: dict[str, Any]):
            self._data = data
            # make attributes accessible: r.A, r.B, ...
            for k, v in data.items():
                setattr(self, k, v)

        def asDict(self) -> dict[str, Any]:
            return self._data

    class DFWithRows(FakeSnowparkDataFrame):
        def __init__(self, session, sql, cols, rows):
            super().__init__(session, sql, cols)
            self._rows = rows

        def collect(self) -> list[Any]:  # type: ignore[override]
            return self._rows

    def fake_sql(sql: str):
        return DFWithRows(
            sf_exec.session,
            sql,
            ["A", "B"],
            [
                FakeRow({"A": 1, "B": "x"}),
                FakeRow({"A": 2, "B": "y"}),
            ],
        )

    # executor auf unseren fake umbiegen
    sf_exec.session.sql = fake_sql  # type: ignore[assignment]

    # ACT
    res = sf_exec.execute_test_sql("SELECT 1")
    assert res.fetchall() == [(1, "x"), (2, "y")]

    res = sf_exec.execute_test_sql("SELECT 1")
    assert res.fetchone() == (1, "x")
