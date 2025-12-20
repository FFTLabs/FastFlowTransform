# tests/integration/executors/snowflake_snowpark/test_snapshots_snowflake_snowpark_integration.py
from __future__ import annotations

from typing import Any

import pytest
from jinja2 import Environment
from tests.common.snapshot_helpers import (
    make_check_snapshot_node,
    make_timestamp_snapshot_node,
    patch_render_sql,
)

from fastflowtransform.snapshots.runtime.base import BaseSnapshotRuntime

SQL_TS_FIRST = """
select 1 as id,
       'a'::string as value,
       to_timestamp_ntz('2024-01-01 00:00:00') as updated_at
"""

SQL_TS_SECOND = """
select 1 as id,
       'b'::string as value,
       to_timestamp_ntz('2024-02-01 00:00:00') as updated_at
"""

SQL_CHECK_FIRST = """
select 1 as id,
       'alpha'::string as value,
       to_timestamp_ntz('2024-01-01 00:00:00') as updated_at
"""

SQL_CHECK_SECOND = """
select 1 as id,
       'beta'::string as value,
       to_timestamp_ntz('2024-01-01 00:00:00') as updated_at
"""


def _all_sql(executor: Any) -> str:
    """
    Concatenate all SQL statements issued by the fake Snowflake session.
    """
    sess = getattr(executor, "session", None)
    calls = getattr(sess, "sql_calls", [])
    return "\n".join(calls)


@pytest.mark.snowflake_snowpark
@pytest.mark.integration
def test_snowflake_timestamp_snapshot_emits_create_table(
    snowflake_executor_fake, jinja_env: Environment
):
    ex = snowflake_executor_fake
    node = make_timestamp_snapshot_node()

    patch_render_sql(ex, SQL_TS_FIRST)
    ex.run_snapshot_sql(node, jinja_env)

    sql = _all_sql(ex).upper()
    assert "CREATE OR REPLACE TABLE" in sql
    assert BaseSnapshotRuntime.SNAPSHOT_VALID_FROM_COL.upper() in sql
    assert BaseSnapshotRuntime.SNAPSHOT_VALID_TO_COL.upper() in sql
    assert BaseSnapshotRuntime.SNAPSHOT_IS_CURRENT_COL.upper() in sql
    assert BaseSnapshotRuntime.SNAPSHOT_UPDATED_AT_COL.upper() in sql
    # timestamp strategy should not rely on a hash column
    # but we allow the implementation to include it if desired


@pytest.mark.snowflake_snowpark
@pytest.mark.integration
def test_snowflake_check_snapshot_emits_hash_column(
    snowflake_executor_fake, jinja_env: Environment
):
    ex = snowflake_executor_fake
    node = make_check_snapshot_node()

    patch_render_sql(ex, SQL_CHECK_FIRST)
    ex.run_snapshot_sql(node, jinja_env)

    sql = _all_sql(ex).upper()
    # first run should create the table with a hash column in the projection
    assert "CREATE OR REPLACE TABLE" in sql
    assert BaseSnapshotRuntime.SNAPSHOT_HASH_COL.upper() in sql


@pytest.mark.snowflake_snowpark
@pytest.mark.integration
def test_snowflake_snapshot_prune_emits_row_number_window(
    snowflake_executor_fake, jinja_env: Environment
):
    """
    snapshot_prune should emit a query with ROW_NUMBER() OVER / PARTITION BY /
    ORDER BY over the business key(s). We don't emulate deletions; we only
    assert that the right *style* of SQL is generated and that it doesn't crash.
    """
    ex = snowflake_executor_fake
    node = make_timestamp_snapshot_node()

    # First/second run to "logically" create the snapshot table; in the fake
    # session this just issues SQL, which is fine for this test.
    patch_render_sql(ex, SQL_TS_FIRST)
    ex.run_snapshot_sql(node, jinja_env)

    patch_render_sql(ex, SQL_TS_SECOND)
    ex.run_snapshot_sql(node, jinja_env)

    # Now call prune; we only care that it emits the right window pattern.
    ex.snapshot_prune("users_snapshot", unique_key=["id"], keep_last=1, dry_run=False)

    sql = _all_sql(ex).upper()
    assert "ROW_NUMBER()" in sql
    assert "OVER" in sql
    assert "PARTITION BY" in sql
    assert "ORDER BY" in sql
