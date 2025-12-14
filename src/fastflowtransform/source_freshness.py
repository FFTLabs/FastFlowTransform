# fastflowtransform/source_freshness.py
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from fastflowtransform.config.sources import resolve_source_entry
from fastflowtransform.core import REGISTRY
from fastflowtransform.logging import get_logger
from fastflowtransform.testing.base import TestFailure, freshness as _freshness_test

logger = get_logger("source_freshness")


@dataclass
class SourceFreshnessResult:
    source_name: str
    table_name: str
    relation: str  # fully-qualified table identifier used in SQL
    loaded_at_field: str
    delay_minutes: float | None
    warn_after_minutes: int | None
    error_after_minutes: int | None
    status: str  # "pass" | "warn" | "error"
    error: str | None = None


def _relation_for_source(
    entry: Mapping[str, Any],
    source_name: str,
    table_name: str,
    executor: Any,
    engine: str | None,
) -> str:
    """Leverage executor-specific source qualification when possible."""

    engine_for_cfg = engine or getattr(executor, "engine_name", None)
    cfg = resolve_source_entry(entry, engine_for_cfg, default_identifier=table_name)

    location = cfg.get("location")
    if location:
        return str(location)

    formatter = getattr(executor, "_format_source_reference", None)
    if callable(formatter):
        cfg_local = dict(cfg)
        cfg_local.setdefault("options", {})
        return cast(str, formatter(cfg_local, source_name, table_name))

    ident = cfg.get("identifier") or table_name
    schema = cfg.get("schema") or cfg.get("dataset")
    database = cfg.get("database") or cfg.get("catalog") or cfg.get("project")
    parts = [p for p in (database, schema, ident) if p]
    return ".".join(str(p) for p in parts) if parts else str(ident)


def run_source_freshness(
    executor: Any,
    *,
    engine: str | None = None,
) -> list[SourceFreshnessResult]:
    """
    Execute freshness checks for all sources that have a configured freshness block.

    Returns:
        List of per-table SourceFreshnessResult.
    """
    engine_label = engine or getattr(executor, "engine_name", None) or ""
    engine_norm = engine_label.lower()
    results: list[SourceFreshnessResult] = []

    sources = getattr(REGISTRY, "sources", {}) or {}
    for src_name, tables in sources.items():
        for tbl_name, entry in (tables or {}).items():
            freshness_cfg = (entry or {}).get("freshness") or {}
            loaded_at = freshness_cfg.get("loaded_at_field")
            warn_after = (freshness_cfg.get("warn_after") or {}).get("count_in_minutes")
            err_after = (freshness_cfg.get("error_after") or {}).get("count_in_minutes")

            if not loaded_at or (warn_after is None and err_after is None):
                # No usable freshness config → skip this table.
                continue

            relation = _relation_for_source(
                entry, src_name, tbl_name, executor, engine_norm or None
            )

            delay: float | None = None
            status = "pass"
            err_msg: str | None = None

            try:
                # Use the same logic as the built-in DQ 'freshness' test.
                # For classification we consider the strictest threshold:
                #   - if error_after is set: classify against that;
                #   - else: classify against warn_after only.
                threshold = err_after if err_after is not None else warn_after
                if threshold is None:
                    # should not happen given the guard above
                    continue
                _freshness_test(executor, relation, loaded_at, max_delay_minutes=int(threshold))
                # If we reach here, delay <= threshold; we can recompute the actual delay
                # by re-running with a large threshold and inferring from error message
                # OR we can simply omit it. Keep it simple and omit for now.
            except TestFailure as e:
                # Parse out the delay from the error message if available.
                # Message format from testing.freshness:
                #   "freshness of table.col too old: {delay} min > {max_delay} min"
                txt = str(e)
                delay = None
                m = None

                m = re.search(r"too old: ([0-9.eE+-]+) min >", txt)
                if m:
                    try:
                        delay = float(m.group(1))
                    except Exception:
                        delay = None

                err_msg = txt

                # Classify WARN vs ERROR
                if err_after is not None and delay is not None and delay > err_after:
                    status = "error"
                elif warn_after is not None and delay is not None and delay > warn_after:
                    status = "warn"
                else:
                    # if we can't parse delay, but test failed against err threshold,
                    # treat as error.
                    status = "error"

            res = SourceFreshnessResult(
                source_name=src_name,
                table_name=tbl_name,
                relation=relation,
                loaded_at_field=loaded_at,
                delay_minutes=delay,
                warn_after_minutes=warn_after,
                error_after_minutes=err_after,
                status=status,
                error=err_msg,
            )
            results.append(res)

    return results
