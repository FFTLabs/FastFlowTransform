from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastflowtransform.artifacts.postgres_store import PostgresArtifactsStore

from .files import (
    _json_dump,
    _target_dir,
    build_catalog,
    build_manifest,
    build_run_results,
    build_test_results,
    build_utest_results,
)


def _normalize_mode(mode: str | None) -> str:
    raw = (mode or "files").strip().lower()
    return raw if raw in {"files", "db", "both"} else "files"


def _ensure_run_id(store: PostgresArtifactsStore | None) -> str:
    if store is not None:
        return store.new_run_id()
    return uuid.uuid4().hex


def _inject_run_meta(
    payload: dict[str, Any],
    *,
    run_id: str,
    env_name: str | None,
    model_engine: str | None,
) -> dict[str, Any]:
    meta = dict(payload.get("metadata") or {})
    meta["run_id"] = run_id
    if env_name:
        meta["env_name"] = env_name
    if model_engine:
        meta["model_engine"] = model_engine
    out = dict(payload)
    out["metadata"] = meta
    return out


def _write_payload_file(project_dir: Path, filename: str, payload: dict[str, Any]) -> None:
    out_dir = _target_dir(project_dir)
    path = out_dir / filename
    _json_dump(path, payload)


def emit_run_artifacts(
    project_dir: Path,
    *,
    artifacts_mode: str | None,
    artifacts_store: PostgresArtifactsStore | None,
    env_name: str | None,
    model_engine: str | None,
    started_at: str,
    finished_at: str,
    node_results: list[Any],
    budgets: dict[str, Any] | None = None,
    executor: Any | None = None,
    include_catalog: bool = True,
) -> str:
    """
    Emit manifest/run_results (and optionally catalog) to files, DB, or both.
    Returns the run_id used for all emitted artifacts.
    """
    mode = _normalize_mode(artifacts_mode)
    run_id = _ensure_run_id(artifacts_store)

    manifest = _inject_run_meta(
        build_manifest(project_dir),
        run_id=run_id,
        env_name=env_name,
        model_engine=model_engine,
    )
    run_results = _inject_run_meta(
        build_run_results(
            project_dir,
            started_at=started_at,
            finished_at=finished_at,
            node_results=node_results,
            budgets=budgets,
        ),
        run_id=run_id,
        env_name=env_name,
        model_engine=model_engine,
    )

    catalog: dict[str, Any] | None = None
    if include_catalog and executor is not None:
        catalog = _inject_run_meta(
            build_catalog(project_dir, executor),
            run_id=run_id,
            env_name=env_name,
            model_engine=model_engine,
        )

    if mode in {"files", "both"}:
        _write_payload_file(project_dir, "manifest.json", manifest)
        _write_payload_file(project_dir, "run_results.json", run_results)
        if catalog is not None:
            _write_payload_file(project_dir, "catalog.json", catalog)

    if mode in {"db", "both"}:
        if artifacts_store is None:
            raise RuntimeError("Artifacts DB mode requires a configured PostgresArtifactsStore.")
        artifacts_store.upsert_run(
            run_id=run_id,
            env_name=env_name,
            model_engine=model_engine,
            meta={"started_at": started_at, "finished_at": finished_at},
        )
        artifacts_store.write_artifact(
            run_id=run_id,
            artifact_type="manifest",
            payload=manifest,
        )
        artifacts_store.write_artifact(
            run_id=run_id,
            artifact_type="run_results",
            payload=run_results,
        )
        if catalog is not None:
            artifacts_store.write_artifact(
                run_id=run_id,
                artifact_type="catalog",
                payload=catalog,
            )

    return run_id


def emit_test_results(
    project_dir: Path,
    *,
    artifacts_mode: str | None,
    artifacts_store: PostgresArtifactsStore | None,
    env_name: str | None,
    model_engine: str | None,
    started_at: str,
    finished_at: str,
    results: list[Any],
) -> str:
    """
    Emit test_results to files, DB, or both.
    Returns the run_id used for the emitted artifact.
    """
    mode = _normalize_mode(artifacts_mode)
    run_id = _ensure_run_id(artifacts_store)

    payload = _inject_run_meta(
        build_test_results(
            project_dir,
            started_at=started_at,
            finished_at=finished_at,
            results=results,
        ),
        run_id=run_id,
        env_name=env_name,
        model_engine=model_engine,
    )

    if mode in {"files", "both"}:
        _write_payload_file(project_dir, "test_results.json", payload)

    if mode in {"db", "both"}:
        if artifacts_store is None:
            raise RuntimeError("Artifacts DB mode requires a configured PostgresArtifactsStore.")
        artifacts_store.upsert_run(
            run_id=run_id,
            env_name=env_name,
            model_engine=model_engine,
            meta={"started_at": started_at, "finished_at": finished_at, "kind": "test"},
        )
        artifacts_store.write_artifact(
            run_id=run_id,
            artifact_type="test_results",
            payload=payload,
        )

    return run_id


def emit_utest_results(
    project_dir: Path,
    *,
    artifacts_mode: str | None,
    artifacts_store: PostgresArtifactsStore | None,
    env_name: str | None,
    model_engine: str | None,
    started_at: str,
    finished_at: str,
    failures: int,
    results: list[Any],
    engine: str | None = None,
) -> str:
    """
    Emit utest_results to files, DB, or both.
    Returns the run_id used for the emitted artifact.
    """
    mode = _normalize_mode(artifacts_mode)
    run_id = _ensure_run_id(artifacts_store)

    payload = _inject_run_meta(
        build_utest_results(
            project_dir,
            started_at=started_at,
            finished_at=finished_at,
            failures=failures,
            results=results,
            engine=engine,
        ),
        run_id=run_id,
        env_name=env_name,
        model_engine=model_engine,
    )

    if mode in {"files", "both"}:
        _write_payload_file(project_dir, "utest_results.json", payload)

    if mode in {"db", "both"}:
        if artifacts_store is None:
            raise RuntimeError("Artifacts DB mode requires a configured PostgresArtifactsStore.")
        artifacts_store.upsert_run(
            run_id=run_id,
            env_name=env_name,
            model_engine=model_engine,
            meta={"started_at": started_at, "finished_at": finished_at, "kind": "utest"},
        )
        artifacts_store.write_artifact(
            run_id=run_id,
            artifact_type="utest_results",
            payload=payload,
        )

    return run_id
