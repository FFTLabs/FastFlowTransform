# tests/unit/docs/test_docs_unit.py
from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import TemplateNotFound

import fastflowtransform.docs as docs_mod
from fastflowtransform.core import Node

# ---------------------------------------------------------------------------
# Helper fakes
# ---------------------------------------------------------------------------


class _FakeTemplate:
    def __init__(self, content: str):
        self._content = content

    def render(self, **ctx: Any) -> str:
        # handle exactly the two patterns we need in the tests
        if "{m.name}" in self._content and "m" in ctx:
            m = ctx["m"]
            return f"MODEL {getattr(m, 'name', 'UNKNOWN')}"
        if "{mermaid_src}" in self._content and "mermaid_src" in ctx:
            return f"INDEX {ctx['mermaid_src']}"
        # fallback: just dump keys
        return self._content


class _FakeEnv:
    def __init__(self):
        # name -> template
        self._tmpls: dict[str, _FakeTemplate] = {
            "index.html.j2": _FakeTemplate("INDEX {mermaid_src}"),
            "model.html.j2": _FakeTemplate("MODEL {m.name}"),
        }

    def get_template(self, name: str) -> _FakeTemplate:
        try:
            return self._tmpls[name]
        except KeyError as exc:
            raise TemplateNotFound(name) from exc


# ---------------------------------------------------------------------------
# Unit tests for small helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_safe_filename_keeps_dots_and_slashes_as_underscore():
    assert docs_mod._safe_filename("abc.sql") == "abc.sql"
    assert docs_mod._safe_filename("a b") == "a_b"
    # exotic chars
    assert docs_mod._safe_filename("äöü") == "___"
    # empty → fallback
    assert docs_mod._safe_filename("") == "_model"


@pytest.mark.unit
def test_render_minimarkdown_inline_code_and_links():
    md = textwrap.dedent(
        """
        Some text with `code` and a [link](https://example.com).

        Second paragraph.
        """
    ).strip()
    html = docs_mod._render_minimarkdown(md)
    assert "<code>code</code>" in html
    assert '<a href="https://example.com"' in html
    # 2 paragraphs
    assert "<p>Some text" in html
    assert "<p>Second paragraph." in html


@pytest.mark.unit
def test_strip_html_removes_tags_and_collapses_ws():
    txt = "<p>Hello <b>World</b></p>\n  <span> X </span>"
    assert docs_mod._strip_html(txt) == "Hello World X"


@pytest.mark.unit
def test_read_markdown_file_with_front_matter(tmp_path: Path):
    p = tmp_path / "doc.md"
    p.write_text(
        "---\ntitle: Hello\n---\nBody text\n",
        encoding="utf-8",
    )
    fm, body = docs_mod._read_markdown_file(p)
    assert fm == {"title": "Hello"}
    assert body.strip() == "Body text"


@pytest.mark.unit
def test_read_markdown_file_no_front_matter(tmp_path: Path):
    p = tmp_path / "doc.md"
    p.write_text("Plain body", encoding="utf-8")
    fm, body = docs_mod._read_markdown_file(p)
    assert fm == {}
    assert body == "Plain body"


@pytest.mark.unit
def test_reverse_deps_builds_inverse_graph():
    nodes = {
        "a": SimpleNamespace(name="a", deps=["b", "c"]),
        "b": SimpleNamespace(name="b", deps=["c"]),
        "c": SimpleNamespace(name="c", deps=[]),
    }
    rev = docs_mod._reverse_deps(nodes)  # type: ignore[arg-type]
    # c is used by a and b
    assert rev["c"] == ["a", "b"]
    # b is used by a
    assert rev["b"] == ["a"]
    # a is not used by anyone
    assert rev["a"] == []


@pytest.mark.unit
def test_materialization_legend_has_incremental():
    legend = docs_mod._materialization_legend()
    assert "incremental" in legend
    assert legend["incremental"]["label"] == "incremental"


@pytest.mark.unit
def test_scan_source_refs(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)
    sql_path = models_dir / "model_a.sql"
    sql_path.write_text("select * from {{ source('crm', 'customers') }}", encoding="utf-8")

    nodes = {"model_a": Node(name="model_a", kind="sql", path=sql_path, deps=[], meta={})}

    by_source, by_model = docs_mod._scan_source_refs(nodes)

    assert by_source[("crm", "customers")] == ["model_a"]
    assert by_model["model_a"] == [("crm", "customers")]


# ---------------------------------------------------------------------------
# read_docs_metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_docs_metadata_merges_yaml_and_markdown(tmp_path: Path):
    # project.yml with docs
    (tmp_path / "project.yml").write_text(
        textwrap.dedent(
            """
            docs:
              models:
                my_model:
                  description: "YAML desc"
                  columns:
                    col1: "YAML col1"
                  lineage:
                    col1:
                      from:
                        - table: src.t
                          column: x
                      transformed: true
            """
        ),
        encoding="utf-8",
    )

    # Markdown model override (should win over YAML description)
    md_dir = tmp_path / "docs" / "models"
    md_dir.mkdir(parents=True)
    (md_dir / "my_model.md").write_text("Markdown desc", encoding="utf-8")

    # Markdown column override
    col_dir = tmp_path / "docs" / "columns" / "my_model"
    col_dir.mkdir(parents=True)
    (col_dir / "col1.md").write_text("MD col1", encoding="utf-8")

    meta = docs_mod.read_docs_metadata(tmp_path)

    # model present
    assert "my_model" in meta["models"]
    # description_html from MD wins
    assert "Markdown desc" in meta["models"]["my_model"]["description_html"]
    # YAML lineage preserved
    assert "lineage" in meta["models"]["my_model"]
    # column override present
    assert meta["columns"]["my_model"]["col1"].startswith("<p>MD col1")


# ---------------------------------------------------------------------------
# _apply_descriptions_to_models
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_descriptions_to_models_applies_short_and_column_desc():
    models = [
        docs_mod.ModelDoc(
            name="m1",
            kind="sql",
            path="models/m1.sql",
            relation="db.sc.m1",
            deps=[],
            materialized="table",
        )
    ]
    docs_meta = {
        "models": {
            "m1": {
                "description_html": "<p>Hello world</p>",
                "columns": {"col1": "<p>Col 1</p>"},
            }
        },
        "columns": {
            "db.sc.m1": {
                "col2": "<p>Col 2</p>",
            }
        },
    }
    cols_by_table = {
        "db.sc.m1": [
            docs_mod.ColumnInfo("col1", "STRING", True),
            docs_mod.ColumnInfo("col2", "INT", False),
        ]
    }

    docs_mod._apply_descriptions_to_models(models, docs_meta, cols_by_table, with_schema=True)

    assert models[0].description_html == "<p>Hello world</p>"
    assert models[0].description_short == "Hello world"
    # column 1 desc from model-level
    assert cols_by_table["db.sc.m1"][0].description_html == "<p>Col 1</p>"
    # column 2 desc from relation-level
    assert cols_by_table["db.sc.m1"][1].description_html == "<p>Col 2</p>"


@pytest.mark.unit
def test_collect_columns_uses_executor_hook_when_available():
    expected = {"tbl": [docs_mod.ColumnInfo("c1", "INT", True)]}

    class FakeExecutor:
        def collect_docs_columns(self):
            return expected

    cols = docs_mod._collect_columns(FakeExecutor())
    assert cols is expected


@pytest.mark.unit
def test_collect_columns_swallows_errors_and_unknown():
    class BoomExecutor:
        def collect_docs_columns(self):
            raise RuntimeError("boom")

    assert docs_mod._collect_columns(BoomExecutor()) == {}
    assert docs_mod._collect_columns(object()) == {}


@pytest.mark.unit
def test_build_macro_list_collects_and_sorts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange
    project_dir = tmp_path
    macros_dir = project_dir / "macros"
    macros_dir.mkdir(parents=True, exist_ok=True)

    m1 = macros_dir / "util_dates.sql"
    m1.write_text("-- sql macro 1", encoding="utf-8")

    m2 = macros_dir / "cleanup.sql"
    m2.write_text("-- sql macro 2", encoding="utf-8")

    m3 = macros_dir / "py_macro.py"
    m3.write_text("def run(): ...", encoding="utf-8")

    fake_registry = SimpleNamespace(
        macros={
            "util_dates": m1,
            "cleanup": m2,
            "py_macro": m3,
        }
    )
    monkeypatch.setattr(docs_mod, "REGISTRY", fake_registry, raising=True)

    # Act
    res = docs_mod._build_macro_list(project_dir)

    # Assert
    names = [x["name"] for x in res]
    assert names == ["py_macro", "cleanup", "util_dates"]

    paths = {x["name"]: x["path"] for x in res}
    assert paths["cleanup"] == "macros/cleanup.sql"
    assert paths["util_dates"] == "macros/util_dates.sql"
    assert paths["py_macro"] == "macros/py_macro.py"

    kinds = {x["name"]: x["kind"] for x in res}
    assert kinds["py_macro"] == "python"
    assert kinds["cleanup"] == "sql"
    assert kinds["util_dates"] == "sql"


@pytest.mark.unit
def test_infer_and_attach_lineage_sql_branch_is_used(monkeypatch: pytest.MonkeyPatch):
    m = docs_mod.ModelDoc(
        name="model_sql",
        kind="sql",
        path="models/model_sql.sql",
        relation="project.dataset.model_sql",
        deps=["src_table"],
        materialized="table",
    )
    models = [m]

    cols_by_table = {
        "project.dataset.model_sql": [
            docs_mod.ColumnInfo("id", "INT", True),
            docs_mod.ColumnInfo("name", "STRING", True),
        ]
    }

    class FakeExecutor:
        def render_sql(self, node, jenv, ref_resolver=None, source_resolver=None):
            return "select id, name from src_table"

        def _resolve_ref(self, name, env):
            return f"resolved_ref_{name}"

        def _resolve_source(self, source, table):
            return f"resolved_source_{source}_{table}"

    fake_executor = FakeExecutor()

    fake_node = SimpleNamespace(
        name="model_sql",
        kind="sql",
        path=Path("models/model_sql.sql"),
        deps=["src_table"],
        meta={},
    )
    fake_registry = SimpleNamespace(
        nodes={"model_sql": fake_node},
        env=SimpleNamespace(),
    )
    monkeypatch.setattr(docs_mod, "REGISTRY", fake_registry, raising=True)

    monkeypatch.setattr(
        docs_mod,
        "infer_sql_lineage",
        lambda rendered: {"id": [{"from_relation": "src_table", "from_column": "id"}]},
        raising=True,
    )
    monkeypatch.setattr(
        docs_mod,
        "parse_sql_lineage_overrides",
        lambda rendered: {},
        raising=True,
    )
    monkeypatch.setattr(
        docs_mod,
        "merge_lineage",
        lambda base, overrides: base,
        raising=True,
    )
    docs_meta: dict[str, object] = {}

    # ACT
    docs_mod._infer_and_attach_lineage(
        models,
        fake_executor,
        docs_meta,
        cols_by_table,
        with_schema=True,
    )

    # ASSERT
    col_id = cols_by_table["project.dataset.model_sql"][0]
    assert col_id.name == "id"
    assert col_id.lineage == [{"from_relation": "src_table", "from_column": "id"}]


@pytest.mark.unit
def test_infer_and_attach_lineage_yaml_override_branch_is_used(monkeypatch: pytest.MonkeyPatch):
    m = docs_mod.ModelDoc(
        name="model_yml",
        kind="sql",
        path="models/model_yml.sql",
        relation="project.dataset.model_yml",
        deps=[],
        materialized="table",
    )
    models = [m]

    cols_by_table = {
        "project.dataset.model_yml": [
            docs_mod.ColumnInfo("total", "NUMBER", True),
            docs_mod.ColumnInfo("cnt", "NUMBER", True),
        ]
    }

    docs_meta = {
        "models": {
            "model_yml": {
                "description_html": None,
                "columns": {},
                "lineage": {
                    "total": {
                        "from": [
                            {"table": "project.dataset.orders", "column": "amount"},
                        ],
                        "transformed": True,
                    }
                },
            }
        },
        "columns": {},
    }

    monkeypatch.setattr(docs_mod, "infer_sql_lineage", lambda *_a, **_k: {}, raising=True)
    monkeypatch.setattr(docs_mod, "parse_sql_lineage_overrides", lambda *_a, **_k: {}, raising=True)
    monkeypatch.setattr(
        docs_mod,
        "merge_lineage",
        lambda base, overrides: (overrides or base),
        raising=True,
    )

    # ACT
    docs_mod._infer_and_attach_lineage(
        models,
        executor=None,
        docs_meta=docs_meta,
        cols_by_table=cols_by_table,
        with_schema=True,
    )

    # ASSERT
    col_total = cols_by_table["project.dataset.model_yml"][0]
    assert col_total.name == "total"
    assert col_total.lineage == [
        {
            "from_relation": "project.dataset.orders",
            "from_column": "amount",
            "transformed": True,
        }
    ]
