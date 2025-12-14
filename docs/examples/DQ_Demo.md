# Data Quality Demo Project

The **Data Quality Demo** shows how to combine:

- **Built-in FFT data quality tests**
- **Tests generated from data contracts (`*.contracts.yml` + `contracts.yml`)**
- **Custom DQ tests (Python & SQL)**
- **Multiple engines** (DuckDB, Postgres, Databricks Spark, BigQuery, Snowflake Snowpark)

on a small, understandable model:

- **Column checks (from contracts + project.yml):**
  - `column_physical_type`
  - `not_null`
  - `unique`
  - `accepted_values`
  - `between`
  - `regex_match`
  - `greater_equal`
  - `non_negative_sum`
  - `row_count_between`
  - `freshness`
  - `relationships`

- **Cross-table reconciliations:**
  - `reconcile_equal`
  - `reconcile_ratio_within`
  - `reconcile_diff_within`
  - `reconcile_coverage`

- **Custom tests (demo):**
  - `min_positive_share` (Python-based)
  - `no_future_orders` (SQL-based)

It uses a simple **customers / orders / mart** setup so you can see exactly what
each test does and how it fails when something goes wrong.

---

## What this example demonstrates

1. **Basic column checks** on staging tables  
   - Enforced via **contracts** (`*.contracts.yml` + `contracts.yml`) and
     project tests:
   - IDs are present / non-null, status values are constrained, numeric ranges
     are respected, physical types match the warehouse.

2. **Freshness** on a timestamp column  
   - Table-level `freshness` test on `orders.order_ts`.
   - Source-level freshness via `sources.yml` for `crm.customers` / `crm.orders`.

3. **Row count sanity checks**  
   - Guard against empty tables and unexpectedly large row counts.

4. **Cross-table reconciliations** between staging and mart  
   - Verify that sums and counts match between `orders` and the aggregated
     `mart_orders_agg`, and that every order has a matching customer.

5. **Tagged tests and selective execution**  
   - All tests are tagged (e.g. `example:dq_demo`, `reconcile`, `fk`,
     `contract`) so you can run exactly the subset you care about.

6. **Contracts-driven tests**  
   - Per-table contracts plus project-wide defaults generate DQ tests
     automatically (including `column_physical_type`).

---

## Project layout

```text
examples/dq_demo/
  .env.dev_bigquery_bigframes
  .env.dev_bigquery_pandas
  .env.dev_databricks
  .env.dev_duckdb
  .env.dev_postgres
  .env.dev_snowflake
  Makefile
  README.md
  contracts.yml
  profiles.yml
  project.yml
  sources.yml

  models/
    README.md
    marts/
      mart_orders_agg.contracts.yml
      mart_orders_agg.ff.sql
    staging/
      customers.contracts.yml
      customers.ff.sql
      orders.contracts.yml
      orders.ff.sql

  seeds/
    README.md
    schema.yml
    seed_customers.csv
    seed_orders.csv

  tests/
    dq/
      min_positive_share.ff.py
      no_future_orders.ff.sql
    unit/
      README.md
````

High level:

* **`.env.dev_*`** — engine-specific environment examples
* **`Makefile`** — convenience wrapper for seeding, running models, DAG HTML and tests
* **`profiles.yml`** — connection profiles for all engines
* **`project.yml`** — central place for **tests** (including reconciliations & custom DQ tests)
* **`contracts.yml`** — project-level **contract defaults**
* **`models/**.contracts.yml`** — per-table contracts
* **`sources.yml`** — source definitions + freshness on raw seeds
* **`seeds/`** — demo CSVs and seed schema
* **`tests/dq/`** — custom DQ tests (Python + SQL)

---

## Seeds

### `seeds/seed_customers.csv`

Simple customer dimension with a creation timestamp:

```csv
customer_id,name,status,created_at
1,Alice,active,2025-01-01T10:00:00
2,Bob,active,2025-01-02T11:00:00
3,Carol,inactive,2025-01-03T12:00:00
```

Columns:

* `customer_id` – integer
* `name` – string
* `status` – string (`active` / `inactive`)
* `created_at` – ISO-8601 timestamp

### `seeds/seed_orders.csv`

Order fact data with per-order timestamps:

```csv
order_id,customer_id,amount,order_ts
100,1,50.00,2025-01-10T09:00:00
101,1,20.00,2025-01-11T09:00:00
102,2,30.00,2025-01-11T10:00:00
103,3,0.00,2025-01-12T10:00:00
```

Columns:

* `order_id` – integer
* `customer_id` – integer
* `amount` – double
* `order_ts` – ISO-8601 timestamp

**One order has `amount = 0.00`** so the custom
`min_positive_share` test has something to complain about.

### Seed schema and sources

`seeds/schema.yml` defines target placement and types:

```yaml
targets:
  seed_customers:
    schema: dq_demo
  seed_orders:
    schema: dq_demo

columns:
  seed_customers:
    customer_id: integer
    name: string
    status: string
    created_at:
      type: timestamp
  seed_orders:
    order_id: integer
    customer_id: integer
    amount: double
    order_ts:
      type: timestamp
```

`sources.yml` exposes them as `crm.customers` and `crm.orders` with **source
freshness**:

```yaml
version: 1

sources:
  - name: crm
    schema: dq_demo
    tables:
      - name: customers
        identifier: seed_customers
        description: "Seeded customers table"
        freshness:
          loaded_at_field: _ff_loaded_at
          warn_after:
            count: 60
            period: minute
          error_after:
            count: 240
            period: minute
      - name: orders
        identifier: seed_orders
        description: "Seeded orders table"
        freshness:
          loaded_at_field: _ff_loaded_at
          warn_after:
            count: 60
            period: minute
          error_after:
            count: 240
            period: minute
```

---

## Models

### 1. Staging: `models/staging/customers.ff.sql`

Materialized as a table; casts IDs and timestamps into proper types and
prepares the customer dimension:

```sql
{{ config(
    materialized='table',
    tags=[
        'example:dq_demo',
        'scope:staging',
        'engine:duckdb',
        'engine:postgres',
        'engine:databricks_spark',
        'engine:bigquery',
        'engine:snowflake_snowpark'
    ],
) }}

-- Staging table for customers
select
  cast(customer_id as int)        as customer_id,
  name,
  status,
  cast(created_at as timestamp)   as created_at
from {{ source('crm', 'customers') }};
```

### 2. Staging: `models/staging/orders.ff.sql`

Materialized as a table; ensures types are suitable for numeric and freshness
checks:

```sql
{{ config(
    materialized='table',
    tags=[
        'example:dq_demo',
        'scope:staging',
        'engine:duckdb',
        'engine:postgres',
        'engine:databricks_spark',
        'engine:bigquery',
        'engine:snowflake_snowpark'
    ],
) }}

-- Staging table for orders with proper types for DQ checks
select
  cast(order_id    as int)        as order_id,
  cast(customer_id as int)        as customer_id,
  cast(amount      as numeric)    as amount,
  cast(order_ts    as timestamp)  as order_ts
from {{ source('crm', 'orders') }};
```

This is important for:

* Numeric checks (`greater_equal`, `non_negative_sum`)
* Timestamp-based `freshness` checks on `order_ts`
* Relationships on `customer_id`

### 3. Mart: `models/marts/mart_orders_agg.ff.sql`

Aggregates orders per customer and prepares data for reconciliation + freshness:

```sql
{{ config(
    materialized='table',
    tags=[
        'example:dq_demo',
        'scope:mart',
        'engine:duckdb',
        'engine:postgres',
        'engine:databricks_spark',
        'engine:bigquery',
        'engine:snowflake_snowpark'
    ],
) }}

-- Aggregate orders per customer for reconciliation & freshness tests
with base as (
  select
    o.order_id,
    o.customer_id,
    -- Ensure numeric and timestamp types for downstream DQ checks
    cast(o.amount   as numeric)    as amount,
    cast(o.order_ts as timestamp) as order_ts,
    c.name   as customer_name,
    c.status as customer_status
  from {{ ref('orders.ff') }} o
  join {{ ref('customers.ff') }} c
    on o.customer_id = c.customer_id
)
select
  customer_id,
  customer_name,
  customer_status as status,
  count(*)        as order_count,
  sum(amount)     as total_amount,
  min(order_ts)   as first_order_ts,
  max(order_ts)   as last_order_ts
from base
group by customer_id, customer_name, customer_status;
```

Key columns:

* `status` → used by contracts (`enum`)
* `order_count` and `total_amount` → used by reconciliation tests
* `first_order_ts` / `last_order_ts` → available for freshness & diagnostics

---

## Contracts in the demo

The demo uses contracts for:

* **Per-table contracts** in `models/**.contracts.yml`
* **Project-wide defaults** in `contracts.yml`

See `docs/Contracts.md` for the full specification; below is how the demo uses
it.

### Project-level defaults: `contracts.yml`

```yaml
version: 1

defaults:
  columns:
    - match:
        name: ".*_id$"
      type: integer
      nullable: false

    - match:
        name: "created_at"
      type: timestamp
      nullable: false

    - match:
        name: ".*_ts$"
      type: timestamp
      nullable: true
      description: "Timestamp-like but allowed to be null in some pipelines"
```

These rules say:

* Any column ending with `_id` is an integer and not nullable.
* Any `created_at` is a non-null timestamp.
* Any `*_ts` column is a (possibly nullable) timestamp with a description.

Defaults are *merged into* per-table contracts, but never override explicit
settings.

### Example: `models/staging/customers.contracts.yml`

```yaml
version: 1
table: customers

columns:
  customer_id:
    type: integer
    nullable: false
    physical:
      duckdb: integer
      postgres: integer
      bigquery: INT64
      snowflake_snowpark: NUMBER
      databricks_spark: INT

  name:
    type: string
    nullable: false
    physical:
      duckdb: VARCHAR
      postgres: text
      bigquery: STRING
      snowflake_snowpark: TEXT
      databricks_spark: STRING

  status:
    type: string
    nullable: false
    enum:
      - active
      - inactive
    physical:
      duckdb: VARCHAR
      postgres: text
      bigquery: STRING
      snowflake_snowpark: TEXT
      databricks_spark: STRING

  created_at:
    type: timestamp
    nullable: false
    physical:
      duckdb: TIMESTAMP
      postgres: timestamp without time zone
      bigquery: TIMESTAMP
      snowflake_snowpark: TIMESTAMP_NTZ
      databricks_spark: TIMESTAMP
```

At runtime, these contracts get turned into tests:

* `column_physical_type` for each column with `physical`
* `not_null` for columns with `nullable: false`
* `accepted_values` for `status` via `enum`
* Plus any inherited defaults from `contracts.yml`

Similarly, the mart has a contract at
`models/marts/mart_orders_agg.contracts.yml` specifying types, nullability,
and enums.

---

## Data quality configuration (`project.yml`)

All **explicit** tests live under `project.yml → tests:`.
Contracts produce additional tests with the tag `contract`.

The demo uses the tag `example:dq_demo` for easy selection.

### Single-table & relationships tests

```yaml
tests:
  # --- Single-table checks ----------------------------------------------------

  - type: row_count_between
    table: customers
    min_rows: 1
    max_rows: 100
    tags: [example:dq_demo, batch]

  - type: greater_equal
    table: orders
    column: amount
    threshold: 0
    tags: [example:dq_demo, batch]

  - type: non_negative_sum
    table: orders
    column: amount
    tags: [example:dq_demo, batch]

  - type: relationships
    table: orders
    column: customer_id
    to: "ref('customers.ff')"
    to_field: customer_id
    tags: [example:dq_demo, fk]

  # Large max_delay_minutes so the example typically passes;
  # adjust down in real projects to enforce freshness SLAs.
  - type: freshness
    table: orders
    column: order_ts
    max_delay_minutes: 100000000
    tags: [example:dq_demo, batch]
```

What these do:

* `row_count_between` — ensure `customers` is not empty and not unexpectedly
  large.
* `greater_equal` / `non_negative_sum` — protect against negative `amount` and
  weird aggregates.
* `relationships` — enforces referential integrity:
  every `orders.customer_id` must exist in `customers.customer_id`.
* `freshness` — checks that the latest `order_ts` is recent enough.

### Reconciliation tests

```yaml
  # --- Reconciliation checks --------------------------------------------------

  - type: reconcile_equal
    name: orders_total_matches_mart
    tags: [example:dq_demo, reconcile]
    left:
      table: orders
      expr: "sum(amount)"
    right:
      table: mart_orders_agg
      expr: "sum(total_amount)"
    abs_tolerance: 0.0

  - type: reconcile_ratio_within
    name: order_counts_match
    tags: [example:dq_demo, reconcile]
    left:
      table: mart_orders_agg
      expr: "sum(order_count)"
    right:
      table: orders
      expr: "count(*)"
    min_ratio: 0.999
    max_ratio: 1.001

  - type: reconcile_diff_within
    name: customers_vs_orders_volume
    tags: [example:dq_demo, reconcile]
    left:
      table: orders
      expr: "count(*)"
    right:
      table: customers
      expr: "count(*)"
    max_abs_diff: 10

  - type: reconcile_coverage
    name: all_orders_have_customers
    tags: [example:dq_demo, reconcile]
    source:
      table: orders
      key: "customer_id"
    target:
      table: customers
      key: "customer_id"
```

These checks ensure:

* Sums match between raw `orders.amount` and `mart_orders_agg.total_amount`.
* The number of rows in `orders` matches the sum of `order_count` in the mart.
* Overall orders vs customers volume stays within a reasonable bound.
* All orders reference an existing customer (coverage).

### Custom tests

```yaml
  # --- Custom tests --------------------------------------------------
  - type: no_future_orders
    table: orders
    column: order_ts
    where: "order_ts is not null"
    tags: [example:dq_demo, batch]

  - type: min_positive_share
    table: orders
    column: amount
    params:
      min_share: 0.75
      where: "amount <> 0"
    tags: [example:dq_demo, batch]
```

* `no_future_orders` — SQL-based test that fails if any order has a timestamp
  in the future.
* `min_positive_share` — Python-based test that requires a minimum share of
  positive values in `amount`.

---

## Custom DQ tests (Python & SQL)

The demo shows how to define **custom data quality tests** that integrate with:

* `project.yml → tests:`
* `fft test`
* The same summary output as built-in tests.

### Python-based test: `min_positive_share`

File: `examples/dq_demo/tests/dq/min_positive_share.ff.py`

```python
# examples/dq_demo/tests/dq/min_positive_share.ff.py
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from fastflowtransform.decorators import dq_test
from fastflowtransform.testing import base as testing


class MinPositiveShareParams(BaseModel):
    """
    Params for the min_positive_share test.

    - min_share: required minimum share of positive values in [0, 1]
    - where: optional WHERE predicate to filter rows
    """

    model_config = ConfigDict(extra="forbid")

    min_share: float = 0.5
    where: str | None = None


@dq_test("min_positive_share", params_model=MinPositiveShareParams)
def min_positive_share(
    executor: Any,
    table: str,
    column: str | None,
    params: dict[str, Any],
) -> tuple[bool, str | None, str | None]:
    """
    Require that at least `min_share` of rows have column > 0.
    """
    if column is None:
        example = f"select count(*) from {table} where <column> > 0"
        return False, "min_positive_share requires a 'column' parameter", example

    min_share: float = params["min_share"]
    where: str | None = params.get("where")

    where_clause = f" where {where}" if where else ""

    total_sql = f"select count(*) from {table}{where_clause}"
    if where:
        pos_sql = f"{total_sql} and {column} > 0"
    else:
        pos_sql = f"select count(*) from {table} where {column} > 0"

    total = testing._scalar(executor, total_sql)
    positives = testing._scalar(executor, pos_sql)

    example_sql = f"{pos_sql};  -- positives\n{total_sql}; -- total"

    if not total:
        return False, f"min_positive_share: table {table} is empty", example_sql

    share = float(positives or 0) / float(total)
    if share < min_share:
        msg = (
            f"min_positive_share failed: positive share {share:.4f} "
            f"< required {min_share:.4f} "
            f"({positives} of {total} rows have {column} > 0"
            + (f" where {where}" if where else "")
            + ")"
        )
        return False, msg, example_sql

    return True, None, example_sql
```

Wiring in `project.yml`:

```yaml
- type: min_positive_share
  table: orders
  column: amount
  params:
    min_share: 0.75
    where: "amount <> 0"
  tags: [example:dq_demo, batch]
```

### SQL-based test: `no_future_orders`

File: `examples/dq_demo/tests/dq/no_future_orders.ff.sql`

```sql
{{ config(
    type="no_future_orders",
    params=["where"]
) }}

-- Custom DQ test: fail if any row has a timestamp in the future.
--
-- Context variables injected by the runner:
--   {{ table }}   : table name (e.g. "orders")
--   {{ column }}  : timestamp column (e.g. "order_ts")
--   {{ where }}   : optional filter (string), from params["where"]
--   {{ params }}  : full params dict (validated), if you ever need it

select count(*) as failures
from {{ table }}
where {{ column }} > current_timestamp
  {%- if where %} and ({{ where }}){%- endif %}
```

And the corresponding entry in `project.yml`:

```yaml
- type: no_future_orders
  table: orders
  column: order_ts
  where: "order_ts is not null"
  tags: [example:dq_demo, batch]
```

At runtime:

* FFT discovers `*.ff.sql` test files under `tests/dq/`.
* `{{ config(...) }}` declares the test `type` and valid `params`.
* `fft test` validates and injects params, then executes the query as a
  “violation count” (`0` = pass, `>0` = fail).

---

## Running the demo

From `examples/dq_demo/`, you can either:

* Use the **Makefile** (recommended), or
* Run `fft` commands manually.

### Using the Makefile

Pick an engine:

```bash
# DuckDB
make demo ENGINE=duckdb

# Postgres
make demo ENGINE=postgres

# Databricks Spark
make demo ENGINE=databricks_spark

# BigQuery (pandas or BigFrames)
make demo ENGINE=bigquery BQ_FRAME=pandas
make demo ENGINE=bigquery BQ_FRAME=bigframes

# Snowflake Snowpark
make demo ENGINE=snowflake_snowpark
```

The `demo` target runs:

1. `fft seed` (load seeds)
2. `fft source freshness`
3. `fft run` (build models)
4. `fft dag` (generate DAG HTML)
5. `fft test` (run DQ tests)
6. Prints locations of artifacts (manifest, run_results, catalog, DAG HTML)

### Running manually (DuckDB example)

From the repo root:

```bash
# 1) Seed
fft seed examples/dq_demo --env dev_duckdb

# 2) Build models
fft run examples/dq_demo --env dev_duckdb

# 3) Run all DQ tests
fft test examples/dq_demo --env dev_duckdb --select tag:example:dq_demo
```

You’ll see a summary of:

* Tests derived from **contracts** (tag: `contract`)
* Explicit tests from `project.yml` (tags: `batch`, `reconcile`, `fk`, …)

You can also run just reconciliations, just FK tests, etc.:

```bash
# Only reconciliation tests
fft test examples/dq_demo --env dev_duckdb --select tag:reconcile

# Only FK-style relationship tests
fft test examples/dq_demo --env dev_duckdb --select tag:fk
```

---

## Things to experiment with

To understand the tests better, intentionally break the data and re-run
`fft test`:

* Set one `customers.customer_id` to `NULL` → `not_null` (from contracts) fails.
* Duplicate a `customer_id` → `unique` (from contracts) fails.
* Put a negative `amount` in `seed_orders.csv` → `greater_equal` and
  `non_negative_sum` fail.
* Change `status` to a value not in the enum → `accepted_values` fails.
* Drop a customer from `customers` or change an ID → `relationships` and
  reconciliation tests complain.
* Change an amount in the mart only → reconciliation tests fail.
* Push an order timestamp into the future → `no_future_orders` fails.
* Change a physical column type in the warehouse to disagree with the
  contract → `column_physical_type` fails.

This makes it very clear what each test guards against.

---

## Summary

The Data Quality Demo is designed to be:

* **Small and readable** – customers, orders, and a single mart.
* **Complete** – exercises:

  * Built-in FFT DQ tests,
  * Tests generated from contracts,
  * Custom Python & SQL tests.
* **Practical** – real-world patterns like:

  * Typing in staging models,
  * Testing freshness on staging tables and sources,
  * Reconciling sums and row counts across tables,
  * Enforcing physical types per engine.

Once you’re comfortable with this example, you can copy the patterns into your
real projects:

1. Start with **contracts** and simple column tests on staging.
2. Add **freshness** on key timestamps and sources.
3. Layer in **reconciliations** across marts and fact tables.
4. Add **custom tests** when built-ins aren’t enough.

> **Tip – Source vs. table freshness**
>
> The demo uses:
>
> * `freshness` tests on tables (`orders.order_ts`), and
> * `freshness` in `sources.yml` (via `_ff_loaded_at`).
>
> Run source freshness with:
>
> ```bash
> fft source freshness examples/dq_demo --env dev_duckdb
> ```
>
> This complements table-level DQ tests by checking whether your inputs are
> recent enough *before* you even build marts.
