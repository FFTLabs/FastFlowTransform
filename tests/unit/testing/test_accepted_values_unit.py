import pytest

from fastflowtransform.executors.duckdb import DuckExecutor
from fastflowtransform.testing.base import TestFailure, accepted_values


@pytest.mark.unit
def test_accepted_values_pass_and_fail():
    ex = DuckExecutor(":memory:")
    ex.con.execute("create table t(id int, email varchar)")
    ex.con.execute("insert into t values (1,'a@example.com'),(2,'b@example.com')")
    # Pass
    accepted_values(ex, "t", "email", values=["a@example.com", "b@example.com"])

    # Fail
    with pytest.raises(TestFailure):
        accepted_values(ex, "t", "email", values=["a@example.com"])
