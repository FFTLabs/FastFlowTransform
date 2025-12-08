# Quickstart

This guide walks you through creating a minimal FastFlowTransform project from scratch and running it end-to-end.

## 0. Create a skeleton (optional)

Start with a minimal project structure:

```bash
fft init demo_project --engine duckdb
```

The command is non-interactive, refuses to overwrite existing directories, and leaves inline comments that point back to the relevant docs (`Project_Config.md`, `Profiles.md`, etc.). Populate the generated files before running the steps below.

## 1. Install & bootstrap

```bash
python3 -m venv .venv
. .venv/bin/activate  # or source .venv/bin/activate
pip install --upgrade pip
pip install fastflowtransform
fft --help
```

Choose extras if you target other engines (combine as needed):

```bash
# Postgres
pip install "fastflowtransform[postgres]"

# BigQuery (pandas) or BigFrames
pip install "fastflowtransform[bigquery]"     # pandas
pip install "fastflowtransform[bigquery_bf]"  # BigFrames

# Databricks/Spark + Delta
pip install "fastflowtransform[spark]"

# Snowflake Snowpark
pip install "fastflowtransform[snowflake]"

# Everything
pip install "fastflowtransform[full]"
```

## 2. Create project layout

```bash
mkdir -p demo/{models,seeds}
cat <<'YAML' > demo/sources.yml
version: 1

sources:
  - name: raw
    schema: staging
    tables:
      - name: users
        identifier: seed_users
YAML

cat <<'CSV' > demo/seeds/seed_users.csv
id,email
1,a@example.com
2,b@example.com
CSV

cat <<'SQL' > demo/models/users.ff.sql
{{ config(materialized='table') }}
select id, email
from {{ source('raw', 'users') }}
SQL

cat <<'YAML' > demo/profiles.yml
dev:
  engine: duckdb
  duckdb:
    path: ".local/demo.duckdb"
YAML
```

## 3. Seed static inputs

```bash
fft seed demo --env dev
```

This materializes the CSV into the configured engine (DuckDB by default) using `seed_users` as the physical table.

## 4. Run the pipeline

```bash
fft run demo --env dev --cache off
```

You should see log lines similar to `✓ L01 [DUCK] users.ff`. The resulting table lives in the target schema (`staging` in this example).

## 5. Inspect artifacts

- `.fastflowtransform/target/manifest.json` → model graph + sources
- `.fastflowtransform/target/run_results.json` → run outcomes and durations

## 6. Add more models (optional)

- Reference other models with `{{ ref('model_name') }}`
- Configure tags or materializations via `{{ config(...) }}` at the top of each SQL file

## 7. Next steps

- Add `project.yml` for reusable `vars:` and metadata
- Use engine profiles under `profiles.yml` to target Postgres, BigQuery, or Databricks (path-based sources supported via `format` + `location` overrides)
- Render the DAG site for this project: `fft dag demo --env dev --html` (find it under `demo/site/dag/index.html`)

Refer to `docs/Config_and_Macros.md` for advanced configuration options.
