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
