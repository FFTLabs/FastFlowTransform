# 🧭 FastFlowTransform – Technical Developer Documentation

> Status: latest updates from your context dump. This document consolidates project structure, architecture, core APIs, error handling, CLI, examples, and roadmap into a print/git-friendly Markdown.
>
> Looking for an overview? Start at the [`docs/index.md`](./index.md) hub, then dive back here when you need details.
>
> Project: **FastFlowTransform** — SQL & Python Data Modeling (Batch + Streaming), DAG, CLI, Auto-Docs, DQ Tests.

---

## Docs Navigation
1. [Getting Started](./index.md)
2. **User Guide** — see [Part I – Operational Guide](#part-i-operational-guide) (this document)
3. [Modeling Reference](./Config_and_Macros.md)
4. **Developer Guide** — see [Part II – Architecture & Internals](#part-ii-architecture-internals) (this document)

---

## Table of Contents

- [Docs Navigation](#docs-navigation)
- [Part I – Operational Guide](#part-i-operational-guide)
  - [Project Layout](#project-layout)
  - [Example Projects and Seeds](#example-projects-and-seeds)
  - [CLI Flows](#cli-flows)
  - [Logging & Verbosity](#logging-verbosity)
  - [Model Unit Tests (`fft utest`)](#model-unit-tests-fft-utest)
  - [Troubleshooting](#troubleshooting)
  - [Profiles & Environment Overrides](#profiles-environment-overrides)
  - [Parallel Execution and Cache](#parallel-execution-and-cache)
  - [Roadmap Snapshot](#roadmap-snapshot)
  - [Cross-Table Reconciliations](#cross-table-reconciliations)
  - [Auto-Docs and Lineage](#auto-docs-and-lineage)
- [Part II – Architecture & Internals](#part-ii-architecture-internals)
  - [Architecture Overview](#architecture-overview)
  - [Core Modules](#core-modules)
    - [`core.py`](#corepy)
    - [`dag.py`](#dagpy)
    - [`errors.py`](#errorspy)
    - [Executors](#executors)
    - [`validation.py`](#validationpy)
    - [`testing.py`](#testingpy)
    - [`docs.py` & Templates](#docspy-templates)
    - [`seeding.py`](#seedingpy)
  - [CLI Implementation](#cli-implementation)
  - [Settings Infrastructure](#settings-infrastructure)
  - [Streaming Components](#streaming-components)
  - [Mini End-to-End Example (Python API)](#mini-end-to-end-example-python-api)

---

## Part I – Operational Guide

### Project Layout

The repository is structured so you can jump straight to the area you need without spelunking:

```
fastflowtransform/
├── src/fastflowtransform/        # core package (CLI, executors, docs, streaming, settings)
│   ├── api/, cli/, executors/, testing/, templates/ and friends
│   └── entry-points such as core.py, dag.py, seeding.py, validation.py
├── docs/                         # user + developer docs (this file plus guides)
├── examples/                     # canonical SQL/Python demo projects with seeds + READMEs
├── examples_article/             # long-form tutorial artifacts referenced by articles
├── articles/, tickets/, _exports/ # content pipelines + planning notes
├── tests/                        # pytest coverage for internal modules
├── _scripts/, rel_dir/, dist/, site/, htmlcov/  # tooling + build outputs
├── pyproject.toml, uv.lock       # build system + dependency lock
└── Makefile*, docker-compose.yml # dev workflow shortcuts and services
```

Treat `fastflowtransform/` as the project root when running commands from this doc.

### Example Projects and Seeds

Need runnable references? Start with the curated demos under `docs/examples/`:

- [Basic Demo](./examples/Basic_Demo.md) shows the minimum viable project (seeds, staging, marts) plus Makefile targets you can copy.
- [API Demo](./examples/API_Demo.md) focuses on HTTP-powered Python models.
- [Environment Matrix](./examples/Environment_Matrix.md) demonstrates multiple profiles talking to different engines.

Each demo includes deterministic seeds (`seeds/*.csv`), schema YAML, and Makefile shortcuts, so the detailed CSV listings and commands here would be redundant. Follow the demo docs (or the [Quickstart](./Quickstart.md)) for the full walkthrough.

### CLI Flows

Looking for command recipes, selection filters, or sync workflows? See the dedicated [CLI Guide](./CLI_Guide.md) for a task-by-task breakdown (seed/run/dag/docgen/test/utest/sync-db-comments) plus links to API-model helpers.

### Logging & Verbosity

Need the exact behaviour of `-q/-v/-vv`, SQL debug output, or the parallel log queue? Head over to [Logging.md](./Logging.md) for the full matrix plus usage snippets.

### Model Unit Tests (`fft utest`)

The full how-to (cache modes, YAML DSL, CI snippets) moved to [Unit_Tests.md](./Unit_Tests.md). Keep this Section in mind whenever you need fast feedback on SQL/Python models without executing the entire DAG.

### Troubleshooting

Common fixes (engines, docs generation, tests) plus the exit-code matrix live in [Troubleshooting.md](./Troubleshooting.md). Skim that doc whenever you hit connectivity issues or need to decode return codes.

### Profiles & Environment Overrides
Need to understand profile precedence, `.env` layering, or the Pydantic models that back settings? Jump to the [Profiles guide](./Profiles.md) which covers file layout, environment helpers, validation, and selection precedence in depth.

### Parallel Execution and Cache

Level-wise parallelism, cache modes, fingerprint formula, and the `_ff_meta` audit table are documented in [Cache_and_Parallelism.md](./Cache_and_Parallelism.md). Use that reference for CLI examples (`--jobs`, `--cache`, `--rebuild`), skip conditions, and troubleshooting tips related to concurrency.

### Roadmap Snapshot

Lightweight glance at near-term priorities:

- **Docs & UX:** tighten CLI help/autocomplete, add more cookbook-style recipes.
- **Engines:** polish Databricks/Spark parity (Unity Catalog, Delta) and Snowpark SQL coverage.
- **Reliability:** concurrency/caching hardening and better error surfacing in auto-docs.
- **DX:** richer typing for Python models plus template improvements for examples/macros.

### Cross-Table Reconciliations

Reconciliation tests (`reconcile_equal`, `reconcile_ratio_within`, `reconcile_diff_within`, `reconcile_coverage`) are fully documented in the [Data Quality Test Reference](./Data_Quality_Tests.md#cross-table-reconciliations). Use that guide for YAML schemas, tolerance parameters, and engine notes before wiring the checks into `fft test`.

### Auto-Docs and Lineage

Rendering the DAG site, feeding project descriptions/lineage, and exporting JSON manifests are covered in [Auto_Docs.md](./Auto_Docs.md). Head there for command flags, markdown/YAML resolution, and lineage overrides.

## Part II – Architecture & Internals

### Architecture Overview

```
CLI (Typer)
│
├── Registry (core.py)
│   ├── Discover models (*.ff.sql / *.ff.py)
│   ├── Load Python models (decorator)
│   ├── Parse/validate dependencies
│   └── Jinja environment + sources.yml
│
├── DAG (dag.py)
│   ├── topo_sort (Kahn, deterministic)
│   └── mermaid() (styled + stable IDs)
│
├── Executors (executors/*)
│   ├── BaseExecutor (SQL rendering, dependency loading, materialization, requires guard)
│   ├── DuckExecutor (DuckDB)
│   ├── PostgresExecutor (SQLAlchemy)
│   ├── BigQueryExecutor (pandas)
│   ├── BigQueryBFExecutor (BigQuery DataFrames / bigframes)
│   ├── DatabricksSparkExecutor (PySpark, without pandas)
│   └── SnowflakeSnowparkExecutor (Snowpark, without pandas)
│
├── Testing (testing.py)
│   ├── generic _exec / _scalar
│   └── Checks: not_null, unique, row_count_between, greater_equal, non_negative_sum, freshness
│
├── Seeding (seeding.py)
│   └── Load seeds (CSV/Parquet/SQL) → engine agnostic
│
├── Docs (docs.py + templates/)
│   ├── Mermaid + overview table (index.html)
│   └── Model detail pages (model.html)
│
├── Settings/Profiles (settings.py)
│   └── Pydantic v2 discriminated union + ENV overrides
│
└── Streaming (streaming/*)
    ├── FileTailSource
    └── StreamSessionizer
```

---

### Core Modules

#### `core.py`

Key data structures and the project loading process.

```python
@dataclass
class Node:
    name: str                # logical name (stem or @model(name=...))
    kind: str                # "sql" | "python"
    path: Path
    deps: List[str] = field(default_factory=list)

class Registry:
    def load_project(self, project_dir: Path) -> None: ...
    def _register_node(self, node: Node) -> None: ...
    def _load_py_module(self, path: Path) -> types.ModuleType: ...
    def _scan_sql_deps(self, path: Path) -> List[str]: ...
```

**Helpers & decorator:**

```python
def relation_for(node_name: str) -> str: ...
def ref(name: str) -> str: ...
def source(source_name: str, table_name: str) -> str: ...

def model(name=None, deps=None, requires=None) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...
```

**Python models (example):**

```python
@model(name="users_enriched", deps=["users.ff"], requires={"users": {"id","email"}})
def enrich(df: pd.DataFrame) -> pd.DataFrame: ...
```

---

#### `dag.py`

Deterministic topological sort plus Mermaid export.

```python
def topo_sort(nodes: Dict[str, Node]) -> List[str]: ...
def mermaid(nodes: Dict[str, Node]) -> str: ...
```

---

#### `errors.py`

Primary error types with helpful messages.

```python
class FastFlowTransformError(Exception): ...
class ModuleLoadError(FastFlowTransformError): ...
class DependencyNotFoundError(FastFlowTransformError): ...
class ModelCycleError(FastFlowTransformError): ...
class TestFailureError(FastFlowTransformError): ...
```

---

#### Executors

Shared logic (`BaseExecutor`) plus engine implementations.

```python
class BaseExecutor(ABC):
    def render_sql(self, node: Node, env: Environment, ref_resolver=None, source_resolver=None) -> str: ...
    def run_python(self, node: Node) -> None: ...
    @abstractmethod
    def _read_relation(self, relation: str, node: Node, deps: Iterable[str]) -> pd.DataFrame: ...
    @abstractmethod
    def _materialize_relation(self, relation: str, df: pd.DataFrame, node: Node) -> None: ...
```

**DuckDB (`duckdb.py`)**

- `run_sql(node, env)` renders Jinja (`ref/source`) and executes the SQL.
- `_read_relation` loads a table as `DataFrame`; surfaces actionable errors when a dependency is missing.
- `_materialize_relation` writes the `DataFrame` as a table (`create or replace table ...`).

**Postgres (`postgres.py`)**

- `run_sql` renders SQL and rewrites `CREATE OR REPLACE TABLE` to `DROP + CREATE AS`.
- `_read_relation` uses pandas, handles schemas, and provides clear guidance.
- `_materialize_relation` writes via `to_sql(if_exists="replace")`.

**BigQuery / BigQuery DataFrames / Spark / Snowpark**

- Identical signatures; IO uses the respective native dataframes (no pandas for Spark/Snowpark).

---

#### `validation.py`

Required-column checks for Python models (single and multi dependency).

```python
class RequiredColumnsError(ValueError): ...
def validate_required_columns(node_name: str, inputs: Any, requires: dict[str, set[str]]): ...
```

---

#### `testing.py`

Minimal data quality framework (engine agnostic via `_exec`).

**Checks:** `not_null`, `unique`, `greater_equal`, `non_negative_sum`, `row_count_between`, `freshness`

```python
class TestFailure(Exception): ...
def _exec(con: Any, sql: Any): ...
def _scalar(con: Any, sql: Any): ...
```

---

#### `docs.py` & Templates

- `render_site(out_dir, nodes)` produces `index.html` plus `model.html` per model.
- Templates (`docs/templates/`) include dark mode, filters, copy buttons, legend.
- Uses `dag.mermaid(nodes)` for the graph.

---

#### `seeding.py`

Engine-agnostic seed loading (CSV/Parquet/SQL).

```python
def seed_project(project_dir: Path, executor, schema: Optional[str] = None) -> int: ...
```

---

### CLI Implementation

Operational usage lives in [CLI Flows](#cli-flows) and the dedicated [CLI Guide](CLI_Guide.md). For implementation details, see the Typer commands in `src/fastflowtransform/cli/`.

---

### Settings Infrastructure

`settings.py` uses a **Pydantic v2 discriminated union** (`engine` as discriminator) plus ENV overrides.

Profile types:
- `DuckDBProfile(engine="duckdb", duckdb: {path})`
- `PostgresProfile(engine="postgres", postgres: {dsn, db_schema})`
- `BigQueryProfile(engine="bigquery", bigquery: {project?, dataset, location?, use_bigframes?})`
- `DatabricksSparkProfile(engine="databricks_spark", ...)`
- `SnowflakeSnowparkProfile(engine="snowflake_snowpark", ...)`

Resolver idea:

```python
def resolve_profile(project_dir: Path, env_name: str, env: EnvSettings) -> Profile: ...
```

---

### Streaming Components

**`streaming/sessionizer.py`**

- Normalizes events (JSONL / batch DF) and writes `fct_sessions_streaming`.
- `process_batch(df)` aggregates sessions (start/end, pageviews, revenue).

**Smoke test (DuckDB):**

```python
def test_stream_sessionizer_produces_sessions(): ...
```

---

### Mini End-to-End Example (Python API)

```python
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from fastflowtransform.core import REGISTRY
from fastflowtransform.dag import topo_sort
from fastflowtransform.executors.duckdb import DuckExecutor

proj = Path("examples/simple_duckdb").resolve()
REGISTRY.load_project(proj)
env = REGISTRY.env  # Jinja env from the registry load

order = topo_sort(REGISTRY.nodes)
ex = DuckExecutor(db_path=str(proj / ".local" / "demo.duckdb"))

for name in order:
    node = REGISTRY.nodes[name]
    if node.kind == "sql":
        ex.run_sql(node, env)
    else:
        ex.run_python(node)

print("✓ Done")
```

---

Need a different angle? Head back to the [Docs Hub](./index.md) or deep-dive into the [Modeling Reference](./Config_and_Macros.md).
