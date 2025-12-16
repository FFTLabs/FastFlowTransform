from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, cast

from fastflowtransform.logging import echo


def parse_max_bytes_env(env_var: str) -> int | None:
    """
    Parse an env var like FF_BQ_MAX_BYTES into an integer byte count.
    """
    raw = os.getenv(env_var)
    if not raw:
        return None

    text = raw.strip().lower().replace("_", "").replace(",", "")
    if not text:
        return None

    multiplier = 1
    suffixes = [
        ("tb", 1024**4),
        ("t", 1024**4),
        ("gb", 1024**3),
        ("g", 1024**3),
        ("mb", 1024**2),
        ("m", 1024**2),
        ("kb", 1024),
        ("k", 1024),
    ]
    for suf, factor in suffixes:
        if text.endswith(suf):
            text = text[: -len(suf)].strip()
            multiplier = factor
            break

    try:
        value = float(text)
    except ValueError:
        echo(
            f"Warning: invalid {env_var}={raw!r}; expected integer bytes or "
            "a number with unit suffix like '10GB'. Ignoring limit."
        )
        return None

    bytes_val = int(value * multiplier)
    if bytes_val <= 0:
        return None
    return bytes_val


def format_bytes(num: int) -> str:
    """Human-readable byte formatting for error messages."""
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(num)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if size >= 10:
                return f"{size:.1f} {unit}"
            return f"{size:.2g} {unit}"
        size /= 1024.0
    return f"{num} B"


EstimatorFn = Callable[[str], int | None]


class BudgetGuard:
    """
    Shared implementation for per-query budget enforcement.
    """

    def __init__(
        self,
        *,
        env_var: str | None,
        estimator_attr: str,
        engine_label: str,
        what: str = "query",
    ):
        self.env_var = env_var
        self.estimator_attr = estimator_attr
        self.engine_label = engine_label
        self.what = what
        self._env_limit: int | None = None
        self._env_limit_populated = False

    def _env_limit_value(self) -> int | None:
        if not self.env_var:
            return None
        if not self._env_limit_populated:
            self._env_limit = parse_max_bytes_env(self.env_var)
            self._env_limit_populated = True
        return self._env_limit

    def resolve_limit(self, override_limit: int | None = None) -> tuple[int | None, str | None]:
        env_limit = self._env_limit_value()
        if env_limit is not None and env_limit > 0:
            source = f"{self.env_var}" if self.env_var else "environment"
            return env_limit, source
        if override_limit is None:
            return None, None
        try:
            limit = int(override_limit)
        except Exception:
            return None, None
        if limit <= 0:
            return None, None
        return limit, "budgets.yml (query_limits)"

    def enforce(self, sql: str, executor: Any, *, limit: int, source: str | None) -> int | None:
        estimator_obj = getattr(executor, self.estimator_attr, None)

        if not callable(estimator_obj):
            echo(
                f"{self.engine_label} cost guard misconfigured: "
                f"missing estimator '{self.estimator_attr}'. Guard ignored."
            )
            return None

        estimator = cast(EstimatorFn, estimator_obj)

        try:
            estimated = estimator(sql)
        except Exception as exc:
            echo(
                f"{self.engine_label} cost estimation failed "
                f"(limit ignored for this {self.what}): {exc}"
            )
            return None

        if estimated is None:
            return None

        value = estimated

        if value <= 0:
            return None

        if value > limit:
            label = source or "configured limit"
            msg = (
                f"Aborting {self.engine_label} {self.what}: estimated scanned bytes "
                f"{value} ({format_bytes(value)}) exceed "
                f"{label}={limit} ({format_bytes(limit)}).\n"
                f"Adjust {label} to allow this {self.what}."
            )
            raise RuntimeError(msg)

        return value
