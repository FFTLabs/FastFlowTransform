# fastflowtransform/contracts/runtime/base.by
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from fastflowtransform.config.contracts import (
    ContractsFileModel,
    PhysicalTypeConfig,
    ProjectContractsModel,
    SchemaEnforcementMode,
)
from fastflowtransform.core import Node


class ContractExecutor(Protocol):
    """
    Minimal surface that runtime contracts are allowed to use on an executor.

    Every engine that wants runtime contract support should conform to this.
    """

    ENGINE_NAME: str

    def _execute_sql(self, sql: str, *args: Any, **kwargs: Any) -> Any: ...
    def introspect_column_physical_type(self, table: str, column: str) -> str | None: ...
    def introspect_table_physical_schema(self, table: str) -> dict[str, str]: ...


E = TypeVar("E", bound=ContractExecutor)


@dataclass
class RuntimeContractConfig:
    mode: SchemaEnforcementMode
    allow_extra_columns: bool


@dataclass
class RuntimeContractContext:
    node: Node
    relation: str  # logical relation name, e.g. "customers"
    physical_table: str  # engine-specific identifier used in SQL (e.g. qualified)
    contract: ContractsFileModel | None
    project_contracts: ProjectContractsModel | None
    config: RuntimeContractConfig
    is_incremental: bool = False  # future: incremental support


def _resolve_physical_type_for_engine(
    cfg: PhysicalTypeConfig | None,
    engine_name: str,
) -> str | None:
    if cfg is None:
        return None
    engine = (engine_name or "").lower()
    # exact engine key
    if hasattr(cfg, engine):
        v = getattr(cfg, engine)
        if v:
            return v
    # engine base prefix before underscore; e.g. "snowflake" from "snowflake_snowpark"
    if "_" in engine:
        base = engine.split("_", 1)[0]
        if hasattr(cfg, base):
            v = getattr(cfg, base)
            if v:
                return v
    # fallback to default
    if cfg.default:
        return cfg.default
    return None


def _canonicalize_physical_type(engine_name: str, typ: str | None) -> str | None:
    """
    Apply minimal, engine-specific normalization so expected vs. actual types
    compare predictably. Keep this small and focused on real metadata quirks.
    """
    if typ is None:
        return None
    engine = (engine_name or "").lower()
    t = typ.strip()
    if not t:
        return None

    # Snowflake: information_schema reports all string-family types as TEXT with a
    # length column; normalize common aliases to VARCHAR and drop the huge default.
    if engine.startswith("snowflake"):
        upper = t.upper()
        if upper in {"TEXT", "STRING", "CHAR", "CHARACTER"}:
            return "VARCHAR"
        if re.fullmatch(r"VARCHAR\s*\(\s*16777216\s*\)", upper):
            return "VARCHAR"
        if upper in {"DECIMAL", "NUMERIC"}:
            return "NUMBER"
        return upper

    # Default: case-insensitive comparison only.
    return t.upper()


def expected_physical_schema(
    *,
    executor: ContractExecutor,
    contract: ContractsFileModel | None,
) -> dict[str, str]:
    """
    Build {column_name: expected_physical_type} for the given executor,
    using the per-table ContractsFileModel.
    """
    if contract is None:
        return {}

    engine = getattr(executor, "ENGINE_NAME", "") or ""
    result: dict[str, str] = {}

    for col_name, col_model in (contract.columns or {}).items():
        phys = col_model.physical
        typ = _resolve_physical_type_for_engine(phys, engine)
        if typ:
            canon = _canonicalize_physical_type(engine, typ)
            if canon:
                result[col_name] = canon

    return result


def resolve_runtime_contract_config(
    *,
    table_name: str,
    contract: ContractsFileModel | None,
    project_contracts: ProjectContractsModel | None,
) -> RuntimeContractConfig:
    # 1) table-level override
    if contract and contract.enforce_schema is not None:
        cfg = contract.enforce_schema
        return RuntimeContractConfig(
            mode=cfg.mode,
            allow_extra_columns=cfg.allow_extra_columns,
        )

    # 2) project-level enforcement
    proj = project_contracts.enforcement if project_contracts is not None else None
    if proj is None:
        return RuntimeContractConfig(mode="off", allow_extra_columns=True)

    table_override = (proj.tables or {}).get(table_name)

    mode: SchemaEnforcementMode = proj.default_mode
    allow_extra = proj.allow_extra_columns

    if table_override is not None:
        if table_override.mode is not None:
            mode = table_override.mode
        if table_override.allow_extra_columns is not None:
            allow_extra = table_override.allow_extra_columns

    return RuntimeContractConfig(mode=mode, allow_extra_columns=allow_extra)


class BaseRuntimeContracts[E: ContractExecutor]:
    """
    Base class for engine-specific runtime contract implementations.

    Executors use this via composition: `self.runtime_contracts = ...`.
    """

    executor: E

    def __init__(self, executor: E):
        self.executor = executor

    # ------------------------------------------------------------------ #
    #  Context builder used by the run-engine                            #
    # ------------------------------------------------------------------ #

    def build_context(
        self,
        *,
        node: Node,
        relation: str,
        physical_table: str,
        contract: ContractsFileModel | None,
        project_contracts: ProjectContractsModel | None,
        is_incremental: bool = False,
    ) -> RuntimeContractContext:
        """
        Build a RuntimeContractContext with the correct RuntimeContractConfig.

        The caller (run-engine) decides which contract applies and passes:
          - node:          the fft Node being built
          - relation:      logical name (typically node.name)
          - physical_table: fully-qualified identifier used in SQL
          - contract:      per-table ContractsFileModel, or None
          - project_contracts: parsed project-level contracts.yml, or None
        """
        # Use the contract's declared table name if present, otherwise fall
        # back to the logical relation name for project-level overrides.
        table_key = contract.table if contract is not None else relation

        cfg = resolve_runtime_contract_config(
            table_name=table_key,
            contract=contract,
            project_contracts=project_contracts,
        )

        return RuntimeContractContext(
            node=node,
            relation=relation,
            physical_table=physical_table,
            contract=contract,
            project_contracts=project_contracts,
            config=cfg,
            is_incremental=is_incremental,
        )

    # --- Hooks used by the run-engine ----------------------------

    def apply_sql_contracts(
        self,
        *,
        ctx: RuntimeContractContext,
        select_body: str,
    ) -> None:
        """
        Entry point for SQL models.

        Engines override this to implement verify/cast mode. The default
        implementation just does a plain CTAS (no enforcement).
        """
        # Default = "off" / do nothing special:
        self.executor._execute_sql(f"create or replace table {ctx.physical_table} as {select_body}")

    def verify_after_materialization(self, *, ctx: RuntimeContractContext) -> None:
        """
        Optional second step (e.g. verify mode).

        Called after the model has been materialized. Default is no-op.
        """
        return

    def coerce_frame_schema(self, df: Any, ctx: RuntimeContractContext) -> Any:
        """
        Optional hook for Python models: given a DataFrame-like object and the
        RuntimeContractContext, return a new frame whose column types have been
        coerced to match the expected physical schema (where reasonable).

        Default implementation is a no-op. Engine-specific subclasses may
        override this (e.g. DuckDB + pandas).
        """
        return df

    def materialize_python(
        self,
        *,
        ctx: RuntimeContractContext,
        df: Any,
    ) -> bool:
        """
        Optional hook for Python models.

        Engines override this to take over materialization for Python
        models (e.g. to enforce contracts via explicit CASTs).

        Return True if you fully materialized ctx.physical_table yourself.
        Return False to let the executor use its normal path
        (_materialize_relation / _materialize_incremental).
        """
        return False
