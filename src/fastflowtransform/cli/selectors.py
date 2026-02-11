from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

from fastflowtransform.artifacts.config import resolve_artifacts_db
from fastflowtransform.artifacts.postgres_store import PostgresArtifactsStore
from fastflowtransform.core import REGISTRY, relation_for


def _parse_select(parts: list[str]) -> list[str]:
    """Accept multiple --select occurrences or a single space-separated string."""
    out: list[str] = []
    for p in parts:
        out.extend(s for s in str(p).split() if s)
    return out


def _selector(predicates: Iterable[Callable[[Any], bool]]) -> Callable[[Any], bool]:
    preds = list(predicates)
    if not preds:
        return lambda n: True
    return lambda n: all(p(n) for p in preds)


def _build_predicates(tokens: list[str]) -> list[Callable[[Any], bool]]:
    """
    Supported tokens:
      - name glob: e.g. orders*, marts_*  (matches Node.name and physical relation name)
      - tag:<tag> : matches Node.meta['tags'] (list or str)
      - type:<view|table|ephemeral> : matches Node.meta['materialized'] (default 'table')
      - kind:<sql|python> : matches Node.kind
      - result:<ok|error|fail|warn> : matches last run_results.json
    AND across tokens.
    """
    preds: list[Callable[[Any], bool]] = []
    # --- Lazy loader for run_results.json (cached per call) ---
    results_cache: dict[str, set[str]] | None = None

    def _load_result_sets() -> dict[str, set[str]]:
        """
        Load last run_results.json and build name-sets:
          ok:    status == 'success'
          error: status == 'error'
          fail:  synonym for error
          warn:  warnings>0 OR 'warn' in message (best-effort)
        If file is missing/corrupt, all sets are empty.
        """
        nonlocal results_cache
        if results_cache is not None:
            return results_cache
        ok: set[str] = set()
        err: set[str] = set()
        wrn: set[str] = set()
        try:
            proj = REGISTRY.get_project_dir()
            data = _load_run_results_from_files_or_db(proj)
            for r in data.get("results") or []:
                name = r.get("name")
                status = (r.get("status") or "").lower()
                message = (r.get("message") or "") or ""
                warnings = r.get("warnings", 0)
                if not name or not isinstance(name, str):
                    continue
                if status == "success":
                    ok.add(name)
                elif status == "error":
                    err.add(name)
                # best-effort warning detection
                if (isinstance(warnings, int) and warnings > 0) or (
                    isinstance(message, str) and "warn" in message.lower()
                ):
                    wrn.add(name)
        except Exception:
            # No file or malformed → empty sets
            pass
        # Define "ok" as success **without** warnings:
        ok_no_warn = ok - wrn
        results_cache = {
            "ok": ok_no_warn,
            "error": err,
            "fail": set(err),
            "warn": wrn,
        }
        return results_cache

    for tok in tokens:
        if tok.startswith("result:"):
            want = tok.split(":", 1)[1].lower()
            # normalize aliases
            if want not in {"ok", "error", "fail", "warn"}:
                # Unknown result token → match nothing (don't raise)
                preds.append(lambda _n: False)
                continue

            def _p_result(n, w=want):
                sets = _load_result_sets()
                # default to empty set if missing
                names = sets.get(w) or set()
                return n.name in names

            preds.append(_p_result)
            continue

        if tok.startswith("tag:"):
            want = tok.split(":", 1)[1]

            def _p_tag(n, w=want):
                tags = (getattr(n, "meta", {}) or {}).get("tags")
                if isinstance(tags, list):
                    return w in tags
                return tags == w

            preds.append(_p_tag)
        elif tok.startswith("type:"):
            want = tok.split(":", 1)[1]
            preds.append(
                cast(
                    Callable[[Any], bool],
                    lambda n, w=want: (getattr(n, "meta", {}) or {}).get("materialized", "table")
                    == w,
                )
            )
        elif tok.startswith("kind:"):
            want = tok.split(":", 1)[1]
            preds.append(cast(Callable[[Any], bool], lambda n, w=want: n.kind == w))
        else:
            pattern = tok
            preds.append(
                cast(
                    Callable[[Any], bool],
                    lambda n, pat=pattern: fnmatch.fnmatch(n.name, pat)
                    or fnmatch.fnmatch(relation_for(n.name), pat),
                )
            )
    return preds


def _load_run_results_from_files_or_db(project_dir: Any) -> dict[str, Any]:
    project_dir = Path(project_dir)
    # 1) Try local file first
    path = project_dir / ".fastflowtransform" / "target" / "run_results.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    # 2) Fallback to DB if configured
    env_name = os.getenv("FF_ENV") or "dev"
    engine = REGISTRY._current_engine()
    mode = os.getenv("FF_ARTIFACTS_MODE", "").strip().lower()

    dsn = os.getenv("FF_ARTIFACTS_PG_DSN")
    schema = os.getenv("FF_ARTIFACTS_PG_SCHEMA")

    if not dsn or not schema:
        resolved = resolve_artifacts_db(project_dir, env_name)
        if resolved:
            mode = mode or resolved.mode
            dsn = dsn or resolved.dsn
            schema = schema or resolved.db_schema

    if mode != "db" or not dsn or not schema:
        return {}

    store = PostgresArtifactsStore(dsn=dsn, schema=schema)
    payload = store.get_latest_artifact("run_results", env_name, engine)
    return payload if isinstance(payload, dict) else {}


def _downstream_closure(names: set[str]) -> set[str]:
    """Return names plus all downstream nodes (transitively)."""
    # Build reverse edges: dep -> [dependents]
    rev: dict[str, list[str]] = {}
    for n, node in REGISTRY.nodes.items():
        for d in node.deps or []:
            rev.setdefault(d, []).append(n)
    out = set(names)
    frontier = list(names)
    while frontier:
        cur = frontier.pop()
        for nxt in rev.get(cur, []):
            if nxt not in out:
                out.add(nxt)
                frontier.append(nxt)
    return out


def _compile_selector(
    select_opt: list[str] | None,
) -> tuple[list[str], Callable[[Any], bool]]:
    """Normalize `--select` values and return (tokens, base_predicate).
    NOTE: state:modified logic is handled at command layer via augment_with_state_modified().
    """
    tokens = _parse_select(select_opt or [])
    preds = _build_predicates(tokens)
    return tokens, _selector(preds)


def augment_with_state_modified(
    tokens: list[str],
    base_pred: Callable[[Any], bool],
    modified: set[str],
) -> Callable[[Any], bool]:
    """
    Combine a base predicate with state:modified tokens.
    - 'state:modified'    → filter to modified only
    - 'state:modified+'   → filter to modified union downstream(modified)
    - Combined with other tokens → logical AND with base predicate
    """
    if not any(t.startswith("state:modified") for t in tokens):
        return base_pred

    plus = any(t.startswith("state:modified+") for t in tokens)
    state_set = set(modified)
    if plus:
        state_set = _downstream_closure(state_set)

    other_tokens = [t for t in tokens if not t.startswith("state:modified")]
    if not other_tokens:
        return lambda n: n.name in state_set
    return lambda n: (n.name in state_set) and base_pred(n)


def _selected_subgraph_names(
    nodes: dict[str, Any],
    select_tokens: list[str] | None,
    exclude_tokens: list[str] | None,
    *,
    seed_names: set[str] | None = None,
) -> set[str]:
    """
    Compute the reduced set of node names to execute:
      1) Seeds = nodes matching --select (or all if --select omitted)
      2) Remove excluded nodes and their downstream closure
      3) Final = upstream-closure of remaining seeds within the remaining graph
    This guarantees that all dependencies of every kept seed are present;
    if a required dep was excluded, the affected seed is dropped.
    """
    if not nodes:
        return set()

    _, sel_pred = _compile_selector(select_tokens or [])
    _, ex_pred = _compile_selector(exclude_tokens or [])

    all_names = set(nodes.keys())
    if seed_names is not None:
        seeds = {n for n in seed_names if n in all_names}
    else:
        seeds = {n for n in all_names if (sel_pred(nodes[n]) if select_tokens else True)}
    if not seeds:
        return set()

    deps_map: dict[str, set[str]] = {n: set(nodes[n].deps or []) for n in all_names}
    rev_map: dict[str, set[str]] = {n: set() for n in all_names}
    for u, ds in deps_map.items():
        for d in ds:
            if d in rev_map:
                rev_map[d].add(u)

    excluded = {n for n in all_names if (ex_pred(nodes[n]) if exclude_tokens else False)}
    if excluded:
        stack = list(excluded)
        downstream = set()
        while stack:
            cur = stack.pop()
            for v in rev_map.get(cur, ()):
                if v not in downstream and v not in excluded:
                    downstream.add(v)
                    stack.append(v)
        removed = excluded | downstream
    else:
        removed = set()

    remaining = all_names - removed
    seeds = seeds - removed
    if not seeds:
        return set()

    result: set[str] = set()
    stack = list(seeds)
    while stack:
        cur = stack.pop()
        if cur in result or cur not in remaining:
            continue
        result.add(cur)
        for d in deps_map.get(cur, ()):
            if d in remaining:
                stack.append(d)
    return result
