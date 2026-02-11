from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fastflowtransform.logging import warn
from fastflowtransform.settings import _render_profiles_template


@dataclass(frozen=True)
class ResolvedArtifactsDb:
    mode: str  # files|db|both
    dsn: str
    db_schema: str


def _read_profiles_yaml(project_dir: Path) -> dict[str, Any]:
    for name in ("profiles.yml", "profiles.yaml"):
        p = project_dir / name
        if p.exists():
            raw_text = p.read_text(encoding="utf-8")
            rendered = _render_profiles_template(raw_text, project_dir)
            raw = yaml.safe_load(rendered) or {}
            return raw if isinstance(raw, dict) else {}
    return {}


def resolve_artifacts_db(project_dir: Path, env_name: str) -> ResolvedArtifactsDb | None:
    """
    Reads project_dir/profiles.yml and looks for:

      <env_name>.artifacts.mode
      <env_name>.artifacts.postgres.dsn
      <env_name>.artifacts.postgres.db_schema

    Also supports an optional top-level 'profiles:' wrapper:

      profiles:
        dev: {...}
    """
    raw = _read_profiles_yaml(project_dir)
    if not raw:
        return None

    profiles = raw.get("profiles")
    root = profiles if isinstance(profiles, dict) else raw

    env = root.get(env_name)
    if not isinstance(env, dict):
        return None

    artifacts = env.get("artifacts")
    if not isinstance(artifacts, dict):
        return None

    mode = str(artifacts.get("mode") or "files").lower().strip()
    if mode not in ("files", "db", "both"):
        warn(f"[artifacts] invalid artifacts.mode={mode!r}; falling back to 'files'")
        mode = "files"

    engine = str(artifacts.get("engine") or "postgres").lower().strip()
    if engine != "postgres":
        warn(f"[artifacts] artifacts.engine={engine!r} not supported yet; ignoring")
        return None

    pg = artifacts.get("postgres")
    if not isinstance(pg, dict):
        return None

    dsn = pg.get("dsn")
    schema = pg.get("db_schema") or pg.get("schema")
    if not isinstance(dsn, str) or not dsn.strip():
        return None
    if not isinstance(schema, str) or not schema.strip():
        return None

    return ResolvedArtifactsDb(mode=mode, dsn=dsn.strip(), db_schema=schema.strip())
