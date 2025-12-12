# fastflowtransform/config/contracts.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fastflowtransform.config.loaders import NoDupLoader
from fastflowtransform.errors import ContractsConfigError

SchemaEnforcementMode = Literal["off", "verify", "cast"]


class PhysicalTypeConfig(BaseModel):
    """
    Engine-specific physical type configuration for a column.

    All fields are optional; you can set:
      - default: applies to all engines if no engine-specific override is set
      - duckdb, postgres, bigquery, snowflake_snowpark, databricks_spark:
        engine-specific physical types (e.g. "integer", "NUMERIC", "TIMESTAMP")

    Example YAML:
      physical: "integer"

      physical:
        default: numeric
        postgres: numeric
        bigquery: NUMERIC
    """

    model_config = ConfigDict(extra="forbid")

    default: str | None = None
    duckdb: str | None = None
    postgres: str | None = None
    bigquery: str | None = None
    snowflake_snowpark: str | None = None
    databricks_spark: str | None = None


class ColumnContractModel(BaseModel):
    """
    Column-level contract definition.

    Example YAML fragment:

        columns:
          id:
            type: integer
            nullable: false
          status:
            type: string
            enum: ["active", "inactive"]
          amount:
            type: double
            nullable: false
            min: 0
            max: 10000
          email:
            type: string
            regex: "^[^@]+@[^@]+$"
    """

    model_config = ConfigDict(extra="forbid")

    # Optional semantic / physical type hint ("integer", "string", "timestamp", ...)
    type: str | None = None

    # Engine-specific physical DB types; see PhysicalTypeConfig.
    physical: PhysicalTypeConfig | None = None

    # Nullability: nullable=False → not_null check
    nullable: bool | None = None

    # Uniqueness: unique=True → unique test
    unique: bool | None = None

    # Enumerated allowed values (accepted_values test)
    enum: list[Any] | None = None

    # Regex constraint; currently used via a generic regex_match test
    regex: str | None = None

    # Numeric range (inclusive) for numeric-like columns
    min: float | int | None = None
    max: float | int | None = None

    # Optional free-form description (handy for docs later)
    description: str | None = None

    @field_validator("enum", mode="before")
    @classmethod
    def _normalize_enum(cls, v: Any) -> list[Any] | None:
        """
        Allow:
          enum: "A"        -> ["A"]
          enum: [1, 2, 3]  -> [1, 2, 3]
        """
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return list(v)
        return [v]

    @field_validator("physical", mode="before")
    @classmethod
    def _coerce_physical(cls, v: Any) -> Any:
        """
        Accept either:
          physical: "integer"
          physical:
            default: numeric
            postgres: numeric
            bigquery: NUMERIC
        and normalize to a PhysicalTypeConfig-compatible dict.
        """
        if v is None:
            return None
        if isinstance(v, PhysicalTypeConfig):
            return v
        if isinstance(v, str):
            # Shorthand: same type for all engines → default
            return {"default": v}
        if isinstance(v, dict):
            # Let Pydantic validate keys; we just pass through.
            return v
        raise TypeError(
            "physical must be either a string or a mapping of engine keys to types "
            "(e.g. {default: numeric, postgres: numeric})"
        )


class TableSchemaEnforcementModel(BaseModel):
    """
    Per-table runtime schema enforcement configuration.

    Example in *.contracts.yml:

        enforce_schema:
          mode: cast          # off | verify | cast
          allow_extra_columns: false
    """

    model_config = ConfigDict(extra="forbid")

    mode: SchemaEnforcementMode = "off"
    allow_extra_columns: bool = True

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_mode(cls, v: Any) -> Any:
        # Allow bare `off` from YAML → False
        if v is False:
            return "off"
        if isinstance(v, str):
            return v.strip().lower()
        return v


class ContractsFileModel(BaseModel):
    """
    One contracts file.

    Convention:
      - One file describes contracts for exactly one table/relation.
      - The table name is what will be used in DQ tests (SELECT ... FROM <table>).

    Example `*.contracts.yml`:

        version: 1
        table: users_enriched
        columns:
          id:
            type: integer
            nullable: false
          status:
            type: string
            enum: ["active", "inactive"]
          email:
            type: string
            nullable: false
            regex: "^[^@]+@[^@]+$"
    """

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    table: str = Field(..., description="Logical/physical table name the contract applies to")
    columns: dict[str, ColumnContractModel] = Field(default_factory=dict)

    enforce_schema: TableSchemaEnforcementModel | None = Field(
        default=None,
        description="Optional runtime schema enforcement config for this table",
    )


# ---------------------------------------------------------------------------
# Project-level contracts (contracts.yml at project root)
# ---------------------------------------------------------------------------


class ColumnMatchModel(BaseModel):
    """
    Column match expression for project-level defaults.

    Currently supports:
      - name: regex on column name (required)
      - table: optional regex on table name (future-proof; optional)
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Regex to match column name")
    table: str | None = Field(
        default=None, description="Optional regex to restrict to specific tables"
    )

    @model_validator(mode="after")
    def _strip(self) -> ColumnMatchModel:
        object.__setattr__(self, "name", self.name.strip())
        if self.table is not None:
            object.__setattr__(self, "table", self.table.strip())
        return self


class ColumnDefaultsRuleModel(BaseModel):
    """
    One rule under defaults.columns in contracts.yml.

    Example:

        defaults:
          columns:
            - match:
                name: ".*_id$"
              type: integer
              nullable: false
            - match:
                name: "created_at"
              type: timestamp
              nullable: false
    """

    model_config = ConfigDict(extra="forbid")

    match: ColumnMatchModel
    # Payload is the same shape as ColumnContractModel but optional:
    type: str | None = None
    physical: PhysicalTypeConfig | None = None
    nullable: bool | None = None
    unique: bool | None = None
    enum: list[Any] | None = None
    regex: str | None = None
    min: float | None = None
    max: float | None = None
    description: str | None = None

    @field_validator("enum", mode="before")
    @classmethod
    def _normalize_enum(cls, v: Any) -> list[Any] | None:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return list(v)
        return [v]

    @field_validator("physical", mode="before")
    @classmethod
    def _coerce_physical(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, PhysicalTypeConfig):
            return v
        if isinstance(v, str):
            return {"default": v}
        if isinstance(v, dict):
            return v
        raise TypeError(
            "defaults.columns[*].physical must be either a string or a mapping of engine "
            "keys to types (e.g. {default: numeric, postgres: numeric})"
        )


class ContractsDefaultsModel(BaseModel):
    """
    Root defaults block for project-level contracts.yml.

    Example:

        version: 1

        defaults:
          models:
            - match:
                name: "staging.*"
              materialized: table

          columns:
            - match:
                name: ".*_id$"
              type: integer
              nullable: false
            - match:
                name: "created_at"
              type: timestamp
              nullable: false
    """

    model_config = ConfigDict(extra="forbid")

    # Future global defaults (e.g. a default severity for contract tests) could live here.
    columns: list[ColumnDefaultsRuleModel] = Field(default_factory=list)


class TableSchemaEnforcementOverrideModel(BaseModel):
    """
    Per-table override in project-level contracts.yml

    Example:

        enforcement:
          tables:
            customers:
              mode: cast
              allow_extra_columns: false
    """

    model_config = ConfigDict(extra="forbid")

    mode: SchemaEnforcementMode | None = None
    allow_extra_columns: bool | None = None


class ProjectSchemaEnforcementModel(BaseModel):
    """
    Project-level schema enforcement defaults (contracts.yml).

    Example:

        version: 1

        enforcement:
          default_mode: verify          # off | verify | cast
          allow_extra_columns: true
          tables:
            customers:
              mode: cast
              allow_extra_columns: false
    """

    model_config = ConfigDict(extra="forbid")

    default_mode: SchemaEnforcementMode = "off"
    allow_extra_columns: bool = True
    tables: dict[str, TableSchemaEnforcementOverrideModel] = Field(default_factory=dict)

    @field_validator("default_mode", mode="before")
    @classmethod
    def _coerce_default_mode(cls, v: Any) -> Any:
        if v is False:
            return "off"
        # Same comment as above if you ever want to accept `true`.
        if isinstance(v, str):
            return v.strip().lower()
        return v


class ProjectContractsModel(BaseModel):
    """
    Top-level model for project-level contracts.yml.

    Only defines defaults, no table-specific contracts (those live in
    per-table *.contracts.yml files).
    """

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    defaults: ContractsDefaultsModel = Field(default_factory=ContractsDefaultsModel)

    enforcement: ProjectSchemaEnforcementModel | None = Field(
        default=None,
        description="Runtime schema enforcement defaults and per-table overrides",
    )


# ---- Parsers -----------------------------------------------------------------


def parse_contracts_file(path: Path) -> ContractsFileModel:
    """
    Load and validate a single *.contracts.yml file.
    Raises a Pydantic validation error or yaml.YAMLError on malformed input.
    """
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=NoDupLoader) or {}
        return ContractsFileModel.model_validate(raw)
    except Exception as exc:
        hint = "Check the contracts YAML for duplicate keys or invalid structure."
        raise ContractsConfigError(
            f"Failed to parse contracts file: {exc}", path=str(path), hint=hint
        ) from exc


def parse_project_contracts_file(path: Path) -> ProjectContractsModel:
    """
    Load and validate the project-level contracts.yml file.
    Returns ProjectContractsModel, raising on malformed input.
    """
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=NoDupLoader) or {}
        return ProjectContractsModel.model_validate(raw)
    except Exception as exc:
        hint = "Check the project-level contracts.yml for duplicate keys or invalid structure."
        raise ContractsConfigError(
            f"Failed to parse project contracts file: {exc}", path=str(path), hint=hint
        ) from exc
