from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastflowtransform import engine_model

if TYPE_CHECKING:
    from snowflake.snowpark import DataFrame
    from snowflake.snowpark import functions as F
    from snowflake.snowpark.window import WindowSpec
else:
    DataFrame = Any
    F = Any
    WindowSpec = Any


__lineage__ = {
    "email_domain": [
        {
            "from_relation": "seed_users",
            "from_column": "email",
            "transformed": True,
            "confidence": "annotated",
        }
    ],
    "latest_signup_date": [
        {"from_relation": "seed_users", "from_column": "signup_date", "confidence": "annotated"}
    ],
}


def _get_snowpark_utils() -> tuple[Any, Any]:
    try:
        from snowflake.snowpark import functions as _F
        from snowflake.snowpark.window import Window as _Window
    except Exception as exc:  # pragma: no cover - optional dep guard
        raise RuntimeError(
            "snowflake-snowpark-python is required for this model. "
            "Install fastflowtransform[snowflake]."
        ) from exc
    return _Window, _F


@engine_model(
    only="snowflake_snowpark",
    name="mart_latest_signup",
    materialized="table",
    tags=[
        "example:basic_demo",
        "scope:mart",
        "engine:snowflake_snowpark",
    ],
    deps=["users_clean.ff"],
    require={"users_clean.ff": ["user_id", "email", "email_domain", "signup_date"]},
)
def build(users_clean: DataFrame) -> DataFrame:
    """Return the latest signup per email domain using Snowpark DataFrame operations."""
    Window, F = _get_snowpark_utils()

    window: WindowSpec = Window.partitionBy(F.col("email_domain")).orderBy(
        F.col("signup_date").desc()
    )

    latest = (
        users_clean.withColumn("row_number", F.row_number().over(window))
        .filter(F.col("row_number") == 1)
        .select(
            F.col("email_domain"),
            F.col("user_id").as_("latest_user_id"),
            F.col("email").as_("latest_email"),
            F.col("signup_date").as_("latest_signup_date"),
        )
    )
    return latest
