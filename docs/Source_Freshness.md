# Source Freshness

Source freshness checks answer a simple question:

> “How old is the latest data in this source, and is that acceptable?”

They complement table-level DQ tests by validating **recency of inputs** (seeds, raw tables,
landing zones) *before* you build marts.

- Configuration lives alongside your `sources.yml` metadata.
- Evaluation is done via the `fft source-freshness` CLI command.
- Output is CI-friendly (non-zero exit when critical freshness rules fail).

---

## When to use source freshness

Use source freshness when:

- you rely on upstream ingestion jobs (ETL, CDC, streaming) and need a guard-rail like
  “`crm.orders` must be < 60 minutes old”;
- you have critical feeds (payments, auth logs, PII) where stale data is dangerous;
- you want a cheap pre-flight check in CI before running a heavier `fft run` + `fft test`.

It is *not* a replacement for table-level `freshness` tests on marts – they work nicely together.

---

## Configuration

Freshness rules are attached to source tables in your metadata (conceptually alongside `sources.yml`).

A minimal example:

```yaml
version: 1
sources:
  - name: crm
    schema: raw
    tables:
      - name: orders
        identifier: seed_orders
        freshness:
          loaded_at_field: "_ff_loaded_at"
          max_delay_minutes: 1440       # 1 day
          warn_after_minutes: 720       # optional: warning threshold
          error_after_minutes: 1440     # optional: hard error threshold
        tags: ["example:dq_demo", "critical_source"]
````

Key fields:

* `loaded_at_field`: timestamp column used to compute the **max** loaded time. When seeds are
  materialized via `fft seed`, every table automatically includes `_ff_loaded_at` (UTC timestamp
  captured during the seed run). Pointing freshness rules at this metadata column keeps demo seeds
  “fresh” even if the CSV contains static business timestamps.
* `max_delay_minutes` / `warn_after_minutes` / `error_after_minutes`:

  * if only `max_delay_minutes` is set, it is treated as an error threshold;
  * `warn_after_minutes` and `error_after_minutes` allow a 3-state result:

    * ✅ **on-time** (age ≤ `warn_after_minutes`)
    * ❕ **late (warning)** (`warn_after_minutes` < age ≤ `error_after_minutes`)
    * ❌ **stale (error)** (age > `error_after_minutes`)

The exact field names should mirror whatever you wired into `run_source_freshness`; adjust the snippet if your structure differs.

---

## Running checks

Basic usage:

```bash
fft source freshness <project> --env <env>
```

Examples:

```bash
# Check all sources in the DQ demo (DuckDB)
fft source freshness examples/dq_demo --env dev_duckdb

# Only check sources tagged "critical_source"
fft source freshness . \
  --env dev \
  --select tag:critical_source

# Combine with other selectors (depends on your implementation)
fft source freshness . \
  --env dev \
  --select source:crm --exclude tag:experimental
```

The command:

* connects using the selected profile (`--env`);
* loads source + freshness metadata;
* executes a `max(loaded_at_column)` query per configured source;
* compares the result to your thresholds and produces:

  * per-source rows (age, thresholds, status),
  * an overall exit status (`0` if all within thresholds, non-zero on error).

---

## CI / automation

Typical pattern in CI:

```bash
# 1) Check source recency
fft source freshness . --env ci

# 2) Only if sources are fresh, run the pipeline and DQ tests
fft run  . --env ci
fft test . --env ci --select tag:ci
```

Because `fft source freshness` exits non-zero on stale inputs, you can simply let the
CI job fail early rather than running a full DAG on obviously outdated data.

---

## Troubleshooting

**“No freshness rules found”**

* You called `fft source-freshness` but nothing was evaluated.
* Check that:

  * at least one source table has a `freshness:` block;
  * your `--select` / `--exclude` patterns aren’t filtering everything out.

**“Column not found”**

* The `loaded_at_column` doesn’t exist in the physical source.
* Verify the column name and that your `identifier` / schema overrides for that source are correct.

**Unexpectedly large ages**

* Make sure your warehouse and timestamps are in the expected timezone.
* Confirm that the ingestion job actually updates `loaded_at_column` (and not some other field).

---

## Relationship to table-level freshness tests

Table-level `freshness` tests in `project.yml`:

* operate on **models** (e.g. `mart_orders_agg.last_order_ts`);
* run via `fft test`.

Source freshness:

* operates on **sources** (e.g. `crm.orders.order_ts`);
* runs via `fft source freshness`.

Using both lets you catch:

1. Stale upstream ingestion (source is old),
2. And downstream pipeline lag or bugs (mart not refreshed even though source is fresh).
