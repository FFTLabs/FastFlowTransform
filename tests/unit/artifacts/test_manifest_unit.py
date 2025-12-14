import json
from pathlib import Path

import pytest

from fastflowtransform.artifacts import write_manifest
from fastflowtransform.core import REGISTRY


@pytest.mark.unit
def test_manifest_minimal(tmp_path: Path):
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "m.ff.sql").write_text("select 1 as x", encoding="utf-8")
    (tmp_path / "sources.yml").write_text(
        "version: 1\nsources: []\n",
        encoding="utf-8",
    )

    REGISTRY.load_project(tmp_path)
    p = write_manifest(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "nodes" in data and "manifest.json" in p.name
    n = data["nodes"]["m.ff"]
    assert n["relation"] == "m"
    assert n["path"] == "models/m.ff.sql"
    assert list(sorted(data["nodes"].keys())) == list(data["nodes"].keys())
