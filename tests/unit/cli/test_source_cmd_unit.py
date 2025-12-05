# tests/unit/cli/test_source_cmd_unit.py
from __future__ import annotations

import pytest
import typer

from fastflowtransform.cli import source_cmd
from fastflowtransform.source_freshness import SourceFreshnessResult


class DummyProfile:
    def __init__(self, engine: str = "duckdb"):
        self.engine = engine


class DummyExecutor:
    def __init__(self):
        # This mimics DuckExecutor having a `.con` attribute
        self.con = object()


class DummyCtx:
    def __init__(self, engine: str = "duckdb"):
        self.profile = DummyProfile(engine)

    def make_executor(self):
        # match the (executor, run_sql, run_py) triple signature
        return DummyExecutor(), None, None


def test_cli_freshness_all_pass(monkeypatch, capsys):
    """
    CLI freshness should print a summary and exit with code 0 when all
    tables pass (no error results).
    """

    # Patch _prepare_context to avoid loading a real project
    def fake_prepare_context(project, env_name, engine, vars):
        assert project == "."
        return DummyCtx(engine="duckdb")

    monkeypatch.setattr(source_cmd, "_prepare_context", fake_prepare_context, raising=True)

    # Patch run_source_freshness to return only passing results
    def fake_run_source_freshness(*args, **kwargs):
        assert kwargs.get("engine") == "duckdb"
        return [
            SourceFreshnessResult(
                source_name="crm",
                table_name="customers",
                relation="dq_demo.seed_customers",
                loaded_at_field="loaded_at",
                delay_minutes=None,
                warn_after_minutes=60,
                error_after_minutes=240,
                status="pass",
                error=None,
            )
        ]

    monkeypatch.setattr(source_cmd, "run_source_freshness", fake_run_source_freshness, raising=True)

    # Call the CLI function directly
    source_cmd.freshness(project=".", env_name="dev_duckdb", engine=None, vars=None)

    out = capsys.readouterr().out

    # Basic sanity checks on output
    assert "Profile: dev_duckdb | Engine: duckdb" in out
    assert "Source Freshness Summary" in out
    assert "crm.customers" in out
    assert "pass" in out

    # No exception -> exit code 0 behaviour is correct


def test_cli_freshness_error_exit(monkeypatch, capsys):
    """
    CLI freshness should exit with code 1 when any result has status 'error'.
    """

    def fake_prepare_context(project, env_name, engine, vars):
        return DummyCtx(engine="duckdb")

    monkeypatch.setattr(source_cmd, "_prepare_context", fake_prepare_context, raising=True)

    def fake_run_source_freshness(executor, engine):
        return [
            SourceFreshnessResult(
                source_name="crm",
                table_name="customers",
                relation="dq_demo.seed_customers",
                loaded_at_field="loaded_at",
                delay_minutes=300.0,
                warn_after_minutes=60,
                error_after_minutes=240,
                status="error",
                error="[freshness] too old",
            )
        ]

    monkeypatch.setattr(source_cmd, "run_source_freshness", fake_run_source_freshness, raising=True)

    with pytest.raises(typer.Exit) as excinfo:
        source_cmd.freshness(project=".", env_name="dev_duckdb", engine=None, vars=None)

    out = capsys.readouterr().out

    assert "crm.customers" in out
    assert "error" in out
    assert excinfo.value.exit_code == 1
