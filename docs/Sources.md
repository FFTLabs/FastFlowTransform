# Sources Configuration

`sources.yml` declares external tables (seeds, raw inputs, lakehouse paths) that models can reference via `{{ source('group', 'table') }}`. This document covers the schema, engine overrides, file paths, and best practices.

## File Location

Place `sources.yml` at your project root (same level as `models/`). Example:

```text
project/
├── models/
├── sources.yml
└── seeds/
```

## YAML Schema (Version 2)

FastFlowTransform expects a dbt-style structure:

```yaml
version: 1
sources:
  - name: raw
    schema: staging                # default schema for this source group
    overrides:
      postgres:
        schema: raw_main           # engine-specific default override

    tables:
      - name: seed_users
        identifier: seed_users     # optional physical name
        overrides:
          duckdb:
            schema: main
          databricks_spark:
            format: delta
            location: "/mnt/delta/raw/seed_users"
```

### Fields

| Level  | Field                | Description                                                                         |
| ------ | -------------------- | ----------------------------------------------------------------------------------- |
| source | `name`               | Logical group identifier referenced by `source('name', ...)`.                       |
|        | `schema`             | Default target schema/database for the group.                                       |
|        | `database`/`catalog` | Optional qualifiers per engine (BigQuery, Snowflake).                               |
|        | `overrides`          | Map of engine → config snippet (schema overrides, formats, locations).              |
| table  | `name`               | Logical table name (second argument in `source()`).                                 |
|        | `identifier`         | Physical name; defaults to `name` if omitted.                                       |
|        | `location`           | File/path location (used with `format`).                                            |
|        | `format`             | Ingestion format for engines supporting path-based sources (`delta`, `parquet`, …). |
|        | `options`            | Dict of format options (Spark/Databricks).                                          |
|        | `overrides`          | Additional engine-specific settings merged with source-level overrides.             |

Engine-specific overrides follow this merge order:

1. Source defaults (`schema`, `database`, …)
2. Source-level `overrides[engine]`
3. Table-level `overrides[engine]`

### Engine Behavior

* **DuckDB / Postgres / BigQuery / Snowflake**: expect `identifier` (plus `schema`/`database` where relevant). Path-based sources raise errors.
* **Databricks Spark**: supports `format` + `location`. The executor registers a temp view with optional `options` (e.g. `compression`).

### Path-Based Sources Example

```yaml
  - name: raw_events
    tables:
      - name: landing
        overrides:
          databricks_spark:
            format: json
            location: "abfss://landing@storage.dfs.core.windows.net/events/*.json"
            options:
              multiline: true
```

### Example: Typical Project Sources

A typical analytics project mixes **seeded reference data**, **database tables**, and **lakehouse paths**. A single `sources.yml` might look like this:

```yaml
version: 1
sources:
  # Seeded reference data (CSV → tables)
  - name: ref
    schema: ref
    tables:
      - name: countries
        identifier: seed_countries
      - name: currencies
        identifier: seed_currencies

  # Core application database (OLTP / CDC)
  - name: crm
    schema: crm
    overrides:
      postgres:
        schema: public
      bigquery:
        dataset: crm_raw
    tables:
      - name: customers
        identifier: customers
      - name: orders
        identifier: orders

  # Lakehouse-style raw events (Spark-only)
  - name: events
    tables:
      - name: clickstream
        overrides:
          databricks_spark:
            format: parquet
            location: "abfss://raw@storage.dfs.core.windows.net/clickstream/*.parquet"
      - name: pageviews
        overrides:
          databricks_spark:
            format: delta
            location: "abfss://delta@storage.dfs.core.windows.net/pageviews"
```

Models then reference sources in a uniform way:

```sql
-- Seeded lookup
select * from {{ source('ref', 'countries') }};

-- OLTP / warehouse tables
select * from {{ source('crm', 'customers') }};

-- Lakehouse paths (on Spark)
select * from {{ source('events', 'clickstream') }};
```

The executor resolves each reference to the correct physical object for the active engine:

* Postgres: `"public"."customers"`
* BigQuery: `crm_raw.customers`
* Databricks: `delta.` or `parquet.` tables / paths behind the scenes.

## Referencing Sources in Models

```sql
select id, email
from {{ source('raw', 'seed_users') }}
```

After rendering, the executor resolves the fully-qualified relation or path depending on the active engine.

## Seed Integration

When combined with `seeds/schema.yml`, you can map CSV/Parquet seeds into schemas per engine:

```yaml
targets:
  raw/users:
    schema: raw
    schema_by_engine:
      duckdb: main
      postgres: staging
```

### Seed metadata columns

The `fft seed` command automatically appends a small set of metadata columns to every materialized
seed table:

| Column            | Description                                                    |
|-------------------|----------------------------------------------------------------|
| `_ff_loaded_at`   | UTC timestamp captured when the seed was written.              |
| `_ff_seed_id`     | Stable identifier derived from the path inside `seeds/`.       |
| `_ff_seed_file`   | Absolute path of the source file (CSV/Parquet) used to load it.|

These columns live alongside your business fields, so downstream models (and freshness checks)
can reference them directly. For example, point a source freshness rule to `_ff_loaded_at` to
assert “seed data was loaded within the last N minutes” irrespective of the timestamps stored in
the raw file.

## Validation & Errors

* Missing `identifier` *and* `location` produce `KeyError` during rendering.
* Unknown source/table names raise `KeyError` with suggestions.
* Unsupported path-based sources on an engine (`location` provided but no `format`) raise descriptive `NotImplementedError`.

Keep `sources.yml` declarative, use engine overrides for schema differences, and lean on `.env` files where credentials or URIs vary per environment.
