# tests/unit/test_testing_unit.py
from __future__ import annotations

from typing import Any, cast

import pytest

from fastflowtransform.executors.base import BaseExecutor
from fastflowtransform.testing.base import (
    TestFailure,
    _fail,
    _pretty_sql,
    _scalar,
    accepted_values,
    greater_equal,
    non_negative_sum,
    not_null,
    reconcile_coverage,
    reconcile_diff_within,
    reconcile_equal,
    reconcile_ratio_within,
    relationships,
    row_count_between,
    sql_list,
    unique,
)


class _FakeResult:
    """Tiny fake fetch result for tests."""

    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeExecutor:
    """
    Minimal executor-like helper for tests.

    - execute_test_sql: returns handler(sql)
    - execute: forwards to execute_test_sql so _scalar/_exec paths work
    - optional compute_freshness_delay_minutes hook when provided
    """

    def __init__(self, handler: Any, freshness_handler: Any | None = None):
        self.handler = handler
        self.freshness_handler = freshness_handler
        self.calls: list[Any] = []

    def execute_test_sql(self, sql: Any) -> Any:
        self.calls.append(sql)
        return self.handler(sql)

    def execute(self, sql: Any) -> Any:
        # allow _exec to call .execute on non-executor objects
        return self.execute_test_sql(sql)


# ---------------------------------------------------------------------------
# _pretty_sql / _sql_list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pretty_sql_plain():
    assert _pretty_sql(" select 1 ") == "select 1"


@pytest.mark.unit
def test_pretty_sql_tuple():
    out = _pretty_sql(("select 1", {"x": 1}))
    assert out.startswith("select 1")
    assert "params={'x': 1}" in out


@pytest.mark.unit
def test_pretty_sql_sequence():
    out = _pretty_sql(["select 1", "select 2"])
    assert "select 1" in out
    assert "select 2" in out
    assert out.startswith("[")
    assert out.endswith("]")


@pytest.mark.unit
def test_sql_list_various_types():
    assert sql_list([1, 2, 3]) == "1, 2, 3"
    assert sql_list(["a", "b"]) == "'a', 'b'"
    assert sql_list([None, "O'Reilly"]) == "NULL, 'O''Reilly'"


# ---------------------------------------------------------------------------
# _scalar
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scalar_returns_first_value():
    execu = _FakeExecutor(lambda sql: _FakeResult([(42, "x")]))
    v = _scalar(cast(BaseExecutor, execu), "select 42")
    expected_value = 42
    assert v == expected_value


@pytest.mark.unit
def test_scalar_returns_none_on_empty():
    execu = _FakeExecutor(lambda sql: _FakeResult([]))
    v = _scalar(cast(BaseExecutor, execu), "select 42")
    assert v is None


# ---------------------------------------------------------------------------
# accepted_values
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_accepted_values_ok():
    # first call: count(*) = 0 → ok
    # second call (sample) should not be executed
    execu = _FakeExecutor(
        lambda sql: _FakeResult([(0,)]) if "count(*)" in sql else _FakeResult([]),
    )
    accepted_values(cast(BaseExecutor, execu), "tbl", "col", values=["a", "b"])
    assert len(execu.calls) == 1


@pytest.mark.unit
def test_accepted_values_fail_collects_samples():
    execu = _FakeExecutor(
        lambda sql: _FakeResult([(3,)]) if "count(*)" in sql else _FakeResult([("X",), ("Y",)]),
    )
    with pytest.raises(TestFailure) as exc:
        accepted_values(cast(BaseExecutor, execu), "x.tbl", "kind", values=["A", "B"])
    msg = str(exc.value)
    assert "x.tbl.kind has 3 value(s) outside accepted set" in msg
    # should include sample values
    assert "X" in msg or "Y" in msg


# ---------------------------------------------------------------------------
# not_null / unique
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_not_null_ok():
    execu = _FakeExecutor(lambda sql: _FakeResult([(0,)]))
    # should not raise
    not_null(cast(BaseExecutor, execu), "tbl", "col")


@pytest.mark.unit
def test_not_null_fails_on_nulls():
    execu = _FakeExecutor(lambda sql: _FakeResult([(2,)]))
    with pytest.raises(TestFailure) as exc:
        not_null(cast(BaseExecutor, execu), "tbl", "col")
    assert "has 2 NULL-values" in str(exc.value)


@pytest.mark.unit
def test_not_null_wraps_db_error():
    execu = _FakeExecutor(
        lambda sql: (_ for _ in ()).throw(RuntimeError("undefinedcolumn: foo HAVING"))
    )
    with pytest.raises(TestFailure) as exc:
        not_null(cast(BaseExecutor, execu), "tbl", "col")
    msg = str(exc.value).lower()
    assert "error in tbl.col" in msg
    assert "undefinedcolumn" in msg
    assert "having" in msg or "note: postgres does not permit alias usage" in msg


@pytest.mark.unit
def test_unique_ok():
    execu = _FakeExecutor(lambda sql: _FakeResult([(0,)]))
    unique(cast(BaseExecutor, execu), "tbl", "col")


@pytest.mark.unit
def test_unique_fails():
    execu = _FakeExecutor(lambda sql: _FakeResult([(5,)]))
    with pytest.raises(TestFailure) as exc:
        unique(cast(BaseExecutor, execu), "tbl", "col")
    assert "contains 5 duplicates" in str(exc.value)


# ---------------------------------------------------------------------------
# numeric checks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_greater_equal_ok():
    execu = _FakeExecutor(lambda sql: _FakeResult([(0,)]))
    greater_equal(cast(BaseExecutor, execu), "tbl", "amount", threshold=10)


@pytest.mark.unit
def test_greater_equal_fails():
    execu = _FakeExecutor(lambda sql: _FakeResult([(3,)]))
    with pytest.raises(TestFailure) as exc:
        greater_equal(cast(BaseExecutor, execu), "tbl", "amount", threshold=10)
    assert "has 3 values < 10" in str(exc.value)


@pytest.mark.unit
def test_non_negative_sum_ok():
    execu = _FakeExecutor(lambda sql: _FakeResult([(0,)]))
    non_negative_sum(cast(BaseExecutor, execu), "tbl", "amount")


@pytest.mark.unit
def test_non_negative_sum_fails():
    execu = _FakeExecutor(lambda sql: _FakeResult([(-5,)]))
    with pytest.raises(TestFailure) as exc:
        non_negative_sum(cast(BaseExecutor, execu), "tbl", "amount")
    assert "is negative: -5" in str(exc.value)


@pytest.mark.unit
def test_row_count_between_ok():
    execu = _FakeExecutor(lambda sql: _FakeResult([(5,)]))
    row_count_between(cast(BaseExecutor, execu), "tbl", min_rows=1, max_rows=10)


@pytest.mark.unit
def test_row_count_between_too_few():
    execu = _FakeExecutor(lambda sql: _FakeResult([(0,)]))
    with pytest.raises(TestFailure):
        row_count_between(cast(BaseExecutor, execu), "tbl", min_rows=1)


@pytest.mark.unit
def test_row_count_between_too_many():
    execu = _FakeExecutor(lambda sql: _FakeResult([(50,)]))
    with pytest.raises(TestFailure):
        row_count_between(cast(BaseExecutor, execu), "tbl", min_rows=1, max_rows=10)


# ---------------------------------------------------------------------------
# reconcile_* helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reconcile_equal_exact_ok():
    execu = _FakeExecutor(lambda sql: _FakeResult([(10,)]))
    reconcile_equal(
        cast(BaseExecutor, execu),
        left={"table": "a", "expr": "sum(x)"},
        right={"table": "b", "expr": "sum(y)"},
    )


@pytest.mark.unit
def test_reconcile_equal_abs_tolerance_ok():
    calls: list[Any] = []

    def handler(sql):
        calls.append(sql)
        return _FakeResult([(10.0,)]) if len(calls) == 1 else _FakeResult([(11.0,)])

    execu = _FakeExecutor(handler)
    reconcile_equal(
        cast(BaseExecutor, execu),
        left={"table": "a", "expr": "v"},
        right={"table": "b", "expr": "v"},
        abs_tolerance=1.5,
    )


@pytest.mark.unit
def test_reconcile_equal_fails():
    calls: list[Any] = []

    def handler(sql):
        calls.append(sql)
        return _FakeResult([(10.0,)]) if len(calls) == 1 else _FakeResult([(20.0,)])

    execu = _FakeExecutor(handler)
    with pytest.raises(TestFailure):
        reconcile_equal(
            cast(BaseExecutor, execu),
            left={"table": "a", "expr": "v"},
            right={"table": "b", "expr": "v"},
        )


@pytest.mark.unit
def test_reconcile_ratio_within_ok():
    calls: list[Any] = []

    def handler(sql):
        calls.append(sql)
        return _FakeResult([(100.0,)]) if len(calls) == 1 else _FakeResult([(50.0,)])

    execu = _FakeExecutor(handler)
    reconcile_ratio_within(
        cast(BaseExecutor, execu),
        left={"table": "l", "expr": "x"},
        right={"table": "r", "expr": "y"},
        min_ratio=1.5,
        max_ratio=2.5,
    )


@pytest.mark.unit
def test_reconcile_ratio_within_fails():
    execu = _FakeExecutor(
        lambda sql: _FakeResult([(10.0,)]) if "from l" in sql else _FakeResult([(100.0,)])
    )

    with pytest.raises(TestFailure):
        reconcile_ratio_within(
            cast(BaseExecutor, execu),
            left={"table": "l", "expr": "x"},
            right={"table": "r", "expr": "y"},
            min_ratio=0.5,
            max_ratio=0.8,
        )


@pytest.mark.unit
def test_reconcile_diff_within_ok():
    calls: list[Any] = []

    def handler(sql):
        calls.append(sql)
        return _FakeResult([(50.0,)]) if len(calls) == 1 else _FakeResult([(53.0,)])

    execu = _FakeExecutor(handler)
    reconcile_diff_within(
        cast(BaseExecutor, execu),
        left={"table": "l", "expr": "x"},
        right={"table": "r", "expr": "y"},
        max_abs_diff=5.0,
    )


@pytest.mark.unit
def test_reconcile_diff_within_fails():
    execu = _FakeExecutor(
        lambda sql: _FakeResult([(10.0,)]) if "from l" in sql else _FakeResult([(25.0,)])
    )

    with pytest.raises(TestFailure):
        reconcile_diff_within(
            cast(BaseExecutor, execu),
            left={"table": "l", "expr": "x"},
            right={"table": "r", "expr": "y"},
            max_abs_diff=5.0,
        )


@pytest.mark.unit
def test_reconcile_coverage_ok():
    execu = _FakeExecutor(lambda sql: _FakeResult([(0,)]))
    reconcile_coverage(
        cast(BaseExecutor, execu),
        source={"table": "src", "key": "id"},
        target={"table": "tgt", "key": "id"},
    )


@pytest.mark.unit
def test_reconcile_coverage_fails():
    execu = _FakeExecutor(lambda sql: _FakeResult([(3,)]))

    with pytest.raises(TestFailure):
        reconcile_coverage(
            cast(BaseExecutor, execu),
            source={"table": "src", "key": "id"},
            target={"table": "tgt", "key": "id"},
        )


@pytest.mark.unit
def test_relationships_ok():
    execu = _FakeExecutor(lambda sql: _FakeResult([(0,)]))

    relationships(
        cast(BaseExecutor, execu),
        table="fact_events",
        field="user_id",
        to_table="dim_users",
        to_field="id",
    )


@pytest.mark.unit
def test_relationships_fails_on_orphans():
    execu = _FakeExecutor(lambda sql: _FakeResult([(5,)]))

    with pytest.raises(TestFailure):
        relationships(
            cast(BaseExecutor, execu),
            table="fact_events",
            field="user_id",
            to_table="dim_users",
            to_field="id",
        )


@pytest.mark.unit
def test_relationships_wraps_db_errors():
    execu = _FakeExecutor(lambda sql: (_ for _ in ()).throw(RuntimeError("no such column")))

    with pytest.raises(TestFailure) as exc:
        relationships(
            cast(BaseExecutor, execu),
            table="fact_events",
            field="user_id",
            to_table="dim_users",
            to_field="id",
        )
    assert "[relationships]" in str(exc.value)


# ---------------------------------------------------------------------------
# _fail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fail_builds_message():
    with pytest.raises(TestFailure) as exc:
        _fail("check_x", "tbl", "col", "select 1", "oops")
    msg = str(exc.value)
    assert "[check_x] tbl.col: oops" in msg
    assert "select 1" in msg
