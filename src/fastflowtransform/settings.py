# fastflowtransform/settings.py
from __future__ import annotations

import os
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastflowtransform.errors import ProfileConfigError

EngineType = Literal["duckdb", "postgres", "bigquery", "databricks_spark", "snowflake_snowpark"]


class BaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DuckDBConfig(BaseConfig):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    path: str = ":memory:"  # file path or ":memory:"
    db_schema: str | None = Field(default=None, alias="schema")
    catalog: str | None = None


class PostgresConfig(BaseConfig):
    dsn: str | None = None  # e.g. postgresql+psycopg://user:pass@host:5432/db
    db_schema: str = "public"


class ArtifactsPostgresConfig(BaseConfig):
    dsn: str | None = None
    db_schema: str | None = Field(default=None, alias="schema")


class ArtifactsConfig(BaseConfig):
    mode: Literal["files", "db", "both"] = "files"
    engine: Literal["postgres"] = "postgres"
    postgres: ArtifactsPostgresConfig | None = None

    @model_validator(mode="after")
    def _validate_postgres_required(self) -> ArtifactsConfig:
        if self.mode in {"db", "both"}:
            if self.engine != "postgres":
                raise ValueError("artifacts.engine must be 'postgres' for db/both mode.")
            if self.postgres is None:
                raise ValueError("artifacts.postgres must be set for db/both mode.")
            if not self.postgres.dsn or not str(self.postgres.dsn).strip():
                raise ValueError("artifacts.postgres.dsn must be set for db/both mode.")
            if not self.postgres.db_schema or not str(self.postgres.db_schema).strip():
                raise ValueError("artifacts.postgres.db_schema must be set for db/both mode.")
        return self


class BigQueryConfig(BaseConfig):
    project: str | None = None
    dataset: str | None = None
    location: str | None = None
    use_bigframes: bool = True
    allow_create_dataset: bool = False


class DatabricksSparkConfig(BaseConfig):
    master: str = "local[*]"
    app_name: str = "fastflowtransform"
    extra_conf: dict[str, Any] | None = None
    warehouse_dir: str | None = None
    use_hive_metastore: bool = False
    catalog: str | None = None
    database: str | None = None
    table_format: str = "parquet"
    table_options: dict[str, Any] | None = None


class SnowflakeSnowparkConfig(BaseConfig):
    account: str
    user: str
    password: str
    warehouse: str
    database: str
    db_schema: str = Field(alias="schema")
    role: str | None = None
    allow_create_schema: bool = False


class DuckDBProfile(BaseConfig):
    engine: Literal["duckdb"]
    duckdb: DuckDBConfig
    artifacts: ArtifactsConfig | None = None


class PostgresProfile(BaseConfig):
    engine: Literal["postgres"]
    postgres: PostgresConfig
    artifacts: ArtifactsConfig | None = None


class BigQueryProfile(BaseConfig):
    engine: Literal["bigquery"]
    bigquery: BigQueryConfig
    artifacts: ArtifactsConfig | None = None


class DatabricksSparkProfile(BaseConfig):
    engine: Literal["databricks_spark"]
    databricks_spark: DatabricksSparkConfig
    artifacts: ArtifactsConfig | None = None


class SnowflakeSnowparkProfile(BaseConfig):
    engine: Literal["snowflake_snowpark"]
    snowflake_snowpark: SnowflakeSnowparkConfig
    artifacts: ArtifactsConfig | None = None


Profile = Annotated[
    DuckDBProfile
    | PostgresProfile
    | BigQueryProfile
    | DatabricksSparkProfile
    | SnowflakeSnowparkProfile,
    Field(discriminator="engine"),
]


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FF_", env_file=".env", extra="ignore")

    # generic
    ENV: str = Field(default="dev", description="active environment: dev/stg/prod")
    PROJECT_DIR: str | None = None

    # Engine selection (overrides profiles)
    ENGINE: EngineType | None = None

    # DuckDB
    DUCKDB_PATH: str | None = None
    DUCKDB_SCHEMA: str | None = None
    DUCKDB_CATALOG: str | None = None

    # Postgres
    PG_DSN: str | None = None
    PG_SCHEMA: str | None = None

    # Artifacts
    ARTIFACTS_MODE: str | None = None
    ARTIFACTS_PG_DSN: str | None = None
    ARTIFACTS_PG_SCHEMA: str | None = None

    # bigquery
    BQ_PROJECT: str | None = None
    BQ_DATASET: str | None = None
    BQ_LOCATION: str | None = None
    BQ_ALLOW_CREATE_DATASET: int | None = None

    # databricks spark
    DBR_MASTER: str | None = None
    DBR_APPNAME: str | None = None
    DBR_ENABLE_HIVE: int | None = None
    DBR_WAREHOUSE_DIR: str | None = None
    DBR_TABLE_FORMAT: str | None = None
    DBR_DATABASE: str | None = None

    # snowflake snowpark
    SF_ACCOUNT: str | None = None
    SF_USER: str | None = None
    SF_PASSWORD: str | None = None
    SF_ROLE: str | None = None
    SF_WAREHOUSE: str | None = None
    SF_DATABASE: str | None = None
    SF_SCHEMA: str | None = None
    SF_ALLOW_CREATE_SCHEMA: int | None = None

    # --- HTTP / API (optional) ---
    HTTP_CACHE_DIR: str | None = None  # maps to FF_HTTP_CACHE_DIR
    HTTP_TTL: int | None = None  # FF_HTTP_TTL
    HTTP_TIMEOUT: int | None = None  # FF_HTTP_TIMEOUT
    HTTP_MAX_RETRIES: int | None = None  # FF_HTTP_MAX_RETRIES
    HTTP_RATE_LIMIT_RPS: float | None = None  # FF_HTTP_RATE_LIMIT_RPS
    HTTP_OFFLINE: int | None = None  # FF_HTTP_OFFLINE (1/0)
    HTTP_ALLOWED_DOMAINS: str | None = None  # FF_HTTP_ALLOWED_DOMAINS (csv)


# ---------- Loader ----------
def load_profiles(project_dir: Path) -> dict:
    """
    Read project.yml/profiles.yml and return a dict per environment.
    Expected format:
      { "dev": {"engine":"duckdb", "duckdb":{"path":":memory:"}}, ... }
    """
    pf_path = project_dir / "profiles.yml"
    if not pf_path.exists():
        return {}
    raw_text = pf_path.read_text(encoding="utf-8")
    rendered = _render_profiles_template(raw_text, project_dir)
    return yaml.safe_load(rendered) or {}


def _render_profiles_template(text: str, project_dir: Path) -> str:
    def _env(name: str, default: str | None = "") -> str:
        val = os.getenv(name)
        if val is not None:
            return val
        return "" if default is None else str(default)

    jenv = Environment(autoescape=False, undefined=StrictUndefined)
    jenv.globals["env"] = _env
    jenv.globals["project_dir"] = lambda: str(project_dir)
    template = jenv.from_string(text)
    try:
        return template.render()
    except Exception as exc:
        raise ProfileConfigError(f"Failed to render profiles.yml: {exc}") from exc


# ---------- Resolver ----------


def _deep_merge(base: Any, override: Any) -> Any:
    """
    Recursive merge for dicts. Lists/scalars are replaced entirely.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = _deep_merge(out.get(k), v)
        return out
    return override if override is not None else base


def resolve_profile(project_dir: Path, env_name: str, env: EnvSettings) -> Profile:
    profiles: dict[str, dict[str, Any]] = load_profiles(project_dir)
    requested = profiles.get(env_name)
    fallback = profiles.get("default")

    if profiles and requested is None and fallback is None:
        raise ProfileConfigError(
            f"Profile '{env_name}' not found "
            "in profiles.yml (define it or add a 'default' profile)."
        )

    raw: dict[str, Any] = (
        requested or fallback or {"engine": "duckdb", "duckdb": {"path": ":memory:"}}
    )

    _apply_env_overrides(raw, env)
    prof: Profile = TypeAdapter(Profile).validate_python(raw)
    _sanity_check_profile(prof)
    return prof


def resolve_utest_profile(project_dir: Path, base_env_name: str, env: EnvSettings) -> Profile:
    """
    Resolve the *utest* profile for a given base env, e.g.
      base_env_name = "dev_duckdb"  -> profile "dev_duckdb_utest"

    Requirements:
      - base profile (base_env_name) is resolved with env overrides (FF_*).
      - utest profile (base_env_name + "_utest") is resolved from profiles.yml
        WITHOUT env overrides so it cannot accidentally point at the same DB/schema.
      - utest profile MUST exist and MUST be isolated from the base one.
    """
    profiles: dict[str, dict[str, Any]] = load_profiles(project_dir)

    # 1) Resolve the *base* profile normally (with env overrides).
    base_prof: Profile = resolve_profile(project_dir, base_env_name, env)

    # 2) Load the raw utest profile from YAML (no env overrides here).
    utest_env_name = f"{base_env_name}_utest"
    raw_utest = profiles.get(utest_env_name)

    if raw_utest is None:
        raise ProfileConfigError(
            f"Unit-test profile '{utest_env_name}' not found in profiles.yml. "
            f"Define it explicitly to run 'fft utest' for env '{base_env_name}'."
        )

    # Work on a copy and DO NOT call _apply_env_overrides().
    raw_utest_copy = deepcopy(raw_utest)

    # --- Inherit SAFE connection fields from base profile ----------------
    # The idea:
    #   - we inherit things that *do not define isolation*
    #     (e.g. DSN, project, account, warehouse, etc.)
    #   - but we DO NOT inherit things like schema/dataset/path that we want to be different.
    eng = base_prof.engine

    if eng == "postgres":
        base_pg = cast(PostgresProfile, base_prof)
        base_dsn = base_pg.postgres.dsn
        if base_dsn:
            pg_cfg = raw_utest_copy.setdefault("postgres", {})
            pg_cfg.setdefault("dsn", base_dsn)

    elif eng == "bigquery":
        base_bq = cast(BigQueryProfile, base_prof)
        bq_cfg = raw_utest_copy.setdefault("bigquery", {})

        # Safe to inherit: project & location & allow_create_dataset
        if base_bq.bigquery.project is not None:
            bq_cfg.setdefault("project", base_bq.bigquery.project)
        if base_bq.bigquery.location is not None:
            bq_cfg.setdefault("location", base_bq.bigquery.location)
        # dataset is the isolation dimension → MUST be set explicitly in the utest profile
        # and will be checked by _assert_utest_isolated (in CLI/bootstrap).
        if "allow_create_dataset" in base_bq.bigquery.__dict__:
            bq_cfg.setdefault("allow_create_dataset", base_bq.bigquery.allow_create_dataset)

    elif eng == "duckdb":
        base_ddb = cast(DuckDBProfile, base_prof)
        ddb_cfg = raw_utest_copy.setdefault("duckdb", {})
        # Safe-ish to inherit catalog; we do NOT inherit path (isolation) or schema
        if base_ddb.duckdb.catalog is not None:
            ddb_cfg.setdefault("catalog", base_ddb.duckdb.catalog)
        # path & db_schema must be explicitly configured for the utest profile.

    elif eng == "databricks_spark":
        base_dbr = cast(DatabricksSparkProfile, base_prof)
        dbr_cfg = raw_utest_copy.setdefault("databricks_spark", {})
        # Safe to inherit connectivity bits:
        if base_dbr.databricks_spark.master is not None:
            dbr_cfg.setdefault("master", base_dbr.databricks_spark.master)
        if base_dbr.databricks_spark.app_name is not None:
            dbr_cfg.setdefault("app_name", base_dbr.databricks_spark.app_name)
        if base_dbr.databricks_spark.warehouse_dir is not None:
            dbr_cfg.setdefault("warehouse_dir", base_dbr.databricks_spark.warehouse_dir)
        if base_dbr.databricks_spark.catalog is not None:
            dbr_cfg.setdefault("catalog", base_dbr.databricks_spark.catalog)
        # database is the isolation dimension → must differ and will be checked elsewhere.

    elif eng == "snowflake_snowpark":
        base_sf = cast(SnowflakeSnowparkProfile, base_prof)
        sf_cfg = raw_utest_copy.setdefault("snowflake_snowpark", {})
        # Safe to inherit: account/user/password/warehouse/database/role/allow_create_schema
        for attr in (
            "account",
            "user",
            "password",
            "warehouse",
            "database",
            "role",
            "allow_create_schema",
        ):
            val = getattr(base_sf.snowflake_snowpark, attr, None)
            if val is not None:
                sf_cfg.setdefault(attr, val)
        # db_schema (schema) must be explicitly set for the utest profile.

    # 3) Validate the resulting utest profile
    utest_prof: Profile = TypeAdapter(Profile).validate_python(raw_utest_copy)
    _sanity_check_profile(utest_prof)

    return utest_prof


# ---------- ENV-Overrides ----------
def _apply_env_overrides(raw: dict[str, Any], env: EnvSettings) -> None:
    if getattr(env, "ENGINE", None):
        raw["engine"] = env.ENGINE

    eng = str(raw.get("engine", "duckdb")).lower()
    handlers = {
        "duckdb": _ov_duckdb,
        "postgres": _ov_postgres,
        "bigquery": _ov_bigquery,
        "databricks_spark": _ov_databricks_spark,
        "snowflake_snowpark": _ov_snowflake_snowpark,
    }
    raw.setdefault(eng, {})
    handler = handlers.get(eng)
    if handler:
        handler(raw, env)
    _ov_artifacts(raw, env)


def _set_if(d: dict[str, Any], key: str, value: Any | None) -> None:
    if value is not None:
        d[key] = value


def _ov_duckdb(raw: dict[str, Any], env: EnvSettings) -> None:
    duck = raw.setdefault("duckdb", {})
    _set_if(duck, "path", getattr(env, "DUCKDB_PATH", None))
    _set_if(duck, "schema", getattr(env, "DUCKDB_SCHEMA", None))
    _set_if(duck, "catalog", getattr(env, "DUCKDB_CATALOG", None))


def _ov_postgres(raw: dict[str, Any], env: EnvSettings) -> None:
    pg = raw.setdefault("postgres", {})
    _set_if(pg, "dsn", getattr(env, "PG_DSN", None))
    if getattr(env, "PG_SCHEMA", None):
        pg["db_schema"] = env.PG_SCHEMA


def _ov_bigquery(raw: dict[str, Any], env: EnvSettings) -> None:
    bq = raw.setdefault("bigquery", {})
    _set_if(bq, "project", getattr(env, "BQ_PROJECT", None))
    if getattr(env, "BQ_DATASET", None):
        bq["dataset"] = env.BQ_DATASET
    _set_if(bq, "location", getattr(env, "BQ_LOCATION", None))
    uf = os.getenv("FF_BQ_USE_BIGFRAMES")
    if uf is not None:
        bq["use_bigframes"] = uf.lower() in ("1", "true", "yes", "on")

    acd = getattr(env, "BQ_ALLOW_CREATE_DATASET", None)
    if acd is not None:
        if isinstance(acd, str):
            bq["allow_create_dataset"] = acd.strip().lower() in {"1", "true", "yes", "on"}
        else:
            bq["allow_create_dataset"] = bool(acd)


def _ov_databricks_spark(raw: dict[str, Any], env: EnvSettings) -> None:
    dbr = raw.setdefault("databricks_spark", {})
    _set_if(dbr, "master", getattr(env, "DBR_MASTER", None))
    _set_if(dbr, "app_name", getattr(env, "DBR_APPNAME", None))
    _set_if(dbr, "warehouse_dir", getattr(env, "DBR_WAREHOUSE_DIR", None))
    _set_if(dbr, "table_format", getattr(env, "DBR_TABLE_FORMAT", None))
    _set_if(dbr, "database", getattr(env, "DBR_DATABASE", None))

    enable_hive = getattr(env, "DBR_ENABLE_HIVE", None)
    if enable_hive is not None:
        if isinstance(enable_hive, str):
            dbr["use_hive_metastore"] = enable_hive.strip().lower() in {"1", "true", "yes", "on"}
        else:
            dbr["use_hive_metastore"] = bool(enable_hive)
    # ggf. weitere Connect-Parameter hier setzen


def _ov_snowflake_snowpark(raw: dict[str, Any], env: EnvSettings) -> None:
    sf = raw.setdefault("snowflake_snowpark", {})
    _set_if(sf, "account", getattr(env, "SF_ACCOUNT", None))
    _set_if(sf, "user", getattr(env, "SF_USER", None))
    _set_if(sf, "password", getattr(env, "SF_PASSWORD", None))
    _set_if(sf, "warehouse", getattr(env, "SF_WAREHOUSE", None))
    _set_if(sf, "database", getattr(env, "SF_DATABASE", None))
    _set_if(sf, "schema", getattr(env, "SF_SCHEMA", None))
    _set_if(sf, "role", getattr(env, "SF_ROLE", None))

    acs = getattr(env, "SF_ALLOW_CREATE_SCHEMA", None)
    if acs is not None:
        if isinstance(acs, str):
            sf["allow_create_schema"] = acs.strip().lower() in {"1", "true", "yes", "on"}
        else:
            sf["allow_create_schema"] = bool(acs)


def _ov_artifacts(raw: dict[str, Any], env: EnvSettings) -> None:
    mode = getattr(env, "ARTIFACTS_MODE", None)
    pg_dsn = getattr(env, "ARTIFACTS_PG_DSN", None)
    pg_schema = getattr(env, "ARTIFACTS_PG_SCHEMA", None)
    if mode is None and pg_dsn is None and pg_schema is None:
        return

    artifacts = raw.setdefault("artifacts", {})
    if mode is not None:
        artifacts["mode"] = mode

    engine = artifacts.get("engine") or "postgres"
    artifacts["engine"] = engine

    if engine == "postgres":
        pg = artifacts.setdefault("postgres", {})
        if pg_dsn is not None:
            pg["dsn"] = pg_dsn
        if pg_schema is not None:
            pg["db_schema"] = pg_schema


# ---------- Sanity Checks ----------
CheckFn = Callable[[Profile], None]


def _sanity_check_profile(prof: Profile) -> None:
    checks: dict[str, CheckFn] = {
        "postgres": lambda p: _check_postgres(cast(PostgresProfile, p)),
        "bigquery": lambda p: _check_bigquery(cast(BigQueryProfile, p)),
        "snowflake_snowpark": lambda p: _check_snowflake_snowpark(
            cast(SnowflakeSnowparkProfile, p)
        ),
        # "databricks_spark": optional
        # "duckdb": keine Checks erforderlich
    }
    fn = checks.get(prof.engine)
    if fn:
        fn(prof)


def _check_postgres(prof: PostgresProfile) -> None:
    if not prof.postgres.dsn:
        raise ProfileConfigError(
            "Postgres profile missing DSN. Hint: set profiles.yml → postgres.dsn or env FF_PG_DSN."
        )
    if prof.postgres.db_schema == "":
        raise ProfileConfigError(
            "Postgres profile has empty schema. "
            "Hint: set profiles.yml → postgres.db_schema or env FF_PG_SCHEMA."
        )


def _check_bigquery(prof: BigQueryProfile) -> None:
    if not prof.bigquery.dataset:
        raise ProfileConfigError(
            "BigQuery profile missing dataset. "
            "Hint: set profiles.yml → bigquery.dataset or env FF_BQ_DATASET."
        )
    # project kann via ADC kommen → kein Hard-Fail


def _check_snowflake_snowpark(prof: SnowflakeSnowparkProfile) -> None:
    sf = prof.snowflake_snowpark
    missing = [
        k
        for k in ("account", "user", "password", "warehouse", "database", "schema")
        if not getattr(sf, k)
    ]
    if missing:
        miss = ", ".join(missing)
        raise ProfileConfigError(
            f"Snowflake profile missing: {miss}. "
            "Hint: set profiles.yml → snowflake_snowpark.* or env FF_SF_*."
        )


def _assert_utest_isolated(base: Profile, utest: Profile, base_env_name: str) -> None:
    """
    Ensure the utest profile does NOT share the same DB/path/schema with the base profile.
    If it does, raise ProfileConfigError and prevent utest from running.
    """
    # Different engines → nothing to compare here
    if base.engine != utest.engine:
        return

    eng = base.engine

    if eng == "duckdb":
        base_path = cast(DuckDBProfile, base).duckdb.path
        utest_path = cast(DuckDBProfile, utest).duckdb.path
        if base_path and utest_path and base_path == utest_path:
            raise ProfileConfigError(
                f"Unit-test profile '{base_env_name}_utest' must NOT reuse the same DuckDB path "
                f"('{base_path}') as profile '{base_env_name}'. "
                "Configure a separate file/path (e.g. '.local/basic_demo_utest.duckdb' "
                "or ':memory:')."
            )

    elif eng == "postgres":
        base_schema = cast(PostgresProfile, base).postgres.db_schema
        utest_schema = cast(PostgresProfile, utest).postgres.db_schema
        if base_schema == utest_schema:
            raise ProfileConfigError(
                f"Unit-test profile '{base_env_name}_utest' must NOT reuse the same Postgres "
                f"schema ('{base_schema}') as profile '{base_env_name}'. "
                "Use a dedicated schema for unit tests."
            )

    elif eng == "bigquery":
        base_b = cast(BigQueryProfile, base).bigquery
        utest_b = cast(BigQueryProfile, utest).bigquery
        if base_b.project == utest_b.project and base_b.dataset == utest_b.dataset:
            raise ProfileConfigError(
                f"Unit-test profile '{base_env_name}_utest' must NOT reuse the same BigQuery "
                f"project/dataset ('{base_b.project}.{base_b.dataset}') "
                f"as profile '{base_env_name}'. "
                "Use a separate dataset for unit tests."
            )

    elif eng == "databricks_spark":
        base_db = cast(DatabricksSparkProfile, base).databricks_spark.database
        utest_db = cast(DatabricksSparkProfile, utest).databricks_spark.database
        if base_db and utest_db and base_db == utest_db:
            raise ProfileConfigError(
                f"Unit-test profile '{base_env_name}_utest' must NOT reuse the same Databricks "
                f"database ('{base_db}') as profile '{base_env_name}'. "
                "Use a dedicated database for unit tests."
            )

    elif eng == "snowflake_snowpark":
        base_schema = cast(SnowflakeSnowparkProfile, base).snowflake_snowpark.db_schema
        utest_schema = cast(SnowflakeSnowparkProfile, utest).snowflake_snowpark.db_schema
        if base_schema == utest_schema:
            raise ProfileConfigError(
                f"Unit-test profile '{base_env_name}_utest' must NOT reuse the same Snowflake "
                f"schema ('{base_schema}') as profile '{base_env_name}'. "
                "Use a dedicated schema for unit tests."
            )
