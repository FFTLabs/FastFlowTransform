# fastflowtransform/config/project.py
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fastflowtransform.config.models import IncrementalConfig, StorageConfig

# ---------------------------------------------------------------------------
# Incremental overlays from project.yml → models.incremental
# ---------------------------------------------------------------------------


class IncrementalModelConfig(BaseModel):
    """
    Per-model incremental overlay from project.yml, for example:

        models:
          incremental:
            fct_events_sql_inline:
              incremental: true
              unique_key: ["event_id"]
              updated_at: "updated_at"
              delta_sql: |
                select ...
              schema_sync: append_new_columns

            fct_events_py_incremental:
              incremental:
                enabled: true
                strategy: merge
                unique_key: ["event_id"]
                updated_at_column: "updated_at"

    This is intentionally compatible with the fields on ModelConfig.
    """

    model_config = ConfigDict(extra="forbid")

    # Master switch / structured config
    incremental: bool | IncrementalConfig | None = None

    # Shortcuts (later merged into ModelConfig)
    unique_key: list[str] | None = None
    primary_key: list[str] | None = None

    updated_at: str | None = None
    updated_at_column: str | None = None
    updated_at_columns: list[str] | None = None
    timestamp_columns: list[str] | None = None

    delta_sql: str | None = None
    delta_config: str | None = None
    delta_python: str | None = None

    schema_sync: Literal["none", "append_new_columns", "sync_all_columns"] | None = None

    @field_validator(
        "unique_key",
        "primary_key",
        "updated_at_columns",
        "timestamp_columns",
        mode="before",
    )
    @classmethod
    def _normalize_key_lists(cls, v: Any) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, str):
            return [v]
        if isinstance(v, Sequence) and not isinstance(v, (str, bytes)):
            return [str(x) for x in v]
        raise TypeError("must be a string or a sequence of strings")


# ---------------------------------------------------------------------------
# models: block from project.yml
# ---------------------------------------------------------------------------


class ModelsBlock(BaseModel):
    """
    project.yml:

        models:
          storage:
            users:
              path: ".local/spark/users"
              format: parquet
            ...

          incremental:
            my_model:
              incremental: true
              unique_key: ["id"]
              updated_at: "updated_at"
    """

    model_config = ConfigDict(extra="forbid")

    storage: dict[str, StorageConfig] = Field(default_factory=dict)
    incremental: dict[str, IncrementalModelConfig] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# seeds: block from project.yml
# ---------------------------------------------------------------------------


class SeedsBlock(BaseModel):
    """
    project.yml:

        seeds:
          storage:
            seed_users:
              path: ".local/spark/seed_users"
              format: parquet
    """

    model_config = ConfigDict(extra="forbid")

    storage: dict[str, StorageConfig] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# docs: block from project.yml
# ---------------------------------------------------------------------------


class DocsColumnConfig(BaseModel):
    """Column-level docs surfaced in generated documentation."""

    model_config = ConfigDict(extra="allow")

    description: str | None = None


class DocsModelConfig(BaseModel):
    """Model-level docs surfaced in generated documentation."""

    # Allow extra keys so docs stay flexible.
    model_config = ConfigDict(extra="allow")

    description: str | None = None
    columns: dict[str, DocsColumnConfig] = Field(default_factory=dict)


class DocsConfig(BaseModel):
    """
    Optional documentation-related configuration.

    Example:

    docs:
      dag_dir: "site/dag"
      include_rendered_sql: true
      models:
        users:
          description: "Raw users table"
          columns:
            id: "Primary key"
    """

    model_config = ConfigDict(extra="forbid")

    dag_dir: str | None = None
    include_rendered_sql: bool = Field(default=False)
    models: dict[str, DocsModelConfig] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# hooks: block from project.yml
# ---------------------------------------------------------------------------


class HookSpec(BaseModel):
    """
    One hook entry from project.yml -> hooks.* lists.
    Example:
      - name: audit_run_start
        kind: sql
        sql: "insert into ..."

      - name: python_banner
        kind: python
        callable: "hooks_demo.hooks.notify:on_run_start"
        select: "tag:example:hooks_demo"
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    kind: Literal["sql", "python"]

    # SQL hook body (for kind == "sql")
    sql: str | None = None

    # Python callable path (for kind == "python"), "pkg.mod:func" or "pkg.mod.func"
    callable: str | None = None

    # Optional selection filter (for before_model / after_model)
    select: str | None = None

    # Optional free-form params if you want them later
    params: Mapping[str, Any] | None = None

    engines: list[str] | None = None  # e.g. ["duckdb", "databricks_spark"]
    envs: list[str] | None = None  # e.g. ["dev_duckdb", "prod_duckdb"]


class HooksConfig(BaseModel):
    """
    Top-level hooks section in project.yml.
    """

    model_config = ConfigDict(extra="forbid")

    on_run_start: list[HookSpec] = Field(default_factory=list)
    on_run_end: list[HookSpec] = Field(default_factory=list)

    # Per-model hooks are optional but allowed
    before_model: list[HookSpec] = Field(default_factory=list)
    after_model: list[HookSpec] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level tests from project.yml (in addition to schema tests)
# ---------------------------------------------------------------------------


class BaseProjectTestConfig(BaseModel):
    """
    Common fields for all project-level tests declared in project.yml under `tests:`.

    NOTE:
      - For table/column-level tests (not_null, unique, ...), `table` and/or `column`
        are required in the concrete subclasses.
      - For reconciliation tests, `table` and `column` are optional and used only
        for display/grouping in summaries.
    """

    model_config = ConfigDict(extra="forbid")

    type: str  # discriminated in concrete subclasses

    severity: Literal["error", "warn"] = "error"
    tags: list[str] = Field(default_factory=list)

    # Optional human-readable label, especially for reconciliations
    name: str | None = None


class NotNullTestConfig(BaseProjectTestConfig):
    """
    not_null test: assert that a column contains no NULL values.
    """

    type: Literal["not_null"]

    # required for this test
    table: str
    column: str

    # optional WHERE predicate
    where: str | None = None


class UniqueTestConfig(BaseProjectTestConfig):
    """
    unique test: detect duplicate values within a column.
    """

    type: Literal["unique"]

    table: str
    column: str

    where: str | None = None


class AcceptedValuesTestConfig(BaseProjectTestConfig):
    """
    accepted_values test: ensure all non-NULL values are inside an allowed set.

    Behaviour:
      - If `values` is None or an empty list, the test is treated as a no-op
        (always passes), but still appears in summaries.
    """

    type: Literal["accepted_values"]

    table: str
    column: str

    # allowed literals (strings, numbers, ...)
    values: list[Any] | None = None
    where: str | None = None


class GreaterEqualTestConfig(BaseProjectTestConfig):
    """
    greater_equal test: require all values to be >= threshold.
    """

    type: Literal["greater_equal"]

    table: str
    column: str

    threshold: float = 0.0


class NonNegativeSumTestConfig(BaseProjectTestConfig):
    """
    non_negative_sum test: validate that SUM(column) is not negative.
    """

    type: Literal["non_negative_sum"]

    table: str
    column: str


class RowCountBetweenTestConfig(BaseProjectTestConfig):
    """
    row_count_between test: ensure row count is between [min_rows, max_rows].

    - `min_rows` defaults to 1.
    - `max_rows` is optional (open-ended upper bound).
    """

    type: Literal["row_count_between"]

    table: str

    min_rows: int = 1
    max_rows: int | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> RowCountBetweenTestConfig:
        """
        Ensure that min_rows is less than or equal to max_rows when both are set.
        """
        if self.max_rows is not None and self.min_rows > self.max_rows:
            raise ValueError(
                f"row_count_between: min_rows ({self.min_rows}) "
                f"must be less than or equal to max_rows ({self.max_rows})."
            )
        return self


class FreshnessTestConfig(BaseProjectTestConfig):
    """
    freshness test: warn or fail when latest timestamp is older
    than `max_delay_minutes`.
    """

    type: Literal["freshness"]

    table: str
    column: str  # timestamp column

    max_delay_minutes: int


class ReconcileExprSide(BaseModel):
    """
    Expression-based reconciliation side (left/right):

      left/right:
        table: str
        expr:  str
        where: optional filter condition
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    expr: str
    where: str | None = None


class ReconcileKeySide(BaseModel):
    """
    Key-based reconciliation side for coverage checks:

      source/target:
        table: str
        key:   str
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    key: str


class ReconcileEqualTestConfig(BaseProjectTestConfig):
    """
    `reconcile_equal` test: compare two scalar expressions with optional tolerances.

    Attributes:
        left (ReconcileExprSide): Left-hand expression (`table`, `expr`, optional `where`).
        right (ReconcileExprSide): Right-hand expression.
        abs_tolerance (float | None): Maximum absolute difference allowed.
        rel_tolerance_pct (float | None): Maximum relative difference (percent).

    Notes:
        The top-level `table`/`column` fields are optional and only used for display in summaries.

    Example (YAML):

    ```yaml
    - type: reconcile_equal
      left:  { table: a_tbl, expr: "sum(x)" }
      right: { table: b_tbl, expr: "sum(y)", where: "dt >= current_date - interval '7 days'" }
      abs_tolerance: 0.01
      rel_tolerance_pct: 1.0
    ```
    """

    type: Literal["reconcile_equal"]

    left: ReconcileExprSide
    right: ReconcileExprSide

    abs_tolerance: float | None = None
    rel_tolerance_pct: float | None = None


class ReconcileRatioWithinTestConfig(BaseProjectTestConfig):
    """
    `reconcile_ratio_within` test: constrain the ratio `left/right` within `[min_ratio, max_ratio]`.

    Attributes:
        left (ReconcileExprSide): Left-hand expression.
        right (ReconcileExprSide): Right-hand expression.
        min_ratio (float): Minimum allowed ratio.
        max_ratio (float): Maximum allowed ratio.

    Example (YAML):

    ```yaml
    - type: reconcile_ratio_within
      left:  { table: orders, expr: "sum(amount)" }
      right: { table: payments, expr: "sum(value)" }
      min_ratio: 0.98
      max_ratio: 1.02
    ```
    """

    type: Literal["reconcile_ratio_within"]

    left: ReconcileExprSide
    right: ReconcileExprSide

    min_ratio: float
    max_ratio: float


class ReconcileDiffWithinTestConfig(BaseProjectTestConfig):
    """
    `reconcile_diff_within` test: limit the absolute difference between two aggregates.

    Attributes:
        left (ReconcileExprSide): Left-hand expression.
        right (ReconcileExprSide): Right-hand expression.
        max_abs_diff (float): Maximum allowed absolute difference.

    Example (YAML):

    ```yaml
    - type: reconcile_diff_within
      left:  { table: a, expr: "count(*)" }
      right: { table: b, expr: "count(*)" }
      max_abs_diff: 10
    ```
    """

    type: Literal["reconcile_diff_within"]

    left: ReconcileExprSide
    right: ReconcileExprSide

    max_abs_diff: float


class ReconcileCoverageTestConfig(BaseProjectTestConfig):
    """
    `reconcile_coverage` test: ensure all keys from `source` exist in `target`.

    Attributes:
        source (ReconcileKeySide): Source side (`table`, `key`).
        target (ReconcileKeySide): Target side (`table`, `key`).
        source_where (str | None): Optional filter predicate applied to the source.
        target_where (str | None): Optional filter predicate applied to the target.

    Example (YAML):

    ```yaml
    - type: reconcile_coverage
      source: { table: crm_users, key: "user_id" }
      target: { table: fact_orders, key: "user_id" }
      source_where: "status = 'active'"
      target_where: "dt >= current_date - interval '30 days'"
    ```
    """

    type: Literal["reconcile_coverage"]

    source: ReconcileKeySide
    target: ReconcileKeySide

    source_where: str | None = None
    target_where: str | None = None


class CustomProjectTestConfig(BaseProjectTestConfig):
    """
    Catch-all config for user-defined tests declared in project.yml under `tests:`.

    - `type`: any non-empty string (must NOT match a built-in test type if you want
      this class to be used; built-ins still win first).
    - `table` / `column`: optional; for tests that don't need them, you can omit.
    - Extra keys are allowed and preserved (e.g. threshold, pattern, window_days).
    """

    # Allow arbitrary extra keys; they'll be visible in model_dump().
    model_config = ConfigDict(extra="allow")

    type: str
    table: str | None = None
    column: str | None = None


ProjectTestConfig = (
    NotNullTestConfig
    | UniqueTestConfig
    | AcceptedValuesTestConfig
    | GreaterEqualTestConfig
    | NonNegativeSumTestConfig
    | RowCountBetweenTestConfig
    | FreshnessTestConfig
    | ReconcileEqualTestConfig
    | ReconcileRatioWithinTestConfig
    | ReconcileDiffWithinTestConfig
    | ReconcileCoverageTestConfig
    | CustomProjectTestConfig
)


# ---------------------------------------------------------------------------
# project.yml - top-level model
# ---------------------------------------------------------------------------


class ProjectConfig(BaseModel):
    """
    Strict representation of project.yml.

    Example:

        name: duckdb_api_demo
        version: "0.1"

        vars: {}

        models:
          storage: { ... }
          incremental: { ... }

        seeds:
          storage: { ... }

        tests:
          - type: not_null
            table: mart_users_join
            column: user_id
            tags: [batch]
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str | int

    # Models directory (in case you want this configurable)
    models_dir: str = "models"

    # Arbitrary variables that can be accessed via var('key') in Jinja
    vars: dict[str, Any] = Field(default_factory=dict)

    models: ModelsBlock = Field(default_factory=ModelsBlock)
    seeds: SeedsBlock = Field(default_factory=SeedsBlock)

    tests: list[ProjectTestConfig] = Field(default_factory=list)

    docs: DocsConfig | None = None

    hooks: HooksConfig | None = None


# ---------------------------------------------------------------------------
# Helper: load & validate project.yml
# ---------------------------------------------------------------------------


def parse_project_yaml_config(project_dir: Path) -> ProjectConfig:
    """
    Read project.yml under `project_dir` and validate it strictly using Pydantic.

    Typical usage inside core._load_project_yaml:

        from fastflowtransform.config.project import parse_project_yaml_config

        proj_cfg = parse_project_yaml_config(project_dir)
        self.project_vars = dict(proj_cfg.vars or {})

        # models.storage → storage.set_model_storage(...)
        # seeds.storage  → storage.set_seed_storage(...)
        # models.incremental → self.incremental_models = ...
    """
    cfg_path = project_dir / "project.yml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return ProjectConfig.model_validate(raw)
