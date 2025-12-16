# fastflowtransform/executors/query_stats.py
from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


@dataclass
class QueryStats:
    """
    Normalised query metrics.

    Engines can fill whichever fields they support; unsupported ones
    can simply stay None.
    """

    bytes_processed: int | None = None
    rows: int | None = None
    duration_ms: int | None = None


class _TrackedQueryJob:
    """
    Thin proxy around an engine-specific query job (BigQuery, Snowflake, Spark...).

    - Forwards all unknown attributes to the wrapped job.
    - Intercepts `.result(...)` and calls `on_complete(inner_job)` exactly once.
    - Never raises from the callback; stats collection is strictly best-effort.
    """

    def __init__(self, inner_job: Any, *, on_complete: Callable[[Any], None]) -> None:
        self._inner_job = inner_job
        self._on_complete = on_complete
        self._done = False

    def result(self, *args: Any, **kwargs: Any) -> Any:
        # Delegate to the underlying job
        res = self._inner_job.result(*args, **kwargs)

        # Fire the completion callback once
        if not self._done:
            self._done = True
            with suppress(Exception):
                self._on_complete(self._inner_job)

        return res

    def __getattr__(self, name: str) -> Any:
        # Forward all other attributes to the underlying job
        return getattr(self._inner_job, name)
