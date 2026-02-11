# Auto-Docs Portal — User Guide

This guide explains how to **generate**, **run**, and **use** the FastFlowTransform Auto-Docs portal, plus what you need to define (and where) to unlock features like **lineage**, **coverage**, **contracts**, and **source freshness**.

---

## What you get

Auto-Docs generates a **static** documentation website (a single-page app) from your project:

- **Overview dashboard** (graph + coverage + shortcuts)
- **Model pages** (tabs: Overview / Columns / Lineage / Code / Meta — plus Contract when present)
- **Source pages** (freshness + who uses the source)
- **Macro browser** (search + linkable routes)
- Optional: **run/test health strip** if you publish runtime artifacts

Everything is rendered from a generated JSON file:

- `site/docs/assets/docs_manifest.json` (path depends on `--out`)

---

## Generate the docs site

### One-liner (recommended)

```bash
fft docgen . --env dev --out site/docs --emit-json site/docs/docs_manifest.json
```

This generates the SPA and also writes a manifest you can use for CI checks or custom tooling.

Note: If `artifacts.mode` is set to `db`, docs generation is disabled because it would create local asset files (SPA JS/CSS and JSON). Switch to `files` or `both` to generate docs.

Artifacts configuration lives in your `profiles.yml` under the selected environment. Example:

```yaml
dev_postgres:
  engine: postgres
  postgres:
    dsn: "{{ env('FF_PG_DSN') }}"
    db_schema: "{{ env('FF_PG_SCHEMA', 'public') }}"

  artifacts:
    mode: both          # files | db | both
    engine: postgres    # currently only postgres supported
    postgres:
      dsn: "{{ env('FF_ARTIFACTS_PG_DSN') }}"
      db_schema: "{{ env('FF_ARTIFACTS_PG_SCHEMA', 'public') }}"  # or `schema: ...`
```

Env overrides:
- `FF_ARTIFACTS_MODE` overrides `artifacts.mode`.
- `FF_ARTIFACTS_PG_DSN` overrides `artifacts.postgres.dsn`.
- `FF_ARTIFACTS_PG_SCHEMA` overrides `artifacts.postgres.db_schema` (or `schema`).

Validation rules:
- `mode: files` does not require any Postgres settings.
- `mode: db` or `both` requires `artifacts.engine=postgres` and a valid `dsn` + `db_schema`.

Frontend contract (SPA data sources):
- The SPA reads globals (if present) to resolve artifact URLs:
  - `__FFT_MANIFEST_PATH__`
  - `__FFT_RUN_RESULTS_PATH__`
  - `__FFT_TEST_RESULTS_PATH__`
  - `__FFT_UTEST_RESULTS_PATH__`
  - `__FFT_ARTIFACTS_API_BASE__`
  - `__FFT_ENV__`
  - `__FFT_ENGINE__`
  - `__FFT_RUN_ID__`
- URL resolution order:
  1. Explicit `__FFT_*_PATH__` globals
  2. API base + params (`__FFT_ARTIFACTS_API_BASE__` with env/engine/run_id)
  3. Query params (`?manifest=...&run=...&test=...&utest=...`)
  4. Local assets (`assets/*.json`)
- Expected endpoint shapes (examples):
  - Latest by env/engine:
    - `/artifacts/latest/docs_manifest?env=<env>&engine=<engine>`
    - `/artifacts/latest/run_results?env=<env>&engine=<engine>`
    - `/artifacts/latest/test_results?env=<env>&engine=<engine>`
    - `/artifacts/latest/utest_results?env=<env>&engine=<engine>`
  - Specific run:
    - `/artifacts/run/<run_id>/docs_manifest`
    - `/artifacts/run/<run_id>/run_results`
    - `/artifacts/run/<run_id>/test_results`
    - `/artifacts/run/<run_id>/utest_results`
- Response payload can be either raw JSON or wrapped as `{ payload: {...} }` or `{ data: {...} }`.

### Classic (DAG-only)

```bash
fft dag . --env dev --html
```

### Open automatically in your browser

```bash
fft docgen . --env dev --out site/docs --open-source
```

---

## Start the local “dev server” (to view the SPA)

The generated portal is static HTML/JS that loads JSON via `fetch()`. Many browsers block `fetch()` from `file://`, so it’s best to serve it locally.

From the output directory:

```bash
cd site/docs
python -m http.server 8000
```

Then open:

- `http://localhost:8000`

> Any static server works; `python -m http.server` is just the simplest default.

### Typical dev loop

1. Run `fft docgen ...` to regenerate docs after changes.
2. Refresh the browser tab (or hard refresh if assets are cached).

---

## Where to put documentation (quick map)

| What you want to document | Where to define it | Notes |
|---|---|---|
| Model description | `project.yml` → `docs.models.<model>.description` | Plain text |
| Column descriptions | `project.yml` → `docs.models.<model>.columns.<col>` | Plain text |
| Rich model writeup | `docs/models/<model>.md` | Overrides YAML description |
| Rich column writeup | `docs/columns/<relation>/<column>.md` | Overrides YAML column description for that relation |
| Column lineage hints | `project.yml` → `docs.models.<model>.lineage` | See examples below |
| Contract (expected schema) | `project.yml` → `docs.models.<model>.contract` or front matter in `docs/models/<model>.md` | Enables Contract tab/badges |
| Source freshness | `sources.yml` → `freshness:` block | Shows on source pages |

**Priority (when multiple exist):**
1. Markdown overrides
2. YAML docs (`project.yml`)
3. Empty

---

## How to use the portal

### Global search (keyboard-first)

- Press `/` (or your UI’s shortcut, often **Ctrl+K**) to focus global search
- Type to search across:
  - model name, relation, descriptions, columns
  - sources
  - macros
- Use **↑/↓** to move selection
- Press **Enter** to navigate
- Press **Esc** to clear/close

### Sidebar navigation + persistent state

The sidebar is organized into collapsible sections (typically **Models / Sources / Macros**).

The portal remembers your UI state in the browser:
- collapsed sections
- last visited page
- your current search/filter text

So when you reopen the docs, you land where you left off.

---

## Model pages (what you’ll find)

Model pages are structured into tabs so you can scan quickly and then drill in.

### Overview tab

Typical content:
- model description
- upstream dependencies (**depends on**) and downstream consumers (**used by**)
- docs coverage badges (model described, columns documented)
- a mini upstream/downstream panel (graph or list)

### Columns tab

A “docs-first” column table designed to make gaps obvious:

- sort by column name / dtype / nullability / documented state
- quick filter
- highlight **undocumented** columns
- show dtype + nullability clearly
- expand a row to view the full description and lineage details

> If schema introspection is enabled for your engine, dtype/nullability can be shown automatically. If it’s not available, you’ll still see any descriptions you provided in YAML/Markdown.

### Lineage tab

Shows where columns come from (best effort), based on what you define (see [Defining lineage](#defining-lineage)) plus any lineage the generator can infer.

You’ll typically see:
- upstream relation + upstream column
- a “transformed” flag where appropriate
- a confidence indicator (e.g., annotated vs inferred), if your build emits it

### Code tab

Depending on model type:
- **SQL models**: raw SQL, and optionally rendered/compiled SQL (if enabled)
- **Python models**: source, signature, docstring (if extracted)

### Meta tab

A structured panel with:
- relation (database/schema/identifier)
- materialization
- file path
- tags/owners/domains (if present in your metadata)
- custom meta/config

---

## Source pages

A source page typically includes:
- description
- relation
- which models consume it
- freshness configuration, when present (loaded_at + warn/error thresholds)
- a clear status badge: **configured** vs **missing freshness**

---

## Macro browser

The macros section provides:
- searchable list of macros
- path/kind shown (so you can find the source)
- linkable macro detail routes (if enabled)

---

## Overview dashboard

The landing page is designed as a “project dashboard,” not just a DAG:

- interactive DAG navigation
- coverage summary (“how documented are we?”)
- shortcuts like:
  - undocumented models
  - high-impact models (high fan-out)
  - python models
- optionally: “newest/changed” models if your generator can surface change metadata

---

## Defining lineage

Lineage only appears if you provide lineage metadata (and/or if your build infers it). The simplest supported approach is to define lineage in `project.yml` under the model.

### Minimal lineage example (YAML)

```yaml
docs:
  models:
    users_enriched:
      description: "Adds gmail flag."
      columns:
        is_gmail: "True if email ends with @gmail.com"

      lineage:
        is_gmail:
          from: [{ table: users, column: email }]
          transformed: true
```

What this does in the UI:
- On `users_enriched`, column `is_gmail` will show lineage pointing to `users.email`
- `transformed: true` tells the UI this column is derived (not a direct passthrough)

### Lineage for multiple upstream inputs

```yaml
docs:
  models:
    orders_enriched:
      lineage:
        user_segment:
          from:
            - { table: users, column: segment }
            - { table: crm_users, column: lifecycle_stage }
          transformed: true
```

### Tips for lineage that “works well” in the portal

- Use upstream identifiers that match how the portal labels relations (table/model names you use consistently).
- Prefer concrete upstream columns rather than “whole table” lineage.
- Mark `transformed: true` for derived columns (casts, concatenations, bucketing, joins, etc.).

> If lineage is missing for a column, the portal will still display the column normally—lineage is additive and should never block docs generation.

---

## Documenting models and columns

### YAML: fast and lightweight

`project.yml`:

```yaml
docs:
  models:
    users_enriched:
      description: "Downstream-ready users dimension."
      columns:
        id: "Primary key."
        email: "Normalized email address."
```

### Markdown: richer writeups

Model Markdown:

- `docs/models/users_enriched.md`

Column Markdown:

- `docs/columns/analytics.users_enriched/email.md` (example relation folder)

Use Markdown when you want:
- longer explanations
- usage notes
- examples
- “gotchas” for downstream consumers

---

## Contracts (expected schema)

If you define a contract for a model, the portal can show:
- **Contracted** badge
- a dedicated **Contract** section listing expected columns/types/nullability/constraints
- optional contract-vs-warehouse comparison (drift detection) when schema info is available

### Contract example (YAML)

```yaml
docs:
  models:
    users_enriched:
      contract:
        enforced: true
        columns:
          id:    { dtype: int,  nullable: false, constraints: [unique] }
          email: { dtype: text, nullable: false }
```

### Contract drift (when schema is available)

If your build includes discovered schema (dtype/nullability), the UI can show a status badge like:
- Verified
- Drift detected
- Schema unavailable

---

## Filters, deep links, and sharing

### Faceted filtering

You can filter models by:
- kind (sql/python)
- materialized
- folder/path prefix
- tags and owners (if emitted)

### Shareable links with state

The portal can encode state in the URL, so sharing a link preserves:
- active filters
- selected graph node
- search text
- selected tab

Use the portal’s **Copy link** action (if present) to grab an exact “deep link” to your current view.

---

## Coverage and “what’s missing?”

Coverage indicators help you answer:
- Do we have a model description?
- How many columns have descriptions?

Typical UI elements:
- “Model described” badge
- “X/Y columns documented”
- landing page shortcuts for:
  - undocumented models
  - models with lots of undocumented columns

---

## Optional operational strip (health)

If your build outputs runtime artifacts (run results, tests), the portal can display:
- last run status + duration
- test summaries and failures
- optional unit test results

This is intended to be lightweight and opt-in: docs remain usable without it.
In `artifacts.mode=db`, runtime artifacts are stored in Postgres and the docs site is not generated, so the health strip is unavailable.

---

## FAQ / troubleshooting

### I opened `index.html` directly and the page is blank

Serve the folder instead of using `file://`:

```bash
cd site/docs
python -m http.server 8000
```

### My Columns tab is empty or missing types/nullability

Schema display depends on engine support and whether schema introspection is enabled in your build. If you don’t have schema introspection for your engine, you can still:
- document columns in YAML/Markdown
- provide lineage in YAML

### Lineage isn’t showing

Check:
- you added `docs.models.<model>.lineage` in `project.yml`
- the column name under `lineage:` matches the output column name you want to annotate
- you regenerated docs (`fft docgen ...`)

---

## Appendix: recommended minimum documentation standard

If you want a simple policy that yields a strong portal experience:

1. Every model has a one-sentence description.
2. Every model documents at least its “public interface” columns.
3. High-impact models add lineage for the top 5–20 columns consumers rely on.
4. Critical models define contracts (even if not enforced yet).
5. Sources define freshness thresholds where operationally relevant.
