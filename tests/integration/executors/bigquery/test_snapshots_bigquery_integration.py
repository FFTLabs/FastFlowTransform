# tests/unit/executors/bigquery/test_snapshots_bigquery_fake.py
from __future__ import annotations

import pytest
from tests.common.snapshot_helpers import (
    make_check_snapshot_node,
    make_timestamp_snapshot_node,
    patch_render_sql,
)

from fastflowtransform.core import relation_for
from fastflowtransform.snapshots.runtime.base import BaseSnapshotRuntime

VF_COL = BaseSnapshotRuntime.SNAPSHOT_VALID_FROM_COL
VT_COL = BaseSnapshotRuntime.SNAPSHOT_VALID_TO_COL
IS_CUR_COL = BaseSnapshotRuntime.SNAPSHOT_IS_CURRENT_COL
HASH_COL = BaseSnapshotRuntime.SNAPSHOT_HASH_COL
UPD_META_COL = BaseSnapshotRuntime.SNAPSHOT_UPDATED_AT_COL

# Simple SQL bodies - they're never actually executed by a real engine,
# we only inspect the resulting BigQuery SQL sent to the fake client.
SQL_TS_FIRST = """
select
  1 as id,
  'a' as value,
  timestamp '2024-01-01 00:00:00' as updated_at
"""

SQL_TS_SECOND = """
select
  1 as id,
  'b' as value,
  timestamp '2024-02-01 00:00:00' as updated_at
"""

SQL_CHECK_FIRST = """
select
  1 as id,
  'alpha' as value,
  timestamp '2024-01-01 00:00:00' as updated_at
"""

SQL_CHECK_SECOND = """
select
  1 as id,
  'beta' as value,
  timestamp '2024-01-01 00:00:00' as updated_at
"""


def _all_sql(executor) -> str:
    """Helper: concatenate all SQL strings recorded on the fake client."""
    client = executor.client  # BigQueryBaseExecutor attribute
    sqls = [entry[0] for entry in getattr(client, "queries", [])]
    return "\n".join(sqls)


@pytest.mark.bigquery
@pytest.mark.integration
def test_bigquery_snapshot_timestamp_emits_create_and_update(bq_executor_fake, jinja_env_bigquery):
    """
    Smoke test for timestamp snapshots on BigQuery:
    - first run: CREATE TABLE ... with snapshot columns
    - second run: emits follow-up DML (INSERT/DELETE/MERGE) also referencing
      snapshot metadata columns.
    """
    ex = bq_executor_fake
    node = make_timestamp_snapshot_node()

    # First run → CREATE TABLE ... AS SELECT ...
    patch_render_sql(ex, SQL_TS_FIRST)
    ex.run_snapshot_sql(node, jinja_env_bigquery)

    sql_first = _all_sql(ex).upper()
    assert "CREATE TABLE" in sql_first
    # snapshot metadata columns should appear in the create
    assert VF_COL.upper() in sql_first
    assert VT_COL.upper() in sql_first
    assert IS_CUR_COL.upper() in sql_first
    assert UPD_META_COL.upper() in sql_first

    # Reset recorded queries for a cleaner second-assertion
    ex.client.queries.clear()  # type: ignore[attr-defined]

    # Second run → incremental behaviour (no need to fully emulate the engine,
    # we just check that BQ DML is generated and references snapshot cols)
    patch_render_sql(ex, SQL_TS_SECOND)
    ex.run_snapshot_sql(node, jinja_env_bigquery)

    sql_second = _all_sql(ex).upper()
    # we expect some form of INSERT / DELETE / MERGE against the same table
    assert relation_for(node.name).upper() in sql_second
    assert VF_COL.upper() in sql_second
    assert VT_COL.upper() in sql_second
    assert IS_CUR_COL.upper() in sql_second
    assert UPD_META_COL.upper() in sql_second


@pytest.mark.bigquery
@pytest.mark.integration
def test_bigquery_snapshot_check_strategy_has_hash_column(bq_executor_fake, jinja_env_bigquery):
    """
    Check-strategy snapshots should generate a hash column in BigQuery SQL.
    """
    ex = bq_executor_fake
    node = make_check_snapshot_node()

    patch_render_sql(ex, SQL_CHECK_FIRST)
    ex.run_snapshot_sql(node, jinja_env_bigquery)

    sql = _all_sql(ex).upper()
    assert "CREATE TABLE" in sql
    assert HASH_COL.upper() in sql  # hash column present in schema / projection


@pytest.mark.bigquery
@pytest.mark.integration
def test_bigquery_snapshot_prune_emits_window_rank(bq_executor_fake, jinja_env_bigquery):
    """
    snapshot_prune should emit a query with ROW_NUMBER() / PARTITION BY
    over the business key(s). We don't emulate deletions; we only assert
    that the right style of SQL is generated and that it doesn't crash.
    """
    ex = bq_executor_fake
    node = make_timestamp_snapshot_node()

    # First + second run just to ensure the table "exists" logically
    patch_render_sql(ex, SQL_TS_FIRST)
    ex.run_snapshot_sql(node, jinja_env_bigquery)
    patch_render_sql(ex, SQL_TS_SECOND)
    ex.run_snapshot_sql(node, jinja_env_bigquery)

    # Clear previous queries so we only see prune SQL
    ex.client.queries.clear()  # type: ignore[attr-defined]

    rel = relation_for(node.name)
    ex.snapshot_prune(rel, unique_key=["id"], keep_last=1, dry_run=True)

    sql = _all_sql(ex).upper()
    # Basic shape of the ranking / prune CTE:
    assert "ROW_NUMBER" in sql
    assert "OVER" in sql
    assert "PARTITION BY" in sql
    assert rel.upper() in sql
