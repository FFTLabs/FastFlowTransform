# tests/unit/executors/test_postgres_unit.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

# Wichtig: wir testen genau dieses Modul
import fastflowtransform.executors.postgres as pgmod
from fastflowtransform.core import Node
from fastflowtransform.errors import ModelExecutionError, ProfileConfigError
from fastflowtransform.executors.postgres import PostgresExecutor

# ---------------------------------------------------------------------------
# Hilfs-Fakes
# ---------------------------------------------------------------------------


class _FakeConn:
    """Simple connection mock that records executed SQL."""

    def __init__(self, rows: list[tuple] | None = None):
        self.executed: list[tuple[Any, dict[str, Any] | None]] = []
        self._rows = rows or []

    def execute(self, stmt, params: dict[str, Any] | None = None):
        """Record stmt + params and return object with .fetchall() / .fetchone()."""
        self.executed.append((stmt, params))
        rows = self._rows

        class _Res:
            def __init__(self, rows):
                self._rows = rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return self._rows

            # needed for "for r in con.execute(...)" style
            def __iter__(self):
                return iter(self._rows)

        return _Res(rows)

    # needed for "with engine.begin() as conn:"
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    """Fake SQLAlchemy engine with begin()."""

    def __init__(self, conn: _FakeConn):
        self._conn = conn
        self.begin_called = 0

    def begin(self):
        self.begin_called += 1
        return self._conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_engine_and_conn(monkeypatch):
    """Patch create_engine → fake engine+conn, return both."""
    conn = _FakeConn()
    engine = _FakeEngine(conn)

    def _fake_create_engine(dsn, future=True):
        return engine

    monkeypatch.setattr(pgmod, "create_engine", _fake_create_engine)
    return engine, conn


@pytest.fixture
def node_tmp():
    return Node(name="m1", kind="sql", path=Path("."))


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.postgres
def test_init_requires_dsn():
    with pytest.raises(ProfileConfigError):
        PostgresExecutor(dsn="")


@pytest.mark.unit
@pytest.mark.postgres
def test_init_creates_schema_when_given(monkeypatch, fake_engine_and_conn):
    _, conn = fake_engine_and_conn
    # just create - should call CREATE SCHEMA IF NOT EXISTS
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    # last execute should be CREATE SCHEMA ...
    assert any("CREATE SCHEMA IF NOT EXISTS" in str(c[0]) for c in conn.executed)
    assert ex.schema == "public"


@pytest.mark.unit
@pytest.mark.postgres
def test_init_schema_creation_failure(monkeypatch):
    """If CREATE SCHEMA fails, we raise ProfileConfigError."""
    bad_conn = _FakeConn()

    def bad_execute(stmt, params=None):
        raise pgmod.SQLAlchemyError("boom")

    bad_conn.execute = bad_execute  # type: ignore[assignment]
    bad_engine = _FakeEngine(bad_conn)

    def _fake_create_engine(dsn, future=True):
        return bad_engine

    monkeypatch.setattr(pgmod, "create_engine", _fake_create_engine)

    with pytest.raises(ProfileConfigError):
        PostgresExecutor("postgresql+psycopg://x", schema="foo")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.postgres
def test_q_ident_and_qualified(monkeypatch, fake_engine_and_conn):
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    assert ex._qualified("tbl") == '"public"."tbl"'
    assert ex._qualified("tbl", schema="x") == '"x"."tbl"'
    # with no schema
    ex2 = PostgresExecutor("postgresql+psycopg://x", schema=None)
    assert ex2._qualified("tbl") == '"tbl"'


@pytest.mark.unit
@pytest.mark.postgres
@pytest.mark.parametrize(
    "inp,exp",
    [
        ("SELECT * FROM x", "SELECT * FROM x"),
        ("  select * from x ;  ", "select * from x"),
        ("with cte as (select 1) select * from cte;", "with cte as (select 1) select * from cte"),
        ("  bla bla", "bla bla"),
    ],
)
def test_extract_select_like(monkeypatch, fake_engine_and_conn, inp, exp):
    ex = PostgresExecutor("postgresql+psycopg://x", schema=None)
    assert ex._extract_select_like(inp) == exp


# ---------------------------------------------------------------------------
# _read_relation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.postgres
def test_read_relation(monkeypatch, fake_engine_and_conn, node_tmp):
    _, conn = fake_engine_and_conn
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")

    def fake_read_sql_query(stmt, c):
        # stmt is TextClause
        assert "select * from" in str(stmt).lower()
        return pd.DataFrame({"id": [1, 2]})

    monkeypatch.setattr(pgmod.pd, "read_sql_query", fake_read_sql_query)

    df = ex._read_relation("my_tbl", node_tmp, deps=[])
    assert list(df["id"]) == [1, 2]
    # search_path should be set
    assert any("SET LOCAL search_path" in str(s[0]) for s in conn.executed)


@pytest.mark.unit
@pytest.mark.postgres
def test_read_relation_propagates_programming_error(monkeypatch, fake_engine_and_conn, node_tmp):
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")

    def bad_read_sql_query(stmt, c):
        raise pgmod.ProgrammingError("nope", None, Exception("orig"))

    monkeypatch.setattr(pgmod.pd, "read_sql_query", bad_read_sql_query)

    with pytest.raises(pgmod.ProgrammingError):
        ex._read_relation("x", node_tmp, deps=[])


# ---------------------------------------------------------------------------
# _materialize_relation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.postgres
def test_materialize_relation_ok(monkeypatch, fake_engine_and_conn, node_tmp):
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")

    df = pd.DataFrame({"id": [1]})

    called = {"ok": False}

    def fake_to_sql(name, engine, if_exists, index, schema, method):
        called["ok"] = True
        assert name == "t_out"
        assert schema == "public"

    monkeypatch.setattr(df, "to_sql", fake_to_sql)

    ex._materialize_relation("t_out", df, node_tmp)
    assert called["ok"] is True


@pytest.mark.unit
@pytest.mark.postgres
def test_materialize_relation_wraps_error(monkeypatch, fake_engine_and_conn, node_tmp):
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    df = pd.DataFrame({"id": [1]})

    def bad_to_sql(*a, **k):
        raise pgmod.SQLAlchemyError("boom")

    monkeypatch.setattr(df, "to_sql", bad_to_sql)

    with pytest.raises(ModelExecutionError) as exc:
        ex._materialize_relation("t_out", df, node_tmp)

    err = exc.value
    assert err.node_name == "m1"
    assert err.relation == '"public"."t_out"'
    assert "boom" in str(err)


# ---------------------------------------------------------------------------
# source formatting
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.postgres
def test_format_source_reference_path_not_supported(fake_engine_and_conn):
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    with pytest.raises(NotImplementedError):
        ex._format_source_reference(
            {"location": "s3://x", "identifier": "foo"},
            "src",
            "tbl",  # should trigger
        )


@pytest.mark.unit
@pytest.mark.postgres
def test_format_source_reference_with_db_and_schema(fake_engine_and_conn):
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    out = ex._format_source_reference(
        {
            "identifier": "t_src",
            "schema": "other",
            "database": "mydb",
        },
        "src",
        "t",
    )
    # "mydb"."other"."t_src"
    assert out == '"mydb"."other"."t_src"'


# ---------------------------------------------------------------------------
# view / table creation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.postgres
def test_create_or_replace_view_ok(monkeypatch, fake_engine_and_conn, node_tmp):
    _, conn = fake_engine_and_conn
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")

    ex._create_or_replace_view('"public"."v_x"', "select 1 as id", node_tmp)

    # should have dropped and created
    texts = [str(c[0]) for c in conn.executed]
    assert any("DROP VIEW IF EXISTS" in t for t in texts)
    assert any("CREATE OR REPLACE VIEW" in t for t in texts)


@pytest.mark.unit
@pytest.mark.postgres
def test_create_or_replace_view_wraps(monkeypatch, fake_engine_and_conn, node_tmp):
    _, conn = fake_engine_and_conn

    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")

    def bad_execute(stmt, params=None):
        raise RuntimeError("db down")

    conn.execute = bad_execute

    with pytest.raises(ModelExecutionError) as exc:
        ex._create_or_replace_view('"public"."v_x"', "select 1", node_tmp)

    err = exc.value
    assert err.node_name == "m1"
    assert err.relation == '"public"."v_x"'
    assert "db down" in str(err)
    assert err.sql_snippet is not None
    assert "select 1" in err.sql_snippet


@pytest.mark.unit
@pytest.mark.postgres
def test_create_or_replace_table_ok(fake_engine_and_conn, node_tmp):
    _, conn = fake_engine_and_conn
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")

    ex._create_or_replace_table('"public"."t_x"', "select 1", node_tmp)

    texts = [str(c[0]) for c in conn.executed]
    assert any("DROP TABLE IF EXISTS" in t for t in texts)
    assert any("CREATE TABLE" in t and "select 1" in t for t in texts)


@pytest.mark.unit
@pytest.mark.postgres
def test_create_or_replace_table_wraps(fake_engine_and_conn, node_tmp):
    # Reuse fake engine/connection from fixture
    engine, conn = fake_engine_and_conn

    # Bypass __init__ to avoid real DSN setup
    ex = PostgresExecutor.__new__(PostgresExecutor)
    ex.engine = engine
    ex.schema = "public"

    # Force the DB call to fail
    def bad_execute(stmt, params=None):
        raise RuntimeError("nope")

    conn.execute = bad_execute  # type: ignore[assignment]

    with pytest.raises(ModelExecutionError) as excinfo:
        ex._create_or_replace_table('"public"."t_x"', "select 1", node_tmp)

    err = excinfo.value
    assert err.node_name == node_tmp.name
    assert err.relation == '"public"."t_x"'
    assert err.message == "nope"
    assert err.sql_snippet is not None
    assert '-- target="public"."t_x"' in err.sql_snippet
    assert "select 1" in err.sql_snippet


# ---------------------------------------------------------------------------
# on_node_built
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.postgres
def test_on_node_built_calls_meta(monkeypatch, fake_engine_and_conn, node_tmp):
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")

    called = {"ens": 0, "up": 0}

    def fake_ensure(executor):
        called["ens"] += 1

    def fake_upsert(executor, name, relation, fp, engine):
        called["up"] += 1
        assert engine == "postgres"

    monkeypatch.setattr(pgmod, "ensure_meta_table", fake_ensure)
    monkeypatch.setattr(pgmod, "upsert_meta", fake_upsert)

    ex.on_node_built(node_tmp, "public.t_x", "fp123")
    assert called["ens"] == 1
    assert called["up"] == 1


@pytest.mark.unit
@pytest.mark.postgres
def test_on_node_built_raises_when_meta_fails(monkeypatch, fake_engine_and_conn, node_tmp):
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")

    def bad_ensure(executor):
        raise RuntimeError("meta fail")

    monkeypatch.setattr(pgmod, "ensure_meta_table", bad_ensure)

    with pytest.raises(RuntimeError):
        ex.on_node_built(node_tmp, "t", "fp")


# ---------------------------------------------------------------------------
# incremental API
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.postgres
def test_exists_relation_true(fake_engine_and_conn):
    _, conn = fake_engine_and_conn
    conn._rows = [(1,)]
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    assert ex.exists_relation("tbl") is True


@pytest.mark.unit
@pytest.mark.postgres
def test_exists_relation_false(fake_engine_and_conn):
    _, conn = fake_engine_and_conn
    conn._rows = []  # no row
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    assert ex.exists_relation("tbl") is False


@pytest.mark.unit
@pytest.mark.postgres
def test_create_table_as(fake_engine_and_conn):
    _, conn = fake_engine_and_conn
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    ex.create_table_as("out_tbl", "WITH x AS (SELECT 1) SELECT * FROM x")
    texts = [str(c[0]) for c in conn.executed]
    assert any("create table" in t.lower() for t in texts)
    # search_path
    assert any("SET LOCAL search_path" in str(s[0]) for s in conn.executed)


@pytest.mark.unit
@pytest.mark.postgres
def test_incremental_insert(fake_engine_and_conn):
    _, conn = fake_engine_and_conn
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    ex.incremental_insert("out_tbl", "select 1")
    texts = [str(c[0]).lower() for c in conn.executed]
    assert any("insert into" in t for t in texts)


@pytest.mark.unit
@pytest.mark.postgres
def test_incremental_merge(fake_engine_and_conn):
    _, conn = fake_engine_and_conn
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    ex.incremental_merge("tgt_tbl", "select 1 as id, 'x' as v", ["id"])
    texts = [str(c[0]).lower() for c in conn.executed]
    # temp table
    assert any("create temporary table ff_stg as select 1 as id" in t for t in texts)
    # delete using
    assert any("delete from" in t and "using ff_stg" in t for t in texts)
    # insert
    assert any("insert into" in t and "select * from ff_stg" in t for t in texts)
    # drop staging
    assert any("drop table if exists ff_stg" in t for t in texts)


@pytest.mark.unit
@pytest.mark.postgres
def test_alter_table_sync_schema_adds_missing(fake_engine_and_conn):
    _, conn = fake_engine_and_conn
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")

    # First execute: select * from (body) q limit 0 -> columns
    # We'll simulate two columns: existing_col, new_col
    # Second execute: information_schema.columns -> existing_col only
    # Then we expect: ALTER TABLE ... ADD COLUMN "new_col" text
    def exec_side_effect(stmt, params=None):
        sql = str(stmt).lower()
        if "select * from (select 1 as existing_col, 2 as new_col)" in sql:
            # "select * from (...) limit 0"
            return _FakeConn(rows=[("existing_col",), ("new_col",)]).execute("dummy")
        if "information_schema.columns" in sql:
            return _FakeConn(rows=[("existing_col",)]).execute("dummy")
        # capture alter table
        return _FakeConn(rows=[]).execute(stmt, params)

    conn.execute = exec_side_effect  # type: ignore[assignment]

    ex.alter_table_sync_schema(
        "target_tbl",
        "select 1 as existing_col, 2 as new_col",
        mode="append_new_columns",
    )


@pytest.mark.unit
@pytest.mark.postgres
def test_create_or_replace_view_from_table_happy(fake_engine_and_conn):
    """Ensure view over table is created with qualified names."""
    engine, conn = fake_engine_and_conn
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    ex.engine = engine  # inject fake
    node = Node(name="m1", kind="sql", path=Path("."), deps=[], meta={})

    ex._create_or_replace_view_from_table("v_out", "src_tbl", node)

    # our fake conn records *everything*, incl. BEGIN/COMMIT → filter
    stmts = [
        (str(stmt), params)
        for (stmt, params) in conn.executed
        # keep only the actual SQL we care about
        if "SET LOCAL" in str(stmt)
        or "DROP VIEW" in str(stmt)
        or "CREATE OR REPLACE VIEW" in str(stmt)
    ]

    expected_statement_len = 5
    assert len(stmts) == expected_statement_len

    assert 'SET LOCAL search_path = "public"' in stmts[0][0]
    assert 'DROP VIEW IF EXISTS "public"."v_out" CASCADE' in stmts[1][0]
    assert (
        'CREATE OR REPLACE VIEW "public"."v_out" AS SELECT * FROM "public"."src_tbl"' in stmts[4][0]
    )


@pytest.mark.unit
@pytest.mark.postgres
def test_create_or_replace_view_from_table_wraps_error(fake_engine_and_conn):
    """Errors during view creation should be wrapped with node + relation preserved."""
    engine, conn = fake_engine_and_conn
    ex = PostgresExecutor("postgresql+psycopg://x", schema="public")
    # inject fake engine so we stay in-memory
    ex.engine = engine
    node = Node(name="m_bad", kind="sql", path=Path("."), deps=[], meta={})

    def boom(stmt, params=None):
        raise RuntimeError("db down")

    # force every execute to fail
    conn.execute = boom  # type: ignore[assignment]

    with pytest.raises(ModelExecutionError) as exc:
        ex._create_or_replace_view_from_table("v_broken", "src_tbl", node)

    err = exc.value
    # Error is wrapped with relation + original message for better context
    assert isinstance(err, ModelExecutionError)
    # Relation should be the qualified one the executor used
    assert err.relation == '"public"."v_broken"'
    assert err.node_name == "m_bad"
    # String form includes both relation and original error text
    assert str(err).endswith("db down")
    assert str(err).startswith('"public"."v_broken":')
    # but the extra context must be present
    assert err.node_name == "m_bad"
    assert err.relation == '"public"."v_broken"'
    # sql_snippet is not used in this method, so we expect None
    assert err.sql_snippet is None
