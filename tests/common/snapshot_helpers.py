# tests/common/snapshot_helpers.py
from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pandas as pd

from fastflowtransform.core import relation_for
from fastflowtransform.snapshots.runtime.base import BaseSnapshotRuntime

SnapshotReadFn = Callable[[Any, str], pd.DataFrame]

VF_COL = BaseSnapshotRuntime.SNAPSHOT_VALID_FROM_COL
VT_COL = BaseSnapshotRuntime.SNAPSHOT_VALID_TO_COL
IS_CUR_COL = BaseSnapshotRuntime.SNAPSHOT_IS_CURRENT_COL
HASH_COL = BaseSnapshotRuntime.SNAPSHOT_HASH_COL
UPD_META_COL = BaseSnapshotRuntime.SNAPSHOT_UPDATED_AT_COL


# ── Node factories ──────────────────────────────────────────────────────────
def make_timestamp_snapshot_node(
    name: str = "users_snapshot.ff",
    *,
    unique_key: str | list[str] = "id",
    updated_at: str = "updated_at",
) -> SimpleNamespace:
    """
    Minimal Node-like object for timestamp snapshots.
    Works across all SQL executors (DuckDB, Postgres, Spark SQL, BigQuery, Snowflake).
    """
    meta: dict[str, Any] = {
        "materialized": "snapshot",
        "strategy": "timestamp",
        "unique_key": unique_key,
        "updated_at": updated_at,
    }
    return SimpleNamespace(
        name=name,
        kind="sql",
        path=f"models/{name}",
        deps=["users_clean.ff"],
        meta=meta,
    )


def make_check_snapshot_node(
    name: str = "users_snapshot_check.ff",
    *,
    unique_key: str | list[str] = "id",
    check_cols: list[str] | str = "value",
    updated_at: str | None = None,
) -> SimpleNamespace:
    """
    Minimal Node-like object for check-based snapshots.
    """
    meta: dict[str, Any] = {
        "materialized": "snapshot",
        "strategy": "check",
        "unique_key": unique_key,
        "check_cols": check_cols,
    }
    if updated_at:
        meta["updated_at"] = updated_at

    return SimpleNamespace(
        name=name,
        kind="sql",
        path=f"models/{name}",
        deps=["users_clean.ff"],
        meta=meta,
    )


# ── Small utilities ────────────────────────────────────────────────────────
def patch_render_sql(executor: Any, sql_text: str) -> None:
    """
    Monkeypatch executor.render_sql(...) to always return `sql_text`.
    Avoids depending on loader/REGISTRY in unit tests.
    """

    def _render(node: Any, env: Any, ref_resolver: Any, source_resolver: Any) -> str:
        return sql_text

    executor.render_sql = _render  # type: ignore[assignment]


def read_snapshot_relation(executor: Any, node_name: str, reader: SnapshotReadFn) -> pd.DataFrame:
    rel = relation_for(node_name)
    return reader(executor, rel)


# ── Generic scenarios / assertions ─────────────────────────────────────────
def scenario_timestamp_first_and_second_run(
    executor: Any,
    node: Any,
    jinja_env: Any,
    read_fn: SnapshotReadFn,
    *,
    sql_first: str,
    sql_second: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run a common timestamp snapshot scenario:
      1) first run with (id=1, value=a, updated_at=2024-01-01...)
      2) second run with (id=1, value=b, updated_at=2024-02-01...)

    Returns (df_after_first_run, df_after_second_run).
    Performs structural assertions on the snapshot columns.
    """
    # first run
    patch_render_sql(executor, sql_first)
    executor.run_snapshot_sql(node, jinja_env)
    df1 = read_snapshot_relation(executor, node.name, read_fn)

    # basic structural checks
    for col in ("id", "value", "updated_at", VF_COL, VT_COL, IS_CUR_COL, UPD_META_COL, HASH_COL):
        assert col in df1.columns, f"missing column {col!r} on first snapshot run"

    assert len(df1) == 1, "first snapshot run should generate exactly one row (one business key)"
    row = df1.iloc[0]
    assert row["id"] == 1
    assert row["value"] == "a"
    assert bool(row[IS_CUR_COL])
    assert pd.isna(row[VT_COL])
    # for timestamp strategy we expect valid_from == updated_at == updated_at_meta
    assert str(row[VF_COL]) == str(row["updated_at"])
    assert str(row[UPD_META_COL]) == str(row["updated_at"])

    # second run
    patch_render_sql(executor, sql_second)
    executor.run_snapshot_sql(node, jinja_env)
    df2 = read_snapshot_relation(executor, node.name, read_fn)

    # two versions now
    assert len(df2) == 2

    cur = df2[df2[IS_CUR_COL] == True]  # noqa: E712
    old = df2[df2[IS_CUR_COL] == False]  # noqa: E712

    assert len(cur) == 1
    assert len(old) == 1

    cur_row = cur.iloc[0]
    old_row = old.iloc[0]

    # current row: latest payload, open version
    assert cur_row["id"] == 1
    assert cur_row["value"] == "b"
    assert pd.isna(cur_row[VT_COL])
    assert str(cur_row[VF_COL]) == str(cur_row["updated_at"])
    assert str(cur_row[UPD_META_COL]) == str(cur_row["updated_at"])

    # closed row: old payload, valid_to set
    assert old_row["id"] == 1
    assert old_row["value"] == "a"
    assert not bool(old_row[IS_CUR_COL])
    assert not pd.isna(old_row[VT_COL])
    assert str(old_row[VF_COL]) == str(old_row["updated_at"])

    return df1, df2


def scenario_snapshot_prune_keep_last(
    executor: Any,
    node: Any,
    jinja_env: Any,
    read_fn: SnapshotReadFn,
    *,
    sql_first: str,
    sql_second: str,
    unique_key: list[str] | None = None,
) -> None:
    """
    Generic prune scenario:
      - run timestamp snapshot twice (2 versions)
      - dry-run prune keep_last=1 → no change
      - real prune keep_last=1 → only latest version kept
    """
    unique_key = unique_key or ["id"]

    _, _ = scenario_timestamp_first_and_second_run(
        executor, node, jinja_env, read_fn, sql_first=sql_first, sql_second=sql_second
    )
    df_before = read_snapshot_relation(executor, node.name, read_fn)
    assert len(df_before) == 2

    rel = relation_for(node.name)

    # dry-run
    executor.snapshot_prune(rel, unique_key=unique_key, keep_last=1, dry_run=True)
    df_dry = read_snapshot_relation(executor, node.name, read_fn)
    assert len(df_dry) == 2

    # real prune
    executor.snapshot_prune(rel, unique_key=unique_key, keep_last=1, dry_run=False)
    df_after = read_snapshot_relation(executor, node.name, read_fn)
    assert len(df_after) == 1

    row = df_after.iloc[0]
    assert row["id"] == 1
    assert row["value"] == "b"
    assert bool(row[IS_CUR_COL])
    assert pd.isna(row[VT_COL])


def scenario_check_strategy_detects_changes(
    executor: Any,
    node: Any,
    jinja_env: Any,
    read_fn: SnapshotReadFn,
    *,
    sql_first: str,
    sql_second: str,
) -> None:
    """
    Check strategy scenario:
      - first run: (id=1, value='alpha', updated_at=X)
      - second run: same id/updated_at, but value='beta'
      → still must open new version based on check_cols hash
    """
    # first run
    patch_render_sql(executor, sql_first)
    executor.run_snapshot_sql(node, jinja_env)
    df1 = read_snapshot_relation(executor, node.name, read_fn)

    assert len(df1) == 1
    r1 = df1.iloc[0]
    assert r1["value"] == "alpha"
    assert bool(r1[IS_CUR_COL])
    # hash column must be non-null for check strategy
    assert HASH_COL in df1.columns
    assert pd.notna(r1[HASH_COL])

    # second run with only check_cols changed
    patch_render_sql(executor, sql_second)
    executor.run_snapshot_sql(node, jinja_env)
    df2 = read_snapshot_relation(executor, node.name, read_fn)

    assert len(df2) == 2

    cur = df2[df2[IS_CUR_COL] == True]  # noqa: E712
    old = df2[df2[IS_CUR_COL] == False]  # noqa: E712

    assert len(cur) == 1
    assert len(old) == 1

    cur_row = cur.iloc[0]
    old_row = old.iloc[0]

    assert cur_row["value"] == "beta"
    assert pd.isna(cur_row[VT_COL])

    assert old_row["value"] == "alpha"
    assert not pd.isna(old_row[VT_COL])
    # valid_from must differ if a second version was created
    assert str(cur_row[VF_COL]) != str(old_row[VF_COL])
