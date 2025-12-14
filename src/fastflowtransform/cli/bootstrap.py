# fastflowtransform/cli/bootstrap.py
from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

import typer
import yaml
from dotenv import dotenv_values
from jinja2 import Environment

from fastflowtransform.config.budgets import BudgetsConfig, load_budgets_config
from fastflowtransform.contracts.core import _load_project_contracts, load_contracts
from fastflowtransform.core import REGISTRY
from fastflowtransform.errors import DependencyNotFoundError
from fastflowtransform.executors.base import BaseExecutor
from fastflowtransform.logging import echo, warn
from fastflowtransform.settings import (
    EngineType,
    EnvSettings,
    Profile,
    resolve_profile as _resolve_profile_impl,
    resolve_utest_profile as _resolve_utest_profile_impl,
)


@dataclass
class CLIContext:
    project: Path
    jinja_env: Environment
    env_settings: EnvSettings
    profile: Profile
    budgets_cfg: BudgetsConfig | None = None

    def make_executor(self) -> tuple[BaseExecutor, Callable, Callable]:
        executor, run_sql, run_py = _make_executor(self.profile, self.jinja_env)
        self._configure_budget_limit(executor)
        return executor, run_sql, run_py

    def _configure_budget_limit(self, executor: Any) -> None:
        if executor is None or not hasattr(executor, "configure_query_budget_limit"):
            return
        limit = None
        engine_name = (self.profile.engine or "").lower()
        if self.budgets_cfg and engine_name:
            entry = self.budgets_cfg.query_limits.get(engine_name)
            if entry:
                limit = entry.max_bytes
        executor.configure_query_budget_limit(limit)


def _resolve_project_path(project_arg: str) -> Path:
    """
    Validate a FastFlowTransform project path:
      - must exist
      - must be a directory
      - must contain 'models/'
    """
    p = Path(project_arg).expanduser().resolve()
    if not p.exists():
        raise typer.BadParameter(
            f"Project path not found: {p}\nTip: Use an absolute path or '.' in the project root."
        )
    if not p.is_dir():
        raise typer.BadParameter(
            f"Project path is not a directory: {p}\nTip: Pass the directory, not the file."
        )
    models = p / "models"
    if not models.exists() or not models.is_dir():
        raise typer.BadParameter(
            f"Invalid project at {p}\n"
            "Expected eian subfolder 'models/'.\n"
            "Tip: change directory to the root and use '.'."
        )
    return p


def _die(msg: str, code: int = 1) -> NoReturn:
    echo(f"\n❌ {msg}")
    raise typer.Exit(code)


def _import_optional(module_path: str, attr: str, *, extra: str | None = None) -> Any:
    try:
        module = importlib.import_module(module_path)
        return getattr(module, attr)
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        if extra:
            _die(
                f"Optional dependency for '{attr}' not found ({exc.name}).\n"
                f"Install it with `pip install fastflowtransform[{extra}]`."
            )
        raise


def _load_project_and_env(project_arg: str) -> tuple[Path, Environment]:
    proj = _resolve_project_path(project_arg)
    try:
        REGISTRY.load_project(proj)
    except DependencyNotFoundError as e:
        echo(str(e))
        raise typer.Exit(1) from e

    jenv = REGISTRY.env
    if jenv is None:
        _die("Internal error: Jinja Environment not initialized after load_project()", code=99)
    return proj, cast(Environment, jenv)


def _load_dotenv_layered(project_dir: Path, env_name: str) -> None:
    """
    Load .env in layers (lowest to highest precedence):
      1) <repo>/.env
      2) <project>/.env
      3) <project>/.env.local
      4) <project>/.env.<env_name>
      5) <project>/.env.<env_name>.local
    """

    original_env = dict(os.environ)
    merged: dict[str, str] = {}

    def _merge(p: Path) -> None:
        try:
            if not p.exists():
                return
            data = dotenv_values(p)
            for key, value in (data or {}).items():
                if value is not None:
                    merged[key] = value
        except Exception:
            pass

    # 1) Repo root defaults
    _merge(Path.cwd() / ".env")
    # 2) Project defaults
    _merge(project_dir / ".env")
    # 3) Project local (gitignored)
    _merge(project_dir / ".env.local")
    # 4) Env-specific
    _merge(project_dir / f".env.{env_name}")
    # 5) Env-specific local (gitignored)
    _merge(project_dir / f".env.{env_name}.local")

    for key, value in merged.items():
        if key not in original_env and value is not None:
            os.environ.setdefault(key, value)


def configure_executor_contracts(project_dir: Path, executor: BaseExecutor | None) -> None:
    """
    Load contracts from project_dir and attach them to the executor (if supported).

    Mirrors the behaviour in `fft run`: parse per-table contracts and the
    project-level contracts.yml; on parse errors, log a warning and continue
    without contracts.
    """
    if executor is None or not hasattr(executor, "configure_contracts"):
        return

    try:
        contracts_by_table = load_contracts(project_dir)
        project_contracts = _load_project_contracts(project_dir)
    except Exception as exc:
        warn(f"[contracts] Failed to load contracts from {project_dir}: {exc}")
        contracts_by_table = {}
        project_contracts = None

    with suppress(Exception):
        executor.configure_contracts(contracts_by_table, project_contracts)


def _resolve_profile(
    env_name: str, engine: EngineType | None, proj: Path
) -> tuple[EnvSettings, Profile]:
    env = EnvSettings()
    if engine is not None:
        env = env.model_copy(update={"ENGINE": engine})
    try:
        prof = _resolve_profile_impl(proj, env_name, env)
    except Exception as exc:
        raise typer.BadParameter(f"Failed to resolve profile '{env_name}': {exc}") from exc

    engine_name = getattr(prof, "engine", None)
    if not engine_name:
        raise typer.BadParameter(
            f"Profile '{env_name}' does not define an engine. "
            "Add one to profiles.yml or pass --engine."
        )

    return env, prof


def _validate_profile_params(env_name: str, prof: Profile) -> None:
    def _fail(msg: str) -> None:
        raise typer.BadParameter(f"Profile '{env_name}' invalid for engine '{prof.engine}': {msg}")

    if prof.engine == "duckdb":
        path = getattr(prof.duckdb, "path", None)
        if not isinstance(path, str) or not path.strip():
            _fail("duckdb.path must be set (profiles.yml → duckdb.path or env FF_DUCKDB_PATH).")
        return

    if prof.engine == "postgres":
        dsn = getattr(prof.postgres, "dsn", None)
        schema = getattr(prof.postgres, "db_schema", None)
        if not dsn or not isinstance(dsn, str) or not dsn.strip():
            _fail("postgres.dsn must be set (profiles.yml → postgres.dsn or env FF_PG_DSN).")
        if schema is None or (isinstance(schema, str) and not schema.strip()):
            _fail(
                "postgres.db_schema must be set (profiles.yml "
                "→ postgres.db_schema or env FF_PG_SCHEMA)."
            )
        return

    if prof.engine == "bigquery":
        dataset = getattr(prof.bigquery, "dataset", None)
        if not dataset or not isinstance(dataset, str) or not dataset.strip():
            _fail(
                "bigquery.dataset must be set (profiles.yml "
                "→ bigquery.dataset or env FF_BQ_DATASET)."
            )
        return

    if prof.engine == "databricks_spark":
        master = getattr(prof.databricks_spark, "master", None)
        app_name = getattr(prof.databricks_spark, "app_name", None)
        if not isinstance(master, str) or not master.strip():
            _fail(
                "databricks_spark.master must be set (profiles.yml "
                "→ databricks_spark.master or env FF_DBR_MASTER)."
            )
        if not isinstance(app_name, str) or not app_name.strip():
            _fail(
                "databricks_spark.app_name must be set (profiles.yml "
                "→ databricks_spark.app_name or env FF_DBR_APPNAME)."
            )
        return

    if prof.engine == "snowflake_snowpark":
        sf = prof.snowflake_snowpark
        required = {
            "account": "snowflake_snowpark.account",
            "user": "snowflake_snowpark.user",
            "password": "snowflake_snowpark.password",
            "warehouse": "snowflake_snowpark.warehouse",
            "database": "snowflake_snowpark.database",
            "db_schema": "snowflake_snowpark.db_schema",
        }
        missing = [label for attr, label in required.items() if not getattr(sf, attr, None)]
        if missing:
            joined = ", ".join(missing)
            _fail(f"{joined} must be set (profiles.yml → snowflake_snowpark.* or env FF_SF_*).")
        return

    if prof.engine is None:
        _fail("engine not specified.")


def _prepare_context(
    project_arg: str,
    env_name: str,
    engine: EngineType | None,
    vars_opt: list[str] | None,
    utest: bool = False,
) -> CLIContext:
    proj = _resolve_project_path(project_arg)
    _load_dotenv_layered(proj, env_name)

    env_settings, base_prof = _resolve_profile(env_name, engine, proj)

    if utest:
        # Use the dedicated utest profile "<env_name>_utest"
        utest_prof = _resolve_utest_profile(env_name, proj, env_settings)
        _validate_profile_params(f"{env_name}_utest", utest_prof)
        prof = utest_prof
    else:
        _validate_profile_params(env_name, base_prof)
        prof = base_prof

    engine_name = getattr(prof, "engine", None)
    REGISTRY.set_active_engine(engine_name)
    if engine_name:
        os.environ["FF_ENGINE"] = engine_name
    else:
        os.environ.pop("FF_ENGINE", None)

    proj_raw, jenv = _load_project_and_env(str(proj))
    proj = Path(proj_raw)

    REGISTRY.set_cli_vars(_parse_cli_vars(vars_opt or []))

    try:
        budgets_cfg = load_budgets_config(proj)
    except Exception as exc:
        raise typer.BadParameter(f"Failed to parse budgets.yml: {exc}") from exc

    return CLIContext(
        project=proj,
        jinja_env=jenv,
        env_settings=env_settings,
        profile=prof,
        budgets_cfg=budgets_cfg,
    )


def _resolve_utest_profile(env_name: str, proj: Path, env: EnvSettings) -> Profile:
    """
    Resolve the utest profile "<env_name>_utest" using the same EnvSettings.
    """
    try:
        # Note: settings.resolve_utest_profile(project_dir, base_env_name, env)
        prof = _resolve_utest_profile_impl(proj, env_name, env)
    except Exception as exc:
        raise typer.BadParameter(
            f"Failed to resolve unit-test profile '{env_name}_utest': {exc}"
        ) from exc
    return prof


def _parse_cli_vars(pairs: list[str]) -> dict[str, object]:
    """
    Parse --vars key=value pairs. Values are YAML-parsed for light typing:
    --vars day='2025-10-01' limit=5 enabled=true tags='[a,b]'
    """
    out: dict[str, object] = {}
    for item in pairs:
        if "=" not in item:
            raise typer.BadParameter(f"--vars expects key=value, got: {item}")
        k, v = item.split("=", 1)
        try:
            out[k] = yaml.safe_load(v)
        except Exception:
            out[k] = v
    return out


def _make_executor(prof: Profile, jenv: Environment) -> tuple[BaseExecutor, Callable, Callable]:
    ex: BaseExecutor
    if prof.engine == "duckdb":
        DuckExecutor = _import_optional(
            "fastflowtransform.executors.duckdb", "DuckExecutor", extra=None
        )
        ex = DuckExecutor(
            db_path=prof.duckdb.path,
            schema=getattr(prof.duckdb, "db_schema", None),
            catalog=getattr(prof.duckdb, "catalog", None),
        )
        return ex, (lambda n: ex.run_sql(n, jenv)), ex.run_python

    if prof.engine == "postgres":
        if prof.postgres.dsn is None:
            raise RuntimeError("Postgres DSN must be set")

        PostgresExecutor = _import_optional(
            "fastflowtransform.executors.postgres", "PostgresExecutor", extra="postgres"
        )
        ex = PostgresExecutor(dsn=prof.postgres.dsn, schema=prof.postgres.db_schema)
        return ex, (lambda n: ex.run_sql(n, jenv)), ex.run_python

    if prof.engine == "bigquery":
        if prof.bigquery.dataset is None:
            raise RuntimeError("BigQuery dataset must be set")

        # Validate env-provided frame selector early (used by examples/Makefiles)
        frame_env = os.getenv("FF_ENGINE_VARIANT") or os.getenv("BQ_FRAME")
        if frame_env:
            frame_normalized = frame_env.lower()
            if frame_normalized not in {"pandas", "bigframes"}:
                raise RuntimeError(
                    f"Unsupported BigQuery frame '{frame_env}'. "
                    "Set FF_ENGINE_VARIANT/BQ_FRAME to 'pandas' or 'bigframes'."
                )

        if prof.bigquery.use_bigframes:
            BigQueryBFExecutor = _import_optional(
                "fastflowtransform.executors.bigquery.bigframes",
                "BigQueryBFExecutor",
                extra="bigquery_bf",
            )
            ex = BigQueryBFExecutor(
                project=prof.bigquery.project or "",
                dataset=prof.bigquery.dataset,
                location=prof.bigquery.location,
                allow_create_dataset=prof.bigquery.allow_create_dataset,
            )
        else:
            BigQueryExecutor = _import_optional(
                "fastflowtransform.executors.bigquery.pandas",
                "BigQueryExecutor",
                extra="bigquery",
            )
            ex = BigQueryExecutor(
                project=prof.bigquery.project or "",
                dataset=prof.bigquery.dataset,
                location=prof.bigquery.location,
                allow_create_dataset=prof.bigquery.allow_create_dataset,
            )
        return ex, (lambda n: ex.run_sql(n, jenv)), ex.run_python

    if prof.engine == "databricks_spark":
        DatabricksSparkExecutor = _import_optional(
            "fastflowtransform.executors.databricks_spark",
            "DatabricksSparkExecutor",
            extra="spark",
        )
        ex = DatabricksSparkExecutor(
            master=prof.databricks_spark.master,
            app_name=prof.databricks_spark.app_name,
            extra_conf=prof.databricks_spark.extra_conf,
            warehouse_dir=prof.databricks_spark.warehouse_dir,
            use_hive_metastore=prof.databricks_spark.use_hive_metastore,
            catalog=prof.databricks_spark.catalog,
            database=prof.databricks_spark.database,
            table_format=prof.databricks_spark.table_format,
            table_options=prof.databricks_spark.table_options,
        )
        return ex, (lambda n: ex.run_sql(n, jenv)), ex.run_python

    if prof.engine == "snowflake_snowpark":
        cfg = {
            "account": prof.snowflake_snowpark.account,
            "user": prof.snowflake_snowpark.user,
            "password": prof.snowflake_snowpark.password,
            "warehouse": prof.snowflake_snowpark.warehouse,
            "database": prof.snowflake_snowpark.database,
            "schema": prof.snowflake_snowpark.db_schema,
            "allow_create_schema": prof.snowflake_snowpark.allow_create_schema,
        }
        if prof.snowflake_snowpark.role:
            cfg["role"] = prof.snowflake_snowpark.role
        SnowflakeSnowparkExecutor = _import_optional(
            "fastflowtransform.executors.snowflake_snowpark",
            "SnowflakeSnowparkExecutor",
            extra="snowflake",
        )
        ex = SnowflakeSnowparkExecutor(cfg)
        return ex, (lambda n: ex.run_sql(n, jenv)), ex.run_python

    _die(f"Unknown engine type: {getattr(prof, 'engine', None)}", code=1)
    raise AssertionError("unreachable")
