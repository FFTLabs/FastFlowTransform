import json
from pathlib import Path

import pytest

from fastflowtransform.artifacts import (
    RunNodeResult,
    write_catalog,
    write_manifest,
    write_run_results,
)
from fastflowtransform.core import REGISTRY
from fastflowtransform.executors.duckdb import DuckExecutor


@pytest.mark.integration
@pytest.mark.duckdb
def test_artifacts_all_written(tmp_path: Path):
    # Project
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "m.ff.sql").write_text(
        "create or replace table m as select 1 as id", encoding="utf-8"
    )
    (tmp_path / "sources.yml").write_text(
        "version: 1\nsources: []\n",
        encoding="utf-8",
    )

    REGISTRY.load_project(tmp_path)
    env = REGISTRY.get_env()
    ex = DuckExecutor(":memory:")
    ex.run_sql(REGISTRY.get_node("m.ff"), env)

    # Manifest
    mp = write_manifest(tmp_path)
    assert mp.exists()

    # Catalog
    cp = write_catalog(tmp_path, ex)
    assert cp.exists()

    # Run results
    rp = write_run_results(
        tmp_path,
        started_at="2025-01-01T00:00:00+00:00",
        finished_at="2025-01-01T00:00:10+00:00",
        node_results=[
            RunNodeResult(
                name="m.ff",
                status="success",
                started_at="2025-01-01T00:00:00+00:00",
                finished_at="2025-01-01T00:00:01+00:00",
                duration_ms=1000,
            )
        ],
    )
    assert rp.exists()
    # sanity check
    data = json.loads(rp.read_text(encoding="utf-8"))
    assert data["results"][0]["name"] == "m.ff"
