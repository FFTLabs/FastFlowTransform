from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from fastflowtransform.core import Node
from fastflowtransform.incremental import _normalize_unique_key

SnapshotStrategy = Literal["timestamp", "check"]


@dataclass
class SnapshotConfigResolved:
    """
    Normalised snapshot configuration usable by executors.

    Supports both:
      - legacy nested config: snapshot={strategy=..., updated_at=..., check_cols=...}
      - flattened config:     strategy=..., updated_at=..., check_cols=...
    """

    strategy: SnapshotStrategy
    unique_key: list[str]
    updated_at: str | None
    check_cols: list[str]


def resolve_snapshot_config(node: Node, meta: Mapping[str, Any]) -> SnapshotConfigResolved:
    """
    Resolve and validate snapshot configuration from a model's meta dict.

    Accepted shapes:
      {{ config(
          materialized='snapshot',
          snapshot={
            'strategy': 'timestamp',
            'updated_at': 'updated_at',
            'check_cols': ['col1', 'col2'],
          },
          unique_key='id',
        ) }}

      OR (flattened)

      {{ config(
          materialized='snapshot',
          strategy='timestamp',
          updated_at='updated_at',
          check_cols=['col1', 'col2'],
          unique_key='id',
        ) }}
    """
    meta = dict(meta or {})

    # Optional nested block
    snapshot_block = meta.get("snapshot")
    if snapshot_block is not None and not isinstance(snapshot_block, Mapping):
        raise TypeError(
            f"{node.path}: snapshot configuration must be a mapping (snapshot={{...}})."
        )
    snapshot_block = dict(snapshot_block or {})

    # ---- unique key ----------------------------------------------------
    unique_key = _normalize_unique_key(meta.get("unique_key") or meta.get("primary_key"))
    if not unique_key:
        raise ValueError(
            f"{node.path}: snapshot models require 'unique_key' (string or list of strings)."
        )

    # ---- strategy ------------------------------------------------------
    raw_strategy = snapshot_block.get("strategy") or meta.get("strategy") or "timestamp"
    strategy_str = str(raw_strategy).lower()
    if strategy_str not in ("timestamp", "check"):
        raise ValueError(
            f"{node.path}: snapshot 'strategy' must be 'timestamp' or 'check', "
            f"got {raw_strategy!r}."
        )

    # Narrow to the Literal["timestamp", "check"] type for type-checkers
    strategy: SnapshotStrategy = "timestamp" if strategy_str == "timestamp" else "check"

    # ---- updated_at ----------------------------------------------------
    updated_at = (
        snapshot_block.get("updated_at")
        or snapshot_block.get("updated_at_column")
        or meta.get("updated_at")
        or meta.get("updated_at_column")
    )

    # ---- check_cols ----------------------------------------------------
    raw_check_cols = (
        snapshot_block.get("check_cols")
        or snapshot_block.get("check_columns")
        or meta.get("check_cols")
        or meta.get("check_columns")
    )
    check_cols = _normalize_unique_key(raw_check_cols) if raw_check_cols else []

    # Per-strategy guards (extra safety besides ModelConfig)
    if strategy == "timestamp" and not updated_at:
        raise ValueError(
            f"{node.path}: strategy='timestamp' snapshots require 'updated_at' column name."
        )
    if strategy == "check" and not check_cols:
        raise ValueError(
            f"{node.path}: strategy='check' snapshots require non-empty "
            "'check_cols' (string or list)."
        )

    return SnapshotConfigResolved(
        strategy=strategy, unique_key=unique_key, updated_at=updated_at, check_cols=check_cols
    )
