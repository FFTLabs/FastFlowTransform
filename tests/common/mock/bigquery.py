# tests/common/mock/bigquery.py
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pandas as pd

# Optional dependency: provide lightweight fallbacks when google libs are absent.
try:
    from google.api_core.exceptions import BadRequest, NotFound  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - when google is not installed

    class BadRequest(Exception):
        pass

    class NotFound(Exception):
        pass

# ---------------------------------------------------------------------------
# Fake types
# ---------------------------------------------------------------------------


class FakeField:
    def __init__(self, name: str, field_type: str = "STRING"):
        self.name = name
        self.field_type = field_type


class FakeDFResult:
    """
    Wird vom pandas-Executor gebraucht: job.result().to_dataframe(...)
    Wir bauen ein minimales pandas-DataFrame daraus.
    """

    def __init__(self, rows: list[tuple] | None = None, schema: list[FakeField] | None = None):
        self._rows = rows or []
        self._schema = schema or []

    def __iter__(self):
        # Allow patterns like: list(job.result())
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def to_dataframe(self, create_bqstorage_client: bool = True):
        if not self._rows:
            return pd.DataFrame([])
        cols = [f.name for f in self._schema] if self._schema else []
        data = [dict(zip(cols, r, strict=False)) for r in self._rows]
        return pd.DataFrame(data)


class FakeJob:
    def __init__(self, rows: list[Any] | None = None, schema: list[FakeField] | None = None):
        self._rows = rows or []
        self._schema = schema or []

    def result(self):
        return FakeDFResult(self._rows, self._schema)

    @property
    def schema(self):
        return self._schema


class FakeQueryJobConfig:
    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs
        # allow executors to set job_config.default_dataset = DatasetReference(...)
        self.default_dataset = None


class FakeDatasetReference:
    def __init__(self, project: str, dataset_id: str):
        self.project = project
        self.dataset_id = dataset_id


class FakeScalarQueryParameter:
    def __init__(self, name: str, typ: str, val: Any):
        self.name = name
        self.type_ = typ
        self.value = val


class FakeDataset:
    def __init__(self, dataset_id: str):
        self.dataset_id = dataset_id
        self.location: str | None = None


class FakeBadRequest(BadRequest):
    """Test helper that behaves like a BadRequest for our wrappers."""

    pass


class FakeNotFound(NotFound):
    """Same idea for NotFound, if you need it."""

    pass


class FakeClient:
    """
    Common Client for both Executor-Tests.
    Can:
      - query(...)
      - list_tables(...)
      - get_table(...)
      - get_dataset(...)
      - create_dataset(...)
      - load_table_from_dataframe(...)  (for pandas-Executor)
    and has:
      - _datasets: set[str]
      - _tables: dict[str, list[Any]]
    """

    def __init__(self, project: str, location: str | None = None):
        self.project = project
        self.location = location
        self.queries: list[tuple[str, str | None, Any | None]] = []
        self._datasets: dict[str, FakeDataset] = {}
        self._tables: dict[str, list[Any]] = {}
        self._relations: set[str] = set()

    # ---- Test helper ----
    def add_dataset(self, ds_id: str) -> None:
        key = ds_id
        if "." not in key:
            key = f"{self.project}.{key}"
        self._datasets.setdefault(key, FakeDataset(key))

    def add_table(self, dataset_id: str, table_id: str) -> None:
        self._tables.setdefault(dataset_id, []).append(SimpleNamespace(table_id=table_id))

    # ---- Emulator methods ----
    def query(self, sql: str, location: str | None = None, job_config: Any | None = None):
        self.queries.append((sql, location, job_config))
        upper_sql = sql.upper()

        # --- INFORMATION_SCHEMA existence checks ---
        if "INFORMATION_SCHEMA.TABLES" in upper_sql or "INFORMATION_SCHEMA.VIEWS" in upper_sql:
            rel_name: str | None = None
            # Extract the @rel parameter from QueryJobConfig, if present
            if job_config is not None and hasattr(job_config, "kwargs"):
                params = job_config.kwargs.get("query_parameters") or []
                for p in params:
                    if getattr(p, "name", None) == "rel":
                        rel_name = getattr(p, "value", None)
                        break

            if rel_name and rel_name in self._relations:
                # Relation exists
                return FakeJob(rows=[(1,)])
            # Relation does NOT exist
            return FakeJob(rows=[])

        # --- CREATE TABLE / CREATE OR REPLACE TABLE ---
        trimmed_upper = upper_sql.lstrip()
        trimmed_orig = sql.lstrip()

        lower_trimmed = trimmed_orig.lower()
        if trimmed_upper.startswith("CREATE TABLE") or trimmed_upper.startswith(
            "CREATE OR REPLACE TABLE"
        ):
            # After "table" comes the identifier: `project.dataset.table` or similar
            after_table = trimmed_orig[lower_trimmed.index("table") + len("table") :].strip()
            if after_table.lower().startswith("if not exists"):
                after_table = after_table[len("if not exists") :].strip()
            # Cut at common delimiters: AS / ( / whitespace-newline
            for sep in [" AS", " (", "\n", "\t"]:
                idx = after_table.find(sep)
                if idx != -1:
                    after_table = after_table[:idx]
                    break
            ident = after_table.strip().strip("`")
            table_name = ident.split(".")[-1]
            self._relations.add(table_name)
            return FakeJob()

        # --- CREATE OR REPLACE VIEW ---
        if trimmed_upper.startswith("CREATE OR REPLACE VIEW"):
            after_view = trimmed_orig[lower_trimmed.index("view") + len("view") :].strip()
            for sep in [" AS", " (", "\n", "\t"]:
                idx = after_view.find(sep)
                if idx != -1:
                    after_view = after_view[:idx]
                    break
            ident = after_view.strip().strip("`")
            view_name = ident.split(".")[-1]
            self._relations.add(view_name)
            return FakeJob()

        # --- Probe-Query (SELECT ... WHERE 1=0) → schema back for alter_table_sync_schema ---
        if "WHERE 1=0" in upper_sql:
            return FakeJob(schema=[FakeField("id"), FakeField("new_col", "INT64")])

        # --- ALTER TABLE ... ADD COLUMN ... ---
        if trimmed_upper.startswith("ALTER TABLE"):
            return FakeJob()

        # Default: generic, empty job
        return FakeJob()

    def list_tables(self, dataset_id: str):
        return self._tables.get(dataset_id, [])

    def get_table(self, table_ref: str):
        if table_ref.endswith(".existing"):
            return SimpleNamespace(schema=[FakeField("id")])

        ds = ".".join(table_ref.split(".")[:2])
        name = table_ref.split(".")[-1]
        for t in self._tables.get(ds, []):
            if t.table_id == name:
                return SimpleNamespace(schema=[FakeField("id")])

        raise FakeNotFound(f"table {table_ref} not found")

    def get_dataset(self, ds_id: str):
        key = ds_id
        if "." not in key:
            key = f"{self.project}.{key}"
        ds = self._datasets.get(key)
        if ds is None:
            raise FakeNotFound(f"dataset {ds_id} not found")
        return ds

    def create_dataset(self, ds_obj: Any, exists_ok: bool | None = None):
        ds_id_raw = getattr(ds_obj, "dataset_id", ds_obj)
        ds_id = ds_id_raw
        if isinstance(ds_id_raw, FakeDatasetReference):
            ds_id = f"{ds_id_raw.project}.{ds_id_raw.dataset_id}"
        if isinstance(ds_id, str) and "." not in ds_id:
            ds_id = f"{self.project}.{ds_id}"

        ds = self._datasets.get(ds_id)
        if ds is None or not exists_ok:
            ds = FakeDataset(ds_id)
        ds.location = getattr(ds_obj, "location", None)
        self._datasets[ds_id] = ds
        return ds

    def load_table_from_dataframe(self, df, table_id: str, job_config: Any, location: str | None):
        return FakeJob()


class FakeWriteDisposition:
    WRITE_TRUNCATE = "WRITE_TRUNCATE"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_fake_bigquery_module() -> types.ModuleType:
    mod = types.ModuleType("google.cloud.bigquery")
    mod.Client = FakeClient  # type: ignore[attr-defined]
    mod.QueryJobConfig = FakeQueryJobConfig  # type: ignore[attr-defined]
    mod.ScalarQueryParameter = FakeScalarQueryParameter  # type: ignore[attr-defined]
    mod.Dataset = FakeDataset  # type: ignore[attr-defined]
    mod.DatasetReference = FakeDatasetReference  # type: ignore[attr-defined]
    mod.BadRequest = FakeBadRequest  # type: ignore[attr-defined]
    mod.NotFound = FakeNotFound  # type: ignore[attr-defined]
    mod.WriteDisposition = FakeWriteDisposition  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# Helper fixture
# ---------------------------------------------------------------------------


def install_fake_bigquery(monkeypatch, target_modules: list[types.ModuleType]) -> types.ModuleType:
    """
    Install the fake BigQuery module into sys.modules and optionally patch
    target modules that expose a top-level ``bigquery`` attribute.

    This ensures that imports like ``from google.cloud import bigquery`` and
    ``import google.cloud.bigquery as bigquery`` see the fake implementation
    during tests.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        target_modules: Modules that may reference a top-level ``bigquery``
            symbol. For each module that actually has such an attribute, it
            will be replaced with the fake BigQuery module.

    Returns:
        The fake BigQuery module that was installed.
    """
    fake_bq = make_fake_bigquery_module()

    # Make the fake visible as google.cloud.bigquery
    gc_mod = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    gc_mod.bigquery = fake_bq  # type: ignore[attr-defined]
    sys.modules["google.cloud.bigquery"] = fake_bq

    # For backwards compatibility, only patch modules that actually expose
    # a top-level `bigquery` attribute. After the executor refactor, not
    # every BigQuery-related module has that symbol anymore.
    for m in target_modules:
        if hasattr(m, "bigquery"):
            monkeypatch.setattr(m, "bigquery", fake_bq, raising=True)

    return fake_bq
