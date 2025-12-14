# fastflowtransform/config/sources.py
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

_SOURCE_CFG_FIELDS = {
    "identifier",
    "schema",
    "database",
    "catalog",
    "project",
    "dataset",
    "location",
    "format",
    "options",
}


def _compact_cfg(cfg: Mapping[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in cfg.items():
        if key == "options":
            if value:
                cleaned[key] = dict(value)
            continue
        if value is not None:
            cleaned[key] = value
    return cleaned


def _normalize_options(value: Any, *, field_path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items()}
    raise ValueError(f"sources.yml → {field_path}: expected mapping, got {type(value).__name__}")


def _pick_source_fields(
    data: Mapping[str, Any] | None,
    base: Mapping[str, Any] | None,
    *,
    field_path: str,
) -> dict[str, Any]:
    """Return a dict limited to the supported source configuration fields."""
    data = data or {}
    base = base or {}
    out: dict[str, Any] = {k: base.get(k) for k in _SOURCE_CFG_FIELDS}
    for key, value in data.items():
        if key not in _SOURCE_CFG_FIELDS:
            continue
        if key == "options":
            base_opts = out.get("options") or {}
            incoming = _normalize_options(value, field_path=f"{field_path}.options")
            merged = dict(base_opts)
            merged.update(incoming)
            out["options"] = merged
        else:
            out[key] = value

    if "options" not in out or out["options"] is None:
        out["options"] = {}
    return out


def _normalize_engine_overrides(
    overrides: Mapping[str, Any] | None,
    *,
    field_path: str,
) -> dict[str, dict[str, Any]]:
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise ValueError(
            f"sources.yml → {field_path}: overrides must be a mapping of engine -> config"
        )

    normalized: dict[str, dict[str, Any]] = {}
    for engine, cfg in overrides.items():
        if cfg is None:
            normalized[str(engine)] = {}
            continue
        if not isinstance(cfg, Mapping):
            raise ValueError(
                f"sources.yml → {field_path}[{engine!r}]: "
                f"expected mapping, got {type(cfg).__name__}"
            )
        picked = _pick_source_fields(cfg, None, field_path=f"{field_path}[{engine!r}]")
        normalized[str(engine)] = _compact_cfg(picked)
    return normalized


def _merge_source_configs(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key == "options":
            opts = dict(merged.get("options") or {})
            opts.update(value or {})
            merged["options"] = opts
        else:
            merged[key] = value
    if "options" not in merged or merged["options"] is None:
        merged["options"] = {}
    return merged


def _combine_engine_overrides(
    source_overrides: Mapping[str, dict[str, Any]],
    table_overrides: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    engines = set(source_overrides) | set(table_overrides)
    combined: dict[str, dict[str, Any]] = {}
    for engine in engines:
        combined[engine] = _merge_source_configs(
            source_overrides.get(engine, {}),
            table_overrides.get(engine, {}),
        )
    return combined


FreshnessPeriod = Literal["minute", "hour", "day"]


class FreshnessWindow(BaseModel):
    """Time window: e.g. {count: 12, period: 'hour'}."""

    model_config = ConfigDict(extra="forbid")

    count: int
    period: FreshnessPeriod

    @field_validator("count")
    @classmethod
    def _count_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("freshness.count must be > 0")
        return v


class SourceFreshnessConfig(BaseModel):
    """
    Freshness configuration for a source or table.

      freshness:
        loaded_at_field: my_ts_col
        warn_after:  {count: 12, period: hour}
        error_after: {count: 24, period: hour}
    """

    model_config = ConfigDict(extra="forbid")

    loaded_at_field: str | None = None
    warn_after: FreshnessWindow | None = None
    error_after: FreshnessWindow | None = None

    def merged_with(self, other: SourceFreshnessConfig | None) -> SourceFreshnessConfig:
        """
        Return a new config where self overrides 'other'.
        Useful for table-level override over source-level defaults.
        """
        if other is None:
            return self
        return SourceFreshnessConfig(
            loaded_at_field=self.loaded_at_field or other.loaded_at_field,
            warn_after=self.warn_after or other.warn_after,
            error_after=self.error_after or other.error_after,
        )


def _period_to_minutes(period: FreshnessPeriod) -> int:
    if period == "minute":
        return 1
    if period == "hour":
        return 60
    # period == "day"
    return 60 * 24


def _freshness_to_dict(cfg: SourceFreshnessConfig | None) -> dict[str, Any] | None:
    """
    Convert a SourceFreshnessConfig into a plain dict we can stash in the
    normalized sources registry. Adds convenient *_minutes fields.
    """
    if cfg is None:
        return None

    out: dict[str, Any] = {}
    if cfg.loaded_at_field:
        out["loaded_at_field"] = cfg.loaded_at_field

    def _window_dict(win: FreshnessWindow | None) -> dict[str, Any] | None:
        if win is None:
            return None
        base = {"count": win.count, "period": win.period}
        base["count_in_minutes"] = win.count * _period_to_minutes(win.period)
        return base

    warn_dict = _window_dict(cfg.warn_after)
    err_dict = _window_dict(cfg.error_after)

    if warn_dict:
        out["warn_after"] = warn_dict
    if err_dict:
        out["error_after"] = err_dict

    return out or None


# ---------------------------------------------------------------------------
# Pydantic models mirroring sources.yml structure
# ---------------------------------------------------------------------------


class SourceTableConfig(BaseModel):
    """
    Schema for an individual table entry under a source group.

    We allow extra keys so that future metadata (e.g. owner) doesn't break users,
    but we only *use* the known ones below when normalizing.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    identifier: str | None = None

    # core location fields
    schema_: str | None = Field(default=None, alias="schema")
    database: str | None = None
    catalog: str | None = None
    project: str | None = None
    dataset: str | None = None
    location: str | None = None
    format: str | None = None
    options: dict[str, Any] | None = None

    overrides: dict[str, dict[str, Any]] | None = None

    # metadata
    description: str | None = None
    columns: Any | None = None
    meta: dict[str, Any] | None = None

    freshness: SourceFreshnessConfig | None = None

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_opts(cls, v: Any) -> dict[str, Any] | None:
        if v is None:
            return None
        if isinstance(v, Mapping):
            return {str(k): v for k, v in v.items()}
        raise TypeError("options must be a mapping if provided")


class SourceGroupConfig(BaseModel):
    """
    Schema for each entry under top-level `sources:` in sources.yml.
    """

    model_config = ConfigDict(extra="forbid")

    name: str

    # group-level location defaults
    schema_: str | None = Field(default=None, alias="schema")
    database: str | None = None
    catalog: str | None = None
    project: str | None = None
    dataset: str | None = None
    location: str | None = None
    format: str | None = None
    options: dict[str, Any] | None = None

    overrides: dict[str, dict[str, Any]] | None = None
    description: str | None = None
    meta: dict[str, Any] | None = None
    freshness: SourceFreshnessConfig | None = None

    tables: list[SourceTableConfig]

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_opts(cls, v: Any) -> dict[str, Any] | None:
        if v is None:
            return None
        if isinstance(v, Mapping):
            return {str(k): v for k, v in v.items()}
        raise TypeError("options must be a mapping if provided")


class SourcesFileConfig(BaseModel):
    """
    Strict representation of sources.yml (version 2).
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    sources: list[SourceGroupConfig] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalization: Pydantic → legacy normalized dict
# ---------------------------------------------------------------------------


def _normalize_sources(cfg: SourcesFileConfig) -> dict[str, dict[str, dict[str, Any]]]:
    """
    Convert the strongly typed config into the normalized structure currently
    expected by Registry.sources and resolve_source_entry.

    Shape:
        {
          "<source_name>": {
            "<table_name>": {
              "base": { ...location fields... },
              "overrides": { "<engine>": { ... } },
              "description": ...,
              "columns": ...,
              "meta": ...,
            },
            ...
          },
          ...
        }
    """
    normalized: dict[str, dict[str, dict[str, Any]]] = {}

    for s_idx, src in enumerate(cfg.sources):
        if src.name in normalized:
            raise ValueError(f"sources.yml: duplicate source '{src.name}'.")

        # group defaults & engine overrides
        src_defaults = _pick_source_fields(
            src.model_dump(
                exclude={"tables", "overrides", "name"},
                exclude_none=True,
                by_alias=True,
            ),
            None,
            field_path=f"sources[{s_idx}]",
        )
        src_overrides = _normalize_engine_overrides(
            src.overrides,
            field_path=f"sources[{s_idx}].overrides",
        )

        src_freshness = src.freshness  # SourceFreshnessConfig | None

        group: dict[str, dict[str, Any]] = {}
        for t_idx, tbl in enumerate(src.tables):
            if tbl.name in group:
                raise ValueError(
                    f"sources.yml → source '{src.name}': duplicate table '{tbl.name}'."
                )

            base_cfg = _pick_source_fields(
                tbl.model_dump(
                    include=_SOURCE_CFG_FIELDS,
                    exclude_none=True,
                    by_alias=True,
                ),
                src_defaults,
                field_path=f"sources[{s_idx}].tables[{t_idx}]",
            )
            if not base_cfg.get("identifier") and not base_cfg.get("location"):
                base_cfg["identifier"] = tbl.name

            table_overrides = _normalize_engine_overrides(
                tbl.overrides,
                field_path=f"sources[{s_idx}].tables[{t_idx}].overrides",
            )
            overrides = _combine_engine_overrides(src_overrides, table_overrides)

            # --- Merge freshness & meta/docs -------------------------
            tbl_freshness = tbl.freshness
            merged_freshness_cfg: SourceFreshnessConfig | None = None
            if src_freshness and tbl_freshness:
                merged_freshness_cfg = tbl_freshness.merged_with(src_freshness)
            elif tbl_freshness:
                merged_freshness_cfg = tbl_freshness
            elif src_freshness:
                merged_freshness_cfg = src_freshness

            freshness_dict = _freshness_to_dict(merged_freshness_cfg)

            entry_meta = {
                "description": tbl.description,
                "columns": tbl.columns,
                "meta": tbl.meta,
                "freshness": freshness_dict,
            }

            group[tbl.name] = {
                "base": base_cfg,
                "overrides": overrides,
                **{k: v for k, v in entry_meta.items() if v is not None},
            }

        normalized[src.name] = group

    return normalized


# ---------------------------------------------------------------------------
# Public helper used by core.Registry
# ---------------------------------------------------------------------------


def load_sources_config(project_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """
    Read `sources.yml` under `project_dir`, validate it with Pydantic, and
    return the normalized dict that Registry expects.

    This function is the direct analogue of `parse_project_yaml_config`.
    """
    cfg_path = project_dir / "sources.yml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    try:
        parsed = SourcesFileConfig.model_validate(raw)
    except Exception:  # pydantic.ValidationError, yaml issues bubbled up earlier
        # Let the caller wrap this into a friendlier "Failed to parse sources.yml" message
        raise

    return _normalize_sources(parsed)


# ---------------------------------------------------------------------------
# Optional: resolve_source_entry helper
# ---------------------------------------------------------------------------


def resolve_source_entry(
    entry: Mapping[str, Any],
    engine: str | None,
    *,
    default_identifier: str | None = None,
) -> dict[str, Any]:
    """
    Apply engine overrides to a normalized entry ("base" + "overrides").

    This is unchanged from your current implementation.
    """
    base = entry.get("base") if isinstance(entry, Mapping) else None
    if not isinstance(base, Mapping):
        base = {}

    cfg = dict(base)
    cfg.setdefault("identifier", None)
    cfg.setdefault("schema", None)
    cfg.setdefault("database", None)
    cfg.setdefault("catalog", None)
    cfg.setdefault("project", None)
    cfg.setdefault("dataset", None)
    cfg.setdefault("location", None)
    cfg.setdefault("format", None)
    cfg.setdefault("options", {})

    overrides = entry.get("overrides") if isinstance(entry, Mapping) else None
    if isinstance(overrides, Mapping):
        # wildcard/default overrides
        for wildcard_key in ("*", "default", "any"):
            if wildcard_key in overrides:
                cfg = _merge_source_configs(cfg, overrides[wildcard_key])
        # engine-specific overrides
        if engine and engine in overrides:
            cfg = _merge_source_configs(cfg, overrides[engine])

    ident = cfg.get("identifier")
    if (ident is None or ident == "") and not cfg.get("location"):
        if default_identifier:
            cfg["identifier"] = default_identifier
        else:
            raise KeyError("Source configuration missing identifier or location")

    return cfg
