# fastflowtransform/contracts/core.py
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastflowtransform.config.contracts import (
    ColumnContractModel,
    ContractsDefaultsModel,
    ContractsFileModel,
    ProjectContractsModel,
    parse_contracts_file,
    parse_project_contracts_file,
)
from fastflowtransform.logging import get_logger
from fastflowtransform.schema_loader import Severity, TestSpec

logger = get_logger("contracts")


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _discover_contract_paths(project_dir: Path) -> list[Path]:
    """
    Discover *.contracts.yml files under models/.

    Convention:
      - You can place contracts anywhere under models/, as long as the file
        name ends with ".contracts.yml".
      - Each file describes contracts for one logical table (ContractsFileModel.table).
    """
    models_dir = project_dir / "models"
    if not models_dir.exists():
        return []

    paths: list[Path] = []
    for p in models_dir.rglob("*.contracts.yml"):
        if p.is_file():
            paths.append(p)
    return sorted(paths)


def load_contracts(project_dir: Path) -> dict[str, ContractsFileModel]:
    """
    Load all per-table contracts from *.contracts.yml under models/.

    Returns:
        dict[table_name, ContractsFileModel]
        If multiple files define the same `table`, the last one wins (with a warning).
    """
    contracts: dict[str, ContractsFileModel] = {}
    for path in _discover_contract_paths(project_dir):
        cfg = parse_contracts_file(path)

        table = cfg.table
        if table in contracts:
            logger.warning(
                "Multiple contracts for table %r: overriding previous definition with %s",
                table,
                path,
            )
        contracts[table] = cfg

    return contracts


def _load_project_contracts(project_dir: Path) -> ProjectContractsModel | None:
    """
    Load project-level contracts.yml (if present).

    The file is optional; if it does not exist, None is returned.
    """
    path = project_dir / "contracts.yml"
    if not path.exists():
        return None

    cfg = parse_project_contracts_file(path)

    return cfg


# ---------------------------------------------------------------------------
# Column defaults application
# ---------------------------------------------------------------------------


def _apply_column_defaults(
    col_name: str,
    table: str,
    col: ColumnContractModel,
    defaults: ContractsDefaultsModel | None,
) -> ColumnContractModel:
    """
    Merge project-level column defaults into a column contract.

    Rules:
      - We only consider defaults.columns rules where the regex on
        name matches `col_name` *and* optional table regex matches `table`.
      - Rules are applied in file order; later rules override earlier ones.
      - Per-table contracts take precedence: we only fill attributes that are
        still None on the ColumnContractModel.
    """
    if defaults is None or not defaults.columns:
        return col

    # Start from the explicit per-table column config (already validated)
    data: dict[str, Any] = col.model_dump()

    for rule in defaults.columns:
        m = rule.match
        # name regex is required
        if not re.search(m.name, col_name):
            continue
        # optional table regex
        if m.table and not re.search(m.table, table):
            continue

        # For each field, only apply if current value is None and rule defines a value
        if data.get("type") is None and rule.type is not None:
            data["type"] = rule.type
        if data.get("physical") is None and rule.physical is not None:
            data["physical"] = rule.physical
        if data.get("nullable") is None and rule.nullable is not None:
            data["nullable"] = rule.nullable
        if data.get("unique") is None and rule.unique is not None:
            data["unique"] = rule.unique
        if data.get("enum") is None and rule.enum is not None:
            data["enum"] = list(rule.enum)
        if data.get("regex") is None and rule.regex is not None:
            data["regex"] = rule.regex
        if data.get("min") is None and rule.min is not None:
            data["min"] = rule.min
        if data.get("max") is None and rule.max is not None:
            data["max"] = rule.max
        if data.get("description") is None and rule.description is not None:
            data["description"] = rule.description

    # Re-validate into a ColumnContractModel (cheap; keeps invariants)
    return ColumnContractModel.model_validate(data)


# ---------------------------------------------------------------------------
# DQ test expansion from contracts
# ---------------------------------------------------------------------------


def _contract_tests_for_table(
    table: str,
    contract: ContractsFileModel,
    *,
    defaults: ContractsDefaultsModel | None,
    default_severity: Severity = "error",
) -> list[TestSpec]:
    """
    Convert column contracts for a single table into TestSpec instances, taking
    project-level column defaults into account.
    """
    specs: list[TestSpec] = []

    # Base tags shared by all contract-derived tests. You can always add more
    # tags at the project level later if needed.
    base_tags: list[str] = ["contract"]

    for col_name, col in contract.columns.items():
        effective_col = _apply_column_defaults(col_name, table, col, defaults)

        # 0) Physical type assertion → column_physical_type test
        if effective_col.physical is not None:
            specs.append(
                TestSpec(
                    type="column_physical_type",
                    table=table,
                    column=col_name,
                    params={"physical": effective_col.physical},
                    severity=default_severity,
                    tags=list(base_tags),
                )
            )

        # 1) Nullability: nullable=False → not_null test
        if effective_col.nullable is False:
            specs.append(
                TestSpec(
                    type="not_null",
                    table=table,
                    column=col_name,
                    params={},  # no extra params
                    severity=default_severity,
                    tags=list(base_tags),
                )
            )

        # 1b) Uniqueness → unique test
        if effective_col.unique:
            specs.append(
                TestSpec(
                    type="unique",
                    table=table,
                    column=col_name,
                    params={},
                    severity=default_severity,
                    tags=list(base_tags),
                )
            )

        # 2) Enumerated values → accepted_values test (if any values declared)
        if effective_col.enum:
            specs.append(
                TestSpec(
                    type="accepted_values",
                    table=table,
                    column=col_name,
                    params={"values": list(effective_col.enum)},
                    severity=default_severity,
                    tags=list(base_tags),
                )
            )

        # 3) Numeric range (inclusive) → between test
        if effective_col.min is not None or effective_col.max is not None:
            params: dict[str, Any] = {}
            if effective_col.min is not None:
                params["min"] = effective_col.min
            if effective_col.max is not None:
                params["max"] = effective_col.max

            specs.append(
                TestSpec(
                    type="between",
                    table=table,
                    column=col_name,
                    params=params,
                    severity=default_severity,
                    tags=list(base_tags),
                )
            )

        # 4) Regex constraint → regex_match test (Python-side evaluation)
        if effective_col.regex:
            specs.append(
                TestSpec(
                    type="regex_match",
                    table=table,
                    column=col_name,
                    params={"pattern": effective_col.regex},
                    severity=default_severity,
                    tags=list(base_tags),
                )
            )

    return specs


def build_contract_tests(
    contracts: dict[str, ContractsFileModel],
    *,
    defaults: ContractsDefaultsModel | None = None,
    default_severity: Severity = "error",
) -> list[TestSpec]:
    """
    Convert a set of ContractsFileModel objects into a flat list of TestSpec.

    `defaults` is the (optional) project-level defaults section from contracts.yml.
    """
    if not contracts:
        return []

    all_specs: list[TestSpec] = []
    for table, cfg in contracts.items():
        all_specs.extend(
            _contract_tests_for_table(
                table,
                cfg,
                defaults=defaults,
                default_severity=default_severity,
            )
        )
    return all_specs


def load_contract_tests(project_dir: Path) -> list[TestSpec]:
    """
    High-level helper used by the CLI:

        project_dir -> [TestSpec, ...]

    This is what we plug into `fft test` so contracts become "first-class" tests.
    """
    contracts = load_contracts(project_dir)
    if not contracts:
        return []

    project_cfg = _load_project_contracts(project_dir)
    defaults = project_cfg.defaults if project_cfg is not None else None

    return build_contract_tests(contracts, defaults=defaults)
