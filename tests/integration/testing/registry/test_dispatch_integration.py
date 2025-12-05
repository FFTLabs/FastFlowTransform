import pytest

from fastflowtransform.executors.duckdb import DuckExecutor
from fastflowtransform.testing.registry import TESTS


@pytest.mark.integration
@pytest.mark.duckdb
def test_registry_not_null_and_unique_and_params_and_sql():
    ex = DuckExecutor(":memory:")
    ex.con.execute("create table t(id int, email varchar)")
    ex.con.execute("insert into t values (1,'a@example.com'),(1,'b@example.com'),(2,null)")

    ok1, msg1, sql1 = TESTS["not_null"](ex, "t", "email", {})
    assert not ok1 and "is null" in (msg1 or "").lower()
    assert "select count(*) from t where email is null" in (sql1 or "").lower()

    ok2, msg2, sql2 = TESTS["unique"](ex, "t", "id", {})
    assert not ok2 and "duplicate" in (msg2 or "").lower()
    assert "group by 1 having count(*) > 1" in (sql2 or "").lower()


@pytest.mark.integration
@pytest.mark.duckdb
def test_registry_relationships_runner():
    ex = DuckExecutor(":memory:")
    ex.con.execute("create table dim_users(id int)")
    ex.con.execute("create table fact_users(user_id int)")
    ex.con.execute("insert into dim_users values (1)")
    ex.con.execute("insert into fact_users values (1),(2)")

    ok, msg, sql = TESTS["relationships"](
        ex,
        "fact_users",
        "user_id",
        {"to": "dim_users"},
    )
    assert not ok
    assert "orphan" in (msg or "").lower()
    assert "left join" in (sql or "").lower()

    ex.con.execute("delete from fact_users where user_id = 2")
    ok2, msg2, _ = TESTS["relationships"](
        ex,
        "fact_users",
        "user_id",
        {"to": "dim_users"},
    )
    assert ok2 and msg2 is None
