# tests/test_contracts_module.py
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fastflowtransform.config.contracts import (
    ColumnContractModel,
    ColumnDefaultsRuleModel,
    ColumnMatchModel,
    ContractsDefaultsModel,
    ContractsFileModel,
    PhysicalTypeConfig,
)
from fastflowtransform.contracts.core import (
    _apply_column_defaults,
    _contract_tests_for_table,
    _discover_contract_paths,
    build_contract_tests,
    load_contract_tests,
    load_contracts,
)
from fastflowtransform.schema_loader import TestSpec as _TestSpec

# ---------------------------------------------------------------------------
# Discovery + loading
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discover_contract_paths_no_models_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    paths = _discover_contract_paths(project_dir)
    assert paths == []


@pytest.mark.unit
def test_discover_contract_paths_with_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    models_dir = project_dir / "models" / "staging"
    models_dir.mkdir(parents=True, exist_ok=True)

    f1 = models_dir / "customers.contracts.yml"
    f2 = models_dir / "orders.contracts.yml"
    f1.write_text("version: 1\ntable: customers\ncolumns: {id: {}}\n", encoding="utf-8")
    f2.write_text("version: 1\ntable: orders\ncolumns: {id: {}}\n", encoding="utf-8")

    paths = _discover_contract_paths(project_dir)
    assert f1 in paths
    assert f2 in paths
    # deterministic order (sorted)
    assert paths == sorted(paths)


@pytest.mark.unit
def test_load_contracts_parses_and_maps_by_table(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    models_dir = project_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    # two different tables
    (models_dir / "customers.contracts.yml").write_text(
        textwrap.dedent(
            """
            version: 1
            table: customers
            columns:
              id: {}
            """
        ),
        encoding="utf-8",
    )

    (models_dir / "orders.contracts.yml").write_text(
        textwrap.dedent(
            """
            version: 1
            table: orders
            columns:
              id: {}
            """
        ),
        encoding="utf-8",
    )

    contracts = load_contracts(project_dir)
    assert set(contracts.keys()) == {"customers", "orders"}
    assert isinstance(contracts["customers"], ContractsFileModel)
    assert contracts["customers"].table == "customers"
    assert "id" in contracts["customers"].columns


@pytest.mark.unit
def test_load_contracts_duplicate_tables_last_wins(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    models_dir = project_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    # First definition
    (models_dir / "a_customers.contracts.yml").write_text(
        textwrap.dedent(
            """
            version: 1
            table: customers
            columns:
              id:
                type: string
            """
        ),
        encoding="utf-8",
    )

    # Second definition (should win)
    (models_dir / "z_customers.contracts.yml").write_text(
        textwrap.dedent(
            """
            version: 1
            table: customers
            columns:
              id:
                type: integer
            """
        ),
        encoding="utf-8",
    )

    contracts = load_contracts(project_dir)
    assert list(contracts.keys()) == ["customers"]
    customers_cfg = contracts["customers"]
    assert customers_cfg.columns["id"].type == "integer"


# ---------------------------------------------------------------------------
# Column defaults application
# ---------------------------------------------------------------------------


def _defaults_for_id_columns() -> ContractsDefaultsModel:
    """
    Helper: one default rule for *_id columns.
    """
    rule = ColumnDefaultsRuleModel(
        match=ColumnMatchModel(name=r".*_id$"),
        type="integer",
        physical=PhysicalTypeConfig(default="BIGINT"),
        nullable=False,
        unique=True,
        enum=None,
    )
    return ContractsDefaultsModel(columns=[rule])


@pytest.mark.unit
def test_apply_column_defaults_no_defaults_returns_same_instance() -> None:
    col = ColumnContractModel()  # all None
    result = _apply_column_defaults("customer_id", "customers", col, defaults=None)

    # We re-validate into a new instance, so equality on fields is what matters
    assert result.type is None
    assert result.nullable is None
    assert result.unique is None
    assert result.enum is None
    assert result.physical is None


@pytest.mark.unit
def test_apply_column_defaults_matches_by_name_and_sets_missing_fields() -> None:
    defaults = _defaults_for_id_columns()
    col = ColumnContractModel()  # no explicit settings

    eff = _apply_column_defaults("customer_id", "customers", col, defaults)

    assert eff.type == "integer"
    assert eff.nullable is False
    assert eff.unique is True
    assert eff.enum is None
    assert isinstance(eff.physical, PhysicalTypeConfig)
    assert eff.physical.default == "BIGINT"


@pytest.mark.unit
def test_apply_column_defaults_respects_existing_values() -> None:
    defaults = _defaults_for_id_columns()
    # Explicitly set type and nullable; defaults should NOT override these.
    col = ColumnContractModel(type="string", nullable=True, unique=None)

    eff = _apply_column_defaults("customer_id", "customers", col, defaults)

    # Existing values are kept
    assert eff.type == "string"
    assert eff.nullable is True
    # Only unset attributes are filled
    assert eff.unique is True
    assert isinstance(eff.physical, PhysicalTypeConfig)


@pytest.mark.unit
def test_apply_column_defaults_table_regex_filters_rules() -> None:
    # Rule only applies to tables whose name matches 'orders_.*'
    rule = ColumnDefaultsRuleModel(
        match=ColumnMatchModel(name=r".*_id$", table=r"^orders_.*$"),
        type="integer",
        nullable=False,
    )
    defaults = ContractsDefaultsModel(columns=[rule])

    col = ColumnContractModel()

    # For non-matching table, no defaults applied
    eff_customers = _apply_column_defaults("customer_id", "customers", col, defaults)
    assert eff_customers.type is None
    assert eff_customers.nullable is None

    # For matching table, defaults apply
    eff_orders = _apply_column_defaults("customer_id", "orders_daily", col, defaults)
    assert eff_orders.type == "integer"
    assert eff_orders.nullable is False


@pytest.mark.unit
def test_apply_column_defaults_multiple_rules_extend_but_do_not_override_same_field() -> None:
    # Rule1 sets nullable, rule2 sets type for all columns.
    rule1 = ColumnDefaultsRuleModel(
        match=ColumnMatchModel(name=r".*"),
        nullable=False,
    )
    rule2 = ColumnDefaultsRuleModel(
        match=ColumnMatchModel(name=r".*"),
        type="integer",
    )
    defaults = ContractsDefaultsModel(columns=[rule1, rule2])

    col = ColumnContractModel()
    eff = _apply_column_defaults("customer_id", "customers", col, defaults)

    # Both fields get filled (because they were None)
    assert eff.nullable is False
    assert eff.type == "integer"


# ---------------------------------------------------------------------------
# Contract → TestSpec expansion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_tests_for_table_basic() -> None:
    contract = ContractsFileModel(
        version=1,
        table="customers",
        columns={
            "customer_id": ColumnContractModel(nullable=False, unique=True),
            "status": ColumnContractModel(enum=["active", "inactive"]),
            "amount": ColumnContractModel(min=0, max=100),
        },
    )

    specs = _contract_tests_for_table(
        "customers",
        contract,
        defaults=None,
        default_severity="error",
    )

    # We expect:
    # - not_null + unique for customer_id
    # - accepted_values for status
    # - between for amount
    types_by_col = {(s.column, s.type) for s in specs}

    assert ("customer_id", "not_null") in types_by_col
    assert ("customer_id", "unique") in types_by_col
    assert ("status", "accepted_values") in types_by_col
    assert ("amount", "between") in types_by_col

    # All tests should have table="customers" and tag "contract"
    for s in specs:
        assert s.table == "customers"
        assert "contract" in (s.tags or [])


@pytest.mark.unit
def test_contract_tests_for_table_with_physical_type_defaults() -> None:
    # Column has no physical type itself; defaults will add it
    contract = ContractsFileModel(
        version=1,
        table="customers",
        columns={
            "customer_id": ColumnContractModel(nullable=False),
        },
    )

    defaults = ContractsDefaultsModel(
        columns=[
            ColumnDefaultsRuleModel(
                match=ColumnMatchModel(name=r"^customer_id$"),
                # IMPORTANT: pass a mapping, not a PhysicalTypeConfig instance
                physical=PhysicalTypeConfig(duckdb="BIGINT"),
            )
        ]
    )

    specs = _contract_tests_for_table(
        "customers",
        contract,
        defaults=defaults,
        default_severity="error",
    )

    # Expect both column_physical_type and not_null
    types_by_col = {(s.column, s.type) for s in specs}
    assert ("customer_id", "column_physical_type") in types_by_col
    assert ("customer_id", "not_null") in types_by_col

    physical_specs = [s for s in specs if s.type == "column_physical_type"]
    assert len(physical_specs) == 1
    p = physical_specs[0]

    # params["physical"] is a PhysicalTypeConfig produced by Pydantic
    phys_cfg = p.params["physical"]
    assert hasattr(phys_cfg, "duckdb")
    assert phys_cfg.duckdb == "BIGINT"


# ---------------------------------------------------------------------------
# build_contract_tests / load_contract_tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_contract_tests_multiple_tables() -> None:
    customers = ContractsFileModel(
        version=1,
        table="customers",
        columns={"id": ColumnContractModel(nullable=False)},
    )
    orders = ContractsFileModel(
        version=1,
        table="orders",
        columns={"id": ColumnContractModel(unique=True)},
    )

    contracts = {"customers": customers, "orders": orders}

    specs = build_contract_tests(contracts, defaults=None, default_severity="warn")

    assert {s.table for s in specs} == {"customers", "orders"}
    # one not_null + one unique
    assert any(s.table == "customers" and s.type == "not_null" for s in specs)
    assert any(s.table == "orders" and s.type == "unique" for s in specs)
    # severity is propagated
    assert all(s.severity == "warn" for s in specs)


@pytest.mark.unit
def test_load_contract_tests_integration_with_project_defaults(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    models_dir = project_dir / "models" / "staging"
    models_dir.mkdir(parents=True, exist_ok=True)

    # Per-table contract
    (models_dir / "customers.contracts.yml").write_text(
        textwrap.dedent(
            """
            version: 1
            table: customers
            columns:
              customer_id: {}
              status:
                enum: ["active", "inactive"]
            """
        ),
        encoding="utf-8",
    )

    # Project-level contracts.yml with a default for *_id columns
    (project_dir / "contracts.yml").write_text(
        textwrap.dedent(
            """
            version: 1
            defaults:
              columns:
                - match:
                    name: ".*_id$"
                  nullable: false
            """
        ),
        encoding="utf-8",
    )

    specs = load_contract_tests(project_dir)

    # We expect:
    # - not_null on customers.customer_id (from project defaults)
    # - accepted_values on customers.status (from per-table enum)
    by_col_type = {(s.column, s.type) for s in specs}

    assert ("customer_id", "not_null") in by_col_type
    assert ("status", "accepted_values") in by_col_type

    # All specs should be TestSpec instances
    assert all(isinstance(s, _TestSpec) for s in specs)
