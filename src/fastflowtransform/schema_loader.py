# fastflowtransform/schema_loader.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# ---- Public datatypes -------------------------------------------------------

Severity = Literal["error", "warn"]


@dataclass(frozen=True)
class TestSpec:
    """
    Normalized test spec for CLI.
    Example: not_null(users_enriched.email), unique(users_enriched.id),
              accepted_values(users_enriched.email, values=[...]) …
    """

    type: str  # "not_null" | "unique" | "accepted_values" | …
    table: str
    column: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    severity: Severity = "error"
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.params is None:
            object.__setattr__(self, "params", {})
        if self.tags is None:
            object.__setattr__(self, "tags", [])


# ---- Loader -----------------------------------------------------------------


def load_schema_tests(project_dir: Path) -> list[TestSpec]:
    """
    Loads schema yamls (version: 1) in models/**.yml (& schema.yml),
    and returns normalized TestSpec objects.
    """
    project_dir = Path(project_dir)
    models_dir = project_dir / "models"
    if not models_dir.exists():
        return []

    files: list[Path] = []
    for p in models_dir.rglob("*.yml"):
        files.append(p)
    # Deduplicate
    files = sorted(set(files))

    specs: list[TestSpec] = []
    version = 1
    for yml in files:
        try:
            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if (data or {}).get("version") != version:
            continue
        for model in data.get("models") or []:
            name = str(model.get("name") or "").strip()
            if not name:
                continue
            table = name[:-3] if name.endswith(".ff") else name
            # Column-Tests
            for col in model.get("columns") or []:
                col_name = str(col.get("name") or "").strip()
                if not col_name:
                    continue
                for test_entry in col.get("tests") or []:
                    _expand_test_entry(
                        specs, table, col_name, test_entry, default_tags=model.get("tags") or []
                    )
            # Model-level tests (optional
            for test_entry in model.get("tests") or []:
                _expand_test_entry(
                    specs, table, None, test_entry, default_tags=model.get("tags") or []
                )
    return specs


def _normalize_severity(val: Any) -> Severity:
    """
    Normalize arbitrary user input to a Severity literal ("error"|"warn").
    Defaults to "error" for unknown values.
    """
    s = str(val).lower() if val is not None else "error"
    return "warn" if s == "warn" else "error"


def _expand_test_entry(
    out: list[TestSpec],
    table: str,
    column: str | None,
    test_entry: Any,
    default_tags: list[str],
) -> None:
    """
    test_entry kann sein:
      - "unique"
      - {"unique": {severity: "warn", ...}}
      - {"accepted_values": {values: [...], severity: "error"}}
    """
    if isinstance(test_entry, str):
        t = test_entry
        params: dict[str, Any] = {}
    elif isinstance(test_entry, dict) and len(test_entry) == 1:
        t, params = next(iter(test_entry.items()))
        params = params or {}
    else:
        return

    severity = _normalize_severity(params.pop("severity", "error"))
    tags = list(default_tags or [])
    # Optional: test-specific tags
    if "tags" in params and isinstance(params["tags"], list):
        tags.extend([str(x) for x in params.pop("tags")])

    out.append(
        TestSpec(
            type=str(t),
            table=table,
            column=column,
            params=params,
            severity=severity,
            tags=tags,
        )
    )
