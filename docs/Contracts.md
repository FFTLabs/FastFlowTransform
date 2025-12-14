# Contracts

FastFlowTransform supports **data contracts**: declarative expectations about your
tables and columns. Contracts are stored in YAML files and are compiled into
normal `fft test` checks.

You get:

- A place to describe the **intended schema** (types, nullability, enums, etc.)
- Automatic **data-quality tests** derived from those contracts
- Optional checks for the **physical DB data type** (per engine)

Contracts live in two places:

- Per-table: `models/**/<table>.contracts.yml`
- Project-level defaults: `contracts.yml` at the project root


---

## Per-table contracts (`*.contracts.yml`)

For each logical table you can create a `*.contracts.yml` file under `models/`.

**Convention**

- File name: ends with `.contracts.yml`
- Location: anywhere under `models/`
- Each file describes **exactly one table**

Example:

```yaml
# models/staging/customers.contracts.yml
version: 1
table: customers

columns:
  customer_id:
    type: integer
    physical:
      duckdb: BIGINT
      postgres: integer
      bigquery: INT64
      snowflake_snowpark: NUMBER
      databricks_spark: BIGINT
    nullable: false
    unique: true

  name:
    type: string
    nullable: false

  status:
    type: string
    nullable: false
    enum:
      - active
      - inactive

  created_at:
    type: timestamp
    nullable: false
````

The `table` name should match the logical relation name you use in your models
(e.g. `relation_for("customers")`).

---

## Column attributes

Each entry under `columns:` is a **column contract**.

Supported attributes:

```yaml
columns:
  some_column:
    type: string                # optional semantic type
    physical:                   # optional physical DB type(s)
      duckdb: VARCHAR
      postgres: text
    nullable: false             # nullability contract
    unique: true                # uniqueness contract
    enum: [a, b, c]             # allowed values
    regex: "^[A-Z]{2}[0-9]{4}$" # regex pattern
    min: 0                      # numeric min (inclusive)
    max: 100                    # numeric max (inclusive)
    description: "Human note"   # free-form description
```

### `type` (semantic type)

Free-form semantic type hint, things like:

* `integer`
* `string`
* `timestamp`
* `boolean`
* …

Right now this is **documentation / intent only**; it does not generate tests by itself.
Use it to communicate intent and align with your physical types.

---

### `physical` (engine-specific physical DB type)

`physical` describes the **actual DB type** of the column, per engine.

There are two forms:

**1) Shorthand string**

```yaml
physical: BIGINT
```

This is interpreted as:

```yaml
physical:
  default: BIGINT
```

**2) Per-engine mapping**

```yaml
physical:
  default: BIGINT          # fallback if no engine-specific key is set
  duckdb: BIGINT
  postgres: integer
  bigquery: INT64
  snowflake_snowpark: NUMBER
  databricks_spark: BIGINT
```

Supported keys:

| Key                  | Engine / executor           |
| -------------------- | --------------------------- |
| `default`            | Fallback for all engines    |
| `duckdb`             | DuckDB executor             |
| `postgres`           | Postgres executor           |
| `bigquery`           | BigQuery executors          |
| `snowflake_snowpark` | Snowflake Snowpark executor |
| `databricks_spark`   | Databricks / Spark executor |

> **Important**
>
> The value here must match what your warehouse reports in its catalog /
> information schema for that column (e.g. `INT64` in BigQuery, `NUMBER` in
> Snowflake, etc.).

Each `physical` contract is turned into a `column_physical_type` test.
If the engine does not yet support physical type introspection, the test will
fail with a clear “engine not yet supported” message instead of silently
passing.

### Engine-canonical type names

Physical type comparisons use the **canonical type strings reported by the engine**.

That means:

* Some engines expose aliases as canonical names in their catalogs.

  * Example (Postgres):

    * `timestamp` is an alias for `timestamp without time zone`
    * `timestamptz` is an alias for `timestamp with time zone`
* FFT compares types after **engine-specific canonicalization**, so contracts can use common names like `timestamp`/`timestamptz` while still matching what Postgres reports.

If you see a mismatch like:

> expected `timestamp`, got `timestamp without time zone`

it means your Postgres executor/runtime is not canonicalizing types yet (or you’re using raw `information_schema.data_type`). In that case, update Postgres type introspection to use `pg_catalog.format_type(...)` so comparisons are consistent.

---

### `nullable`

```yaml
nullable: false
```

* `nullable: false` → generates a `not_null` test for this column.
* `nullable: true` or omitted → no nullability test.

---

### `unique`

```yaml
unique: true
```

* `unique: true` → generates a `unique` test for this column.
* `unique: false` or omitted → no uniqueness test.

---

### `enum`

```yaml
enum:
  - active
  - inactive
  - pending
```

`enum` defines a finite set of allowed values and generates an
`accepted_values` test.

You can also use a single scalar:

```yaml
enum: active
```

which is treated as `["active"]`.

---

### `regex`

```yaml
regex: "^[^@]+@[^@]+$"
```

`regex` defines a pattern that all non-null values must match. It generates a
`regex_match` test.

---

### `min` / `max`

```yaml
min: 0
max: 100
```

`min` and `max` define an inclusive numeric range and generate a `between` test.

You can specify just one side:

```yaml
min: 0        # only lower bound
# or
max: 100      # only upper bound
```

---

### `description`

```yaml
description: "Customer signup timestamp in UTC"
```

Free-form description field. This does not generate tests; it’s for docs /
tooling.

---

## Project-level contracts (`contracts.yml`)

You can define **project-wide defaults** in a single `contracts.yml` file at
the project root.

This file only defines **defaults**, not concrete tables.

Example:

```yaml
# contracts.yml
version: 1

defaults:
  columns:
    # All *_id columns are non-null integers with engine-specific types
    - match:
        name: ".*_id$"
      type: integer
      nullable: false
      physical:
        duckdb: BIGINT
        postgres: integer
        bigquery: INT64

    # created_at should always be a non-null timestamp
    - match:
        name: "^created_at$"
      type: timestamp
      nullable: false
```

### `contracts.yml` enforcement configuration

Example:

```yaml
version: 1

defaults:
  columns:
    - match:
        name: ".*_id$"
      type: integer
      nullable: false

enforcement:
  # Modes: off | verify | cast
  default_mode: off

  # If true, contract enforcement only cares about declared columns.
  # Extra columns produced by the model are allowed.
  allow_extra_columns: true

  # Optional per-table overrides (by logical relation name)
  tables:
    mart_users_by_domain:
      mode: verify
      allow_extra_columns: true

    mart_latest_signup:
      mode: cast
      allow_extra_columns: true
```

Rules:

* `enforcement.default_mode` applies to all tables unless overridden.
* `enforcement.tables.<table>.mode` overrides the default for a single table.
* `allow_extra_columns` controls whether the model output may contain columns not listed in the contract:

  * `true`: extra columns are ignored by enforcement (but still exist in the table)
  * `false`: extra columns fail enforcement


### Column match rules

Each entry under `defaults.columns` is a **column default rule**:

```yaml
- match:
    name: "regex on column name"  # required
    table: "regex on table name"  # optional
  type: ...
  physical: ...
  nullable: ...
  unique: ...
  enum: ...
  regex: ...
  min: ...
  max: ...
  description: ...
```

* `match.name`
  Required **regex** applied to the column name.

* `match.table`
  Optional **regex** applied to the table name.

All the other fields are the same as in `*.contracts.yml`. They act as
**defaults**.

### How defaults are applied

For each column contract from a per-table file:

1. All `defaults.columns` rules are evaluated **in file order**.
2. A rule applies if both:

   * `match.name` matches the column name, and
   * `match.table` is empty or matches the table name.
3. For every applicable rule:

   * Fields that are currently `null` / unset on the column are **filled** from
     the rule.
   * Fields that are already set on the column are **not overridden**.

**Per-table contracts always win.**
Defaults only fill in missing values.

Example:

```yaml
# contracts.yml
defaults:
  columns:
    - match:
        name: ".*_id$"
      nullable: false
      physical: BIGINT
```

```yaml
# models/orders.contracts.yml
version: 1
table: orders
columns:
  customer_id:
    # nullable unspecified → inherited as false from defaults
    physical:
      duckdb: BIGINT
      postgres: integer  # overrides default
```

Effective contract for `orders.customer_id`:

```yaml
type: null
nullable: false                 # from defaults
physical:
  duckdb: BIGINT                # from per-table
  postgres: integer             # from per-table
  default: BIGINT               # from defaults.physical (other engines)
unique: null
...
```

---

## How contracts become tests

Contracts are turned into regular `TestSpec` entries used by `fft test`.

For each column:

| Contract field    | Generated test type    | Notes                        |
| ----------------- | ---------------------- | ---------------------------- |
| `physical`        | `column_physical_type` | Uses engine-specific mapping |
| `nullable: false` | `not_null`             |                              |
| `unique: true`    | `unique`               |                              |
| `enum`            | `accepted_values`      |                              |
| `min` / `max`     | `between`              | inclusive range              |
| `regex`           | `regex_match`          | Python regex                 |

All contract-derived tests:

* Use **severity** `error` by default (today)
* Receive the tag `contract` (so you can filter on them later)

Example for `customers`:

```yaml
# models/staging/customers.contracts.yml
version: 1
table: customers
columns:
  customer_id:
    nullable: false
    unique: true
    physical:
      duckdb: BIGINT
  status:
    enum: [active, inactive]
```

This yields tests roughly equivalent to:

```text
customers.customer_id not_null (tags: contract)
customers.customer_id unique (tags: contract)
customers.customer_id column_physical_type (tags: contract)
customers.status accepted_values (tags: contract)
```

You don’t need to write those tests yourself; they’re derived automatically
from the contract files.

### Runtime enforcement (optional)

In addition to turning contracts into `fft test` checks, FastFlowTransform can **enforce** contracts **at runtime** while building models.

Runtime enforcement means:

* FFT can **verify** that the materialized table matches the contract schema, and fail the run if not.
* FFT can **cast** the model output into the declared physical types before creating the table.

This is configured in **project-level `contracts.yml`** under `enforcement`.

#### Enforcement modes

Contracts enforcement supports three modes:

* `off`
  Do not enforce at build time. (Contracts may still generate tests.)

* `verify`
  Build the table normally, then verify the physical schema matches the contract.

* `cast`
  Build the table by selecting from your model and **casting** contract columns into their declared physical types, then verify.

> `cast` is useful when your warehouse would infer “close but not exact” types (e.g. `COUNT(*)` becoming a sized numeric type) and you want stable physical types across engines.

### Failure messages

If enforcement fails, FFT raises an error like:

* Missing/extra columns
* Type mismatch (expected vs actual physical type)
* Non-null/unique contract failures (if those are enforced at runtime in your setup)

The error includes the table name and a list of mismatches.

### Enforcement with incremental models

When a model is materialized as `incremental`, FFT applies enforcement to the **incremental write path**, not only full refresh.

Typical behavior:

* On the first run, the model creates the target relation (full refresh behavior) and enforcement is applied.
* On subsequent runs, FFT computes a delta dataset and writes it using the engine’s incremental strategy (insert/merge/delete+insert, etc.).
* Enforcement is applied so the target table remains compatible with the contract.

Practical recommendations:

* If the incremental model relies on `unique_key`, make sure your source change simulation does not introduce duplicated keys in the delta.
* For “update simulation” in demos, prefer a **second full seed file** that represents the entire source after the update (not just appended rows), then rerun incremental. This produces a realistic “source changed” scenario without creating duplicates.

### Tests vs runtime enforcement

Contracts can be used in two independent ways:

1. **Tests** (`fft test`)
   Contracts generate test specs like `not_null`, `unique`, `accepted_values`, `regex_match`, and `column_physical_type`.

2. **Runtime enforcement** (`fft run`)
   Enforcement runs during model materialization and can fail the run early.

You can use either one alone, or both together.

### Enforcement for SQL models

When enforcing contracts for a SQL model:

* `verify` mode:

  1. FFT creates the table/view normally from the model SQL
  2. FFT introspects the created object and compares the physical schema to the contract

* `cast` mode:

  1. FFT wraps the model SQL in a projection that casts the declared columns:

     ```sql
     select
       cast(col_a as <physical-type>) as col_a,
       cast(col_b as <physical-type>) as col_b,
       ...
       -- optionally include extra columns if allow_extra_columns=true
     from (<model select>) as src
     ```
  2. FFT creates the table from that casted SELECT
  3. FFT verifies the resulting physical schema

Notes:

* Enforcement is best-effort: if a contract has no physical types for the current engine, `cast` mode cannot enforce and will fail with a clear error.
* `allow_extra_columns=true` means non-contracted columns are carried through unchanged.

### Enforcement for Python models

For Python models (pandas / Spark / Snowpark / BigFrames):

* FFT first materializes the DataFrame result according to the executor.
* If enforcement is enabled, the runtime contracts layer may:

  * Stage the DataFrame into a temporary table (engine-specific)
  * Re-create the target table using casts (`cast` mode)
  * Or only verify the schema (`verify` mode)

This allows a consistent enforcement mechanism even when the model result is not expressed as SQL.

---

## Using contracts with `fft test`

The high-level flow:

1. You define `*.contracts.yml` under `models/` and, optionally, a root
   `contracts.yml` with defaults.
2. `fft test` loads:

   * all per-table contracts
   * project-level defaults
3. Contracts are expanded into test specs.
4. Tests are executed like any other `fft test` checks.

If a contract file is malformed (YAML, duplicate keys, or schema), FFT raises a
friendly `ContractsConfigError` with a hint. The test run will fail until the
file is fixed, rather than silently skipping it.

---

## Current limitations

A few things contracts **do not** do yet:

* Contracts **do not change DDL**: tables are still created with the types
  inferred by the warehouse from your `SELECT`.
* `type` (semantic type) is not used to alter the schema; it is for intent /
  documentation.
* Physical type checks require engine support:

  * Currently, only engines that can introspect their `INFORMATION_SCHEMA`
    and expose that to FFT can fully enforce `column_physical_type`.
  * Other engines may reject such tests with a clear “engine not supported”
    message.

### Current limitations

* Enforcement behavior can differ by engine depending on what the executor can introspect and how it stages/casts data.
* `cast` mode requires explicit `physical` types for the current engine.
* Some warehouses expose “decorated” physical types (e.g. `VARCHAR(16777216)`, `NUMBER(18,0)`) rather than a short base type name. Contracts should match the canonical/normalized representation used by the engine implementation.
