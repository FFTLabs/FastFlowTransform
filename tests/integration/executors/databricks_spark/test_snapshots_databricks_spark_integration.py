# tests/integration/executors/test_snapshots_databricks_spark_integration.py
from __future__ import annotations

from contextlib import suppress

import pytest
from tests.common.snapshot_helpers import (
    make_check_snapshot_node,
    make_timestamp_snapshot_node,
    scenario_check_strategy_detects_changes,
    scenario_snapshot_prune_keep_last,
    scenario_timestamp_first_and_second_run,
)

from fastflowtransform.core import relation_for
from fastflowtransform.executors.databricks_spark import DatabricksSparkExecutor

# Spark-friendly SQL (very similar; Spark understands timestamp '...')
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


def _reset_snapshot_table(executor, node_name: str) -> None:
    """
    Spark keeps tables across tests (shared SparkSession + warehouse).
    Ensure each snapshot scenario starts from a clean table so that
    the first snapshot run is truly a 'first run'.
    """
    rel = relation_for(node_name)
    physical = executor._physical_identifier(rel)
    with suppress(Exception):
        executor.spark.sql(f"DROP TABLE IF EXISTS {physical}")


def _read_spark(ex: DatabricksSparkExecutor, relation: str):
    physical = ex._physical_identifier(relation)
    return (
        ex.spark.table(physical)
        .toPandas()
        .sort_values(["id", ex.snapshot_runtime.SNAPSHOT_VALID_FROM_COL])
    )


@pytest.mark.databricks_spark
@pytest.mark.integration
def test_spark_snapshot_timestamp_first_and_second_run(jinja_env, spark_exec):
    node = make_timestamp_snapshot_node()
    _reset_snapshot_table(spark_exec, node.name)

    scenario_timestamp_first_and_second_run(
        executor=spark_exec,
        node=node,
        jinja_env=jinja_env,
        read_fn=_read_spark,
        sql_first=SQL_TS_FIRST,
        sql_second=SQL_TS_SECOND,
    )


@pytest.mark.databricks_spark
@pytest.mark.integration
def test_spark_snapshot_prune_keep_last(jinja_env, spark_exec):
    node = make_timestamp_snapshot_node()
    _reset_snapshot_table(spark_exec, node.name)

    scenario_snapshot_prune_keep_last(
        executor=spark_exec,
        node=node,
        jinja_env=jinja_env,
        read_fn=_read_spark,
        sql_first=SQL_TS_FIRST,
        sql_second=SQL_TS_SECOND,
        unique_key=["id"],
    )


@pytest.mark.databricks_spark
@pytest.mark.integration
def test_spark_snapshot_check_strategy(jinja_env, spark_exec):
    node = make_check_snapshot_node()
    _reset_snapshot_table(spark_exec, node.name)

    scenario_check_strategy_detects_changes(
        executor=spark_exec,
        node=node,
        jinja_env=jinja_env,
        read_fn=_read_spark,
        sql_first=SQL_CHECK_FIRST,
        sql_second=SQL_CHECK_SECOND,
    )
