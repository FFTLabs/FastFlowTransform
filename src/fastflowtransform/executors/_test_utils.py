# fastflowtransform/executors/_test_utils.py
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


class _ListFetchWrapper:
    """
    Minimal fetch wrapper for iterable results to mimic DB-API/SQLA cursors.
    """

    def __init__(self, rows: Iterable[Any] | None):
        self._rows = list(rows or [])
        self._idx = 0

    def fetchone(self) -> Any:
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self) -> list[Any]:
        if self._idx == 0:
            self._idx = len(self._rows)
            return list(self._rows)
        rows = self._rows[self._idx :]
        self._idx = len(self._rows)
        return rows


def make_fetchable(result: Any) -> Any:
    """
    Ensure a result supports .fetchone()/.fetchall().

    - If already fetchable, return as-is.
    - If it has .result() (e.g., BigQuery QueryJob), use that first.
    - If it's an iterable (not string/bytes), wrap in a simple fetch wrapper.
    - Otherwise return unchanged (may still fail if caller expects fetch methods).
    """
    if hasattr(result, "fetchone") and hasattr(result, "fetchall"):
        return result
    if hasattr(result, "result") and callable(result.result):
        try:
            return make_fetchable(result.result())
        except Exception:
            raise
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes, bytearray)):
        return _ListFetchWrapper(result)
    return result


def rows_to_tuples(rows: Iterable[Any] | None) -> list[tuple[Any, ...]]:
    """
    Normalize various row shapes to simple tuples.

    Supported shapes:
      - tuple -> returned as-is
      - mapping-like via .asDict() -> tuple(values)
      - general Sequence (excluding str/bytes) -> tuple(row)
      - fallback: (row,)
    """

    def _one(row: Any) -> tuple[Any, ...]:
        if isinstance(row, tuple):
            return row
        if hasattr(row, "asDict"):
            try:
                d = row.asDict()
                if isinstance(d, dict):
                    return tuple(d.values())
            except Exception:
                pass
        if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
            try:
                return tuple(row)
            except Exception:
                pass
        return (row,)

    return [_one(r) for r in (rows or [])]
