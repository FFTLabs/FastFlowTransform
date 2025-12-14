from __future__ import annotations

from pathlib import Path

import pytest

from fastflowtransform.core import REGISTRY
from fastflowtransform.dag import topo_sort
from fastflowtransform.executors.duckdb import DuckExecutor
from fastflowtransform.seeding import seed_project

pytestmark = pytest.mark.duckdb  # uses DuckDB


def _w(p: Path, txt: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt, encoding="utf-8")
    return p


@pytest.mark.duckdb
@pytest.mark.integration
def test_ephemeral_inlining_end_to_end(tmp_path: Path):
    proj = tmp_path
    models = proj / "models"
    seeds = proj / "seeds"

    # seeds
    _w(seeds / "seed_users.csv", "id,email\n1,a@example.com\n2,b@example.com\n")
    _w(seeds / "base.csv", "id\n1\n3\n")

    # sources.yml
    _w(
        proj / "sources.yml",
        """version: 1

sources:
  - name: crm
    tables:
      - name: users
        identifier: seed_users
  - name: core
    tables:
      - name: base
        identifier: base
""",
    )

    # models
    # users from source → materialized table
    _w(
        models / "users.ff.sql",
        "create or replace table users as\nselect id, email from {{ source('crm','users') }}",
    )
    # ephemeral model: list of ids from base
    _w(
        models / "ephemeral_ids.ff.sql",
        "{{ config(materialized='ephemeral') }}\nselect id from {{ source('core','base') }}",
    )
    # view referencing users and ephemeral → must inline the ephemeral subquery
    _w(
        models / "v_users.ff.sql",
        "{{ config(materialized='view') }}\n"
        "select u.id from {{ ref('users.ff') }} u "
        "join {{ ref('ephemeral_ids.ff') }} e using(id)",
    )
    # final mart table selecting from the view
    _w(
        models / "mart.ff.sql",
        "create or replace table mart as\nselect * from {{ ref('v_users.ff') }}",
    )

    # Load project (registry, jinja env, deps)
    REGISTRY.load_project(proj)

    # Execute with DuckDB (file DB so we can inspect after)
    ex = DuckExecutor(db_path=":memory:")
    # seed tables into same connection
    seed_project(proj, ex)

    # Run models in topo order
    for name in topo_sort(REGISTRY.nodes):
        node = REGISTRY.nodes[name]
        if node.kind == "sql":
            ex.run_sql(node, REGISTRY.env)  # type: ignore[arg-type]
        else:
            ex.run_python(node)

    # Assertions: 'mart' exists, has expected rows; 'ephemeral' not materialized
    con = ex.con
    # mart should contain only intersection of users ids {1,2} and base ids {1,3} => {1}
    rows = con.execute("select * from mart order by 1").fetchall()
    assert rows == [(1,)]

    # ephemeral relation must NOT exist as a table/view
    existing = [
        r[0]
        for r in con.execute(
            "select table_name from information_schema.tables where table_schema in ('main','temp')"
        ).fetchall()
    ]
    assert "ephemeral_ids" not in existing
