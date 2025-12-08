# src/fastflowtransform/seeding.py
from __future__ import annotations

import math
import os
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, NamedTuple, cast

import pandas as pd

from fastflowtransform.config.seeds import SeedColumnConfig, SeedsSchemaConfig, load_seeds_schema
from fastflowtransform.logging import echo
from fastflowtransform.settings import EngineType

# ----------------------------- File I/O & Schema (dtypes) -----------------------------


def _read_seed_file(path: Path) -> pd.DataFrame:
    """Read a seed file (.csv, .parquet, .pq) into a pandas DataFrame."""
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() in (".parquet", ".pq"):
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported seed file format: {path.name}")


def _table_key_candidates(table: str, seed_id: str | None = None) -> list[str]:
    """Generate lookup keys for schema.yml sections (prioritize path-based IDs)."""
    candidates: list[str] = []
    if seed_id:
        candidates.append(seed_id)
        if "/" in seed_id:
            candidates.append(seed_id.replace("/", "."))
    candidates.append(table)
    return [c for c in candidates if c]


def _resolve_dtypes_for_table(
    schema_cfg: SeedsSchemaConfig | None, table: str, seed_id: str | None
) -> dict[str, str]:
    if not schema_cfg:
        return {}
    for key in _table_key_candidates(table, seed_id):
        dtypes = schema_cfg.dtypes.get(key)
        if dtypes:
            return dtypes
    return {}


def _apply_schema(
    df: pd.DataFrame,
    table: str,
    schema_cfg: SeedsSchemaConfig | None,
    seed_id: str | None,
) -> pd.DataFrame:
    """
    Apply optional pandas dtypes from seeds/schema.yml for a given table key.

    The validated configuration is:

      dtypes:
        <table_key>:
          col_a: string
          col_b: int64

    Casting errors are swallowed on purpose to avoid blocking seed loads.
    """
    dtypes = _resolve_dtypes_for_table(schema_cfg, table, seed_id)
    if not dtypes:
        return df

    cast_map = {col: dtype for col, dtype in dtypes.items() if col in df.columns}
    try:
        return df.astype(cast_map)
    except Exception:
        # Prefer loading data over failing the run; you may log a warning here.
        return df


_DEFAULT_COL_TYPE = "string"

_TYPE_ALIASES = {
    "varchar": "string",
    "text": "string",
    "str": "string",
    "int": "integer",
    "int4": "integer",
    "integer": "integer",
    "bigint": "bigint",
    "int8": "bigint",
    "double": "double",
    "float": "double",
    "float64": "double",
    "numeric": "numeric",
    "decimal": "numeric",
    "boolean": "boolean",
    "bool": "boolean",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "timestamptz": "timestamptz",
    "date": "date",
}


def _canonical_type(value: str | None) -> str:
    if not value:
        return _DEFAULT_COL_TYPE
    low = value.strip().lower()
    return _TYPE_ALIASES.get(low, low or _DEFAULT_COL_TYPE)


def _column_schema_for_table(
    schema_cfg: SeedsSchemaConfig | None, table: str, seed_id: str | None
) -> dict[str, SeedColumnConfig]:
    if not schema_cfg:
        return {}
    for key in _table_key_candidates(table, seed_id):
        column_cfg = schema_cfg.columns.get(key)
        if column_cfg:
            return column_cfg
    return {}


def _resolve_column_type_for_engine(
    column: SeedColumnConfig | None,
    engine: str,
) -> str:
    if column is None:
        return _DEFAULT_COL_TYPE
    engine_key = cast(EngineType, engine)
    override = column.engines.get(engine_key)
    if override:
        return _canonical_type(override)
    return _canonical_type(column.type_)


def _cast_series_to_type(series: pd.Series, target: str, table: str, column: str) -> pd.Series:
    kind = target or _DEFAULT_COL_TYPE
    if kind in {"string", "varchar", "text"}:
        return series.astype("string")
    if kind in {"integer", "bigint"}:
        numeric = pd.to_numeric(series, errors="raise")
        return numeric.astype("Int64")
    if kind in {"double", "numeric"}:
        return pd.to_numeric(series, errors="raise")
    if kind == "boolean":
        return series.astype("boolean")
    if kind == "timestamp":
        return pd.to_datetime(series, errors="raise")
    if kind == "timestamptz":
        return pd.to_datetime(series, errors="raise", utc=True)
    if kind == "date":
        dt = pd.to_datetime(series, errors="raise")
        return dt.dt.date
    raise ValueError(f"Unsupported column type '{target}' for {table}.{column} in seeds/schema.yml")


def _apply_column_schema(
    df: pd.DataFrame,
    table: str,
    schema_cfg: SeedsSchemaConfig | None,
    executor: Any,
    seed_id: str | None,
) -> pd.DataFrame:
    column_cfg = _column_schema_for_table(schema_cfg, table, seed_id)
    if not column_cfg:
        return df

    engine = executor.engine_name
    missing = [col for col in column_cfg if col not in df.columns]
    if missing:
        cols = ", ".join(missing)
        raise ValueError(
            f"seeds/schema.yml declares column(s) {cols} "
            f"for table '{table}', but they are not present"
        )

    df_out = df.copy()
    for col, col_cfg in column_cfg.items():
        target_type = _resolve_column_type_for_engine(col_cfg, engine)
        try:
            df_out[col] = _cast_series_to_type(df_out[col], target_type, table, col)
        except Exception as exc:
            raise ValueError(
                f"Failed to cast column '{col}' in '{table}' to type '{target_type}': {exc}"
            ) from exc

    return df_out


def _inject_seed_metadata(df: pd.DataFrame, seed_id: str, path: Path) -> pd.DataFrame:
    """Append metadata columns that track load info for every seed."""
    stamp = datetime.now(UTC)
    df_out = df.copy()
    df_out["_ff_loaded_at"] = stamp
    df_out["_ff_seed_id"] = seed_id
    df_out["_ff_seed_file"] = str(path)
    return df_out


# -------------------------------- Pretty echo helpers ---------------------------------


def _human_int(n: int) -> str:
    """Format integers with thin-space grouping (12 345)."""
    return f"{n:,}".replace(",", " ")


def _human_bytes(n: int) -> str:
    """Coarse byte-size formatting for user hints."""
    mb_threshold = 1024
    if n < mb_threshold:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB", "PB"]
    exp = min(int(math.log(n, 1024)), len(units))
    val = n / (1024**exp)
    unit = units[exp - 1] if exp > 0 else "KB"
    return f"{val:.1f} {unit}"


def _echo_seed_line(
    full_name: str,
    rows: int,
    cols: int,
    engine: str,
    ms: int,
    created_schema: bool = False,
    action: str = "replaced",
    extra: str | None = None,
) -> None:
    """
    Emit a single pretty seed log line, e.g.:
      — ✓ raw.users • 12 345x6 • 1.2 MB • 138 ms [duckdb] (+schema)
    """
    size_hint: str | None = None
    with suppress(Exception):
        # Heuristic: 8 bytes per cell. Good enough as a hint, not exact.
        size_hint = _human_bytes(rows * max(cols, 1) * 8)

    parts = [
        f"✓ {full_name}",
        f"{_human_int(rows)}×{cols}",  # Noqa RUF001
        *([size_hint] if size_hint else []),
        f"{ms} ms",
        f"[{engine}]",
        *(["(+schema)"] if created_schema else []),
        *([extra] if extra else []),
    ]
    echo("— " + " • ".join(parts))


# ------------------------------ Target resolution (CFG) -------------------------------


class SeedTarget(NamedTuple):
    """Resolved seed target (schema, table)."""

    schema: str | None
    table: str


def _seed_id(seeds_dir: Path, path: Path) -> str:
    """
    Build a unique seed ID from the path relative to `seeds/`, without the extension.
    Examples:
      seeds/raw/users.csv      -> "raw/users"
      seeds/staging/users.parquet -> "staging/users"
      seeds/users.csv          -> "users"
    """
    rel = path.relative_to(seeds_dir)
    return rel.with_suffix("").as_posix()


def _resolve_schema_and_table_by_cfg(
    seed_id: str,
    stem: str,
    schema_cfg: SeedsSchemaConfig | None,
    executor: Any,
    default_schema: str | None,
) -> tuple[str | None, str]:
    """
    Resolve (schema, table) using seeds/schema.yml with a clear priority:
      1) targets[<seed_id>] (recommended; path-based ID e.g. "raw/users")
      2) targets[<seed_id with dots>] (optional convenience; "raw.users")
      3) targets[<stem>] (legacy; only safe if the stem is unique)
      4) default_schema (profile/executor-supplied)
    Supports:
      targets:
        raw/users:
          schema: raw
          table: users
          schema_by_engine:
            postgres: raw
            duckdb: main
    """
    schema = default_schema
    table = stem
    if not schema_cfg:
        return schema, table

    targets = schema_cfg.targets

    entry = targets.get(seed_id)
    if not entry:
        # Optional "raw.users" style key as a convenience
        dotted_id = seed_id.replace("/", ".")
        entry = targets.get(dotted_id)

    # stem-based only if present (uniqueness checked by caller)
    if not entry and stem in targets:
        entry = targets[stem]

    if not entry:
        return schema, table

    table = entry.table or table
    engine = executor.engine_name
    engine_key = cast(EngineType, engine)
    schema = entry.schema_by_engine.get(engine_key) or entry.schema_ or schema
    return schema, table


def materialize_seed(
    table: str, df: pd.DataFrame, executor: Any, schema: str | None = None
) -> None:
    """
    Materialize a DataFrame as a database table across engines.
    """
    t0 = perf_counter()
    result, full_name, created_schema = executor.load_seed(table, df, schema)
    dt_ms = int((perf_counter() - t0) * 1000)

    _echo_seed_line(
        full_name=full_name,
        rows=len(df),
        cols=df.shape[1],
        engine=executor.engine_name,
        ms=dt_ms,
        created_schema=created_schema,
        action="replaced",
    )
    return result


# ----------------------------------- Seeding runner -----------------------------------


def _resolve_seeds_dir(project_dir: Path) -> Path:
    """
    Allow overriding the seeds directory via FFT_SEEDS_DIR, falling back to <project>/seeds.
    Relative overrides are resolved against the project directory.
    """
    override = os.getenv("FFT_SEEDS_DIR")
    if override:
        path = Path(override)
        if not path.is_absolute():
            path = project_dir / path
        return path
    return project_dir / "seeds"


def seed_project(project_dir: Path, executor: Any, default_schema: str | None = None) -> int:
    """
    Load every seed file under <project>/seeds recursively and materialize it.

    Supports configuration in seeds/schema.yml (validated via Pydantic):

      targets:
        <seed-id>:                 # e.g., "raw/users" (path-based, recommended)
          schema: <schema-name>    # global target schema
          table: <table-name>      # optional rename
          schema_by_engine:        # optional engine overrides (EngineType keys)
            postgres: raw
            duckdb: main

      dtypes:
        <table-key>:
          column_a: string
          column_b: int64

    Resolution priority for (schema, table):
      1) targets[<seed-id>]  (e.g., "raw/users")
      2) targets[<seed-id with dots>] (e.g., "raw.users")
      3) targets[<stem>] (*only* if stem is unique)
      4) executor.schema or default_schema

    Returns:
      Number of successfully materialized seed tables.

    Raises:
      ValueError: if schema.yml uses a plain stem key while multiple files share that stem.
    """
    seeds_dir = _resolve_seeds_dir(project_dir)
    if not seeds_dir.exists():
        return 0

    # Pydantic-validated seeds/schema.yml (or None if not present)
    schema_cfg = load_seeds_schema(project_dir, seeds_dir=seeds_dir)

    # Collect seed files recursively to allow folder-based schema conventions.
    paths: list[Path] = [
        p
        for p in sorted(seeds_dir.rglob("*"))
        if p.is_file() and p.suffix.lower() in (".csv", ".parquet", ".pq")
    ]
    if not paths:
        return 0

    # Check for ambiguous stems (same filename in different folders).
    stem_counts: dict[str, int] = {}
    for p in paths:
        stem_counts[p.stem] = stem_counts.get(p.stem, 0) + 1

    count = 0
    for path in paths:
        seedid = _seed_id(seeds_dir, path)
        stem = path.stem

        # Default schema may come from executor or caller.
        base_schema = getattr(executor, "schema", None) or default_schema
        schema, table = _resolve_schema_and_table_by_cfg(
            seedid, stem, schema_cfg, executor, base_schema
        )

        # If schema.yml uses a bare stem while that stem exists multiple times,
        # force disambiguation.
        if schema_cfg and stem in schema_cfg.targets and stem_counts.get(stem, 0) > 1:
            raise ValueError(
                f'Seed stem "{stem}" appears multiple times. '
                f"Please configure using the path-based seed ID "
                f'(e.g., "{seedid}") in seeds/schema.yml.'
            )

        df = _read_seed_file(path)
        # Use the resolved *table* key for schema enforcement (allows rename-aware mapping).
        df = _apply_schema(df, table, schema_cfg, seedid)
        df = _apply_column_schema(df, table, schema_cfg, executor, seedid)
        df = _inject_seed_metadata(df, seedid, path)

        materialize_seed(table, df, executor, schema=schema)
        count += 1

    return count
