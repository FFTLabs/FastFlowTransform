# src/fastflowtransform/decorators.py
from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, ParamSpec, Protocol, TypeVar, cast

from pydantic import BaseModel

from fastflowtransform.core import REGISTRY, relation_for
from fastflowtransform.errors import ModuleLoadError
from fastflowtransform.testing.registry import DQParamsBase, Runner, register_python_test

P = ParamSpec("P")
R_co = TypeVar("R_co", covariant=True)


class HasFFMeta(Protocol[P, R_co]):
    __ff_name__: str
    __ff_deps__: list[str]
    __ff_require__: Any
    __ff_path__: Path
    __ff_tags__: list[str]
    __ff_kind__: str
    __ff_meta__: dict[str, Any]

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...


def _normalize_require(deps: list[str], require: Any | None) -> dict[str, set[str]]:
    """
    Accepts:
      - None
      - Iterable[str]  (for a single dependency)
      - Mapping[str, Iterable[str]]  (for multiple dependencies)
    Keys may be logical dependency names (e.g. 'orders.ff' or 'users_enriched');
    they are mapped to physical relations via relation_for(...).
    """
    if not require:
        return {}
    if len(deps) == 1 and not isinstance(require, Mapping):
        cols = set(cast(Iterable[str], require))
        return {relation_for(deps[0]): cols}
    if isinstance(require, Mapping):
        out: dict[str, set[str]] = {}
        for k, cols in require.items():
            out[relation_for(cast(str, k))] = set(cast(Iterable[str], cols))
        return out
    raise TypeError(
        "require must be a list/set for single dependency "
        "or a dict[dep_name, list[str]] for multiple dependencies"
    )


def model(
    name: str | None = None,
    deps: Sequence[str] | None = None,
    require: Any | None = None,
    requires: Any | None = None,
    *,
    tags: Sequence[str] | None = None,
    kind: str = "python",
    materialized: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> Callable[[Callable[P, R_co]], HasFFMeta[P, R_co]]:
    """
    Decorator to register a Python model.

    Args:
        name: Logical node name in the DAG (defaults to function name).
        deps: Upstream node names (e.g., ['users.ff']).
        require: Required columns per dependency; accepted shapes mirror `requires`.
        requires: Alias for `require` (only one of require/requires may be set).
            - Single dependency: Iterable[str] of required columns from that dependency.
            - Multiple dependencies: Mapping[dep_name, Iterable[str]]
              (dep_name = logical name or physical relation).
        tags: Optional tags for selection (e.g. ['demo','env']).
        kind: Logical kind; defaults to 'python' (useful for selectors kind:python).
        materialized: Shorthand for meta['materialized']; mirrors config(materialized='...').
        meta: Arbitrary metadata for executors/docs (merged with materialized if provided).
    """
    # Normalize the alias: allow only one of require/requires
    if require is not None and requires is not None:
        raise TypeError("Pass at most one of 'require' or 'requires', not both")

    effective_require = require if require is not None else requires

    def deco(func: Callable[P, R_co]) -> HasFFMeta[P, R_co]:
        f_any = cast(Any, func)

        fname = name or f_any.__name__
        fdeps = list(deps) if deps is not None else []

        # Attach metadata to the function (keeps backward compatibility)
        f_any.__ff_name__ = fname
        f_any.__ff_deps__ = fdeps

        # Normalize require and mirror it on the function and inside the registry
        req_norm = _normalize_require(fdeps, effective_require)
        f_any.__ff_require__ = req_norm  # useful for tooling/loaders
        REGISTRY.py_requires[fname] = req_norm  # executors read this directly

        f_any.__ff_tags__ = list(tags) if tags else []
        f_any.__ff_kind__ = kind or "python"

        metadata = dict(meta) if meta else {}
        if materialized is not None:
            metadata["materialized"] = materialized
        f_any.__ff_meta__ = metadata

        # Determine the source path (better error message if it fails)
        src: str | None = inspect.getsourcefile(func)
        if src is None:
            try:
                src = inspect.getfile(func)
            except Exception as e:
                raise ModuleLoadError(
                    f"Cannot determine source path for model '{fname}': {e}"
                ) from e

        f_any.__ff_path__ = Path(src).resolve()

        # Register the function
        REGISTRY.py_funcs[fname] = func
        return cast(HasFFMeta[P, R_co], func)

    return deco


def engine_model(
    *,
    only: str | Iterable[str] | None = None,
    env_match: Mapping[str, str] | None = None,
    **model_kwargs: Any,
) -> Callable[[Callable[P, R_co]], HasFFMeta[P, R_co] | Callable[P, R_co]]:
    """
    Env-aware decorator to register a Python model only when the current
    environment matches.

    Args:
        only:
            Backwards compatible engine filter based on FF_ENGINE
            (e.g. only="bigquery" or only=("duckdb", "postgres")).
        env_match:
            Arbitrary environment match, e.g.:
                env_match={"FF_ENGINE": "bigquery", "FF_ENGINE_VARIANT": "bigframes"}
    """

    # Normalize "only" → allowed engine names (lowercased)
    allowed_engines: set[str] | None = None
    if only is not None:
        if isinstance(only, str):
            allowed_engines = {only.lower()}
        else:
            allowed_engines = {str(e).lower() for e in only}

    def should_register() -> bool:
        # 1) Check env_match if provided
        if env_match:
            for key, expected in env_match.items():
                if os.getenv(key) != expected:
                    return False

        # 2) Check FF_ENGINE against "only" if provided
        if allowed_engines is not None:
            current = os.getenv("FF_ENGINE", "").lower()
            if current not in allowed_engines:
                return False

        return True

    def deco(fn: Callable[P, R_co]) -> HasFFMeta[P, R_co] | Callable[P, R_co]:
        if should_register():
            # Register in REGISTRY and attach __ff_* metadata
            return model(**model_kwargs)(fn)
        # No registration in this env → return the plain function
        return fn

    return deco


def dq_test(
    name: str | None = None,
    *,
    overwrite: bool = False,
    params_model: type[BaseModel] | None = None,
) -> Callable[[Callable[..., Any]], Runner]:
    """
    Decorator to register a custom data-quality test runner.

    Usage:

        from fastflowtransform import dq_test

        @dq_test("email_domain_allowed")
        def email_domain_allowed(executor, table, column, params):
            ...
            return True, None, "select ..."

    If `name` is omitted, the function name is used:

        @dq_test()
        def email_sanity(executor, table, column, params):
            ...

        # In project.yml / schema.yml: type: email_sanity

    Params model:

        class EmailTestParams(DQParamsBase):
            allowed_domains: list[str]

        @dq_test("email_domain_allowed", params_model=EmailTestParams)
        def email_domain_allowed(executor, table, column, params: EmailTestParams):
            ...

    Args:
        name: Optional explicit test name. If None, fn.__name__ is used.
        overwrite: If True, allow overriding an existing test name.
        params_model: Optional Pydantic model to validate `params`.
                      If omitted, DQParamsBase (extra='forbid') is used.
    """

    def decorator(fn: Callable[..., Any]) -> Runner:
        # Prefer attribute __name__ when available; fallback is just a placeholder.
        if name is not None:
            reg_name: str = name
        else:
            reg_name = cast(str, getattr(fn, "__name__", "<anonymous_test>"))

        pm = params_model or DQParamsBase

        # Central registration so test_cmd can pick up the params schema
        register_python_test(reg_name, fn, params_model=pm, overwrite=overwrite)

        # Attach a bit of metadata (not required, but can be handy for debugging/introspection)
        fn_any = cast(Any, fn)
        fn_any.__ff_test_name__ = reg_name
        fn_any.__ff_test_params_model__ = pm

        # Type-wise, fn already matches Runner's call signature at runtime
        return cast(Runner, fn)

    return decorator
