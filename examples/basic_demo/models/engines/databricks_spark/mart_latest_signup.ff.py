from typing import TYPE_CHECKING, Any

from fastflowtransform import engine_model

if TYPE_CHECKING:
    from pyspark.sql import DataFrame
    from pyspark.sql import WindowSpec
    from pyspark.sql import functions as F
else:
    DataFrame = Any
    WindowSpec = Any
    F = Any


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


def _get_spark_utils() -> tuple[Any, Any]:
    try:
        from pyspark.sql import Window as _Window
        from pyspark.sql import functions as _F
    except Exception as exc:  # pragma: no cover - optional dep guard
        raise RuntimeError(
            "pyspark is required for this model. Install fastflowtransform[spark]."
        ) from exc
    return _Window, _F


@engine_model(
    only="databricks_spark",
    name="mart_latest_signup",
    materialized="table",
    tags=[
        "example:basic_demo",
        "scope:mart",
        "engine:databricks_spark",
    ],
    deps=["users_clean.ff"],
    require={"users_clean.ff": ["user_id", "email", "email_domain", "signup_date"]},
)
def build(users_clean: DataFrame) -> DataFrame:
    """Return the latest signup per email domain using PySpark DataFrame operations."""
    Window, F = _get_spark_utils()
    window = Window.partitionBy("email_domain").orderBy(F.col("signup_date").desc())

    latest = (
        users_clean.withColumn("row_number", F.row_number().over(window))
        .filter(F.col("row_number") == 1)
        .select(
            F.col("email_domain"),
            F.col("user_id").alias("latest_user_id"),
            F.col("email").alias("latest_email"),
            F.col("signup_date").alias("latest_signup_date"),
        )
    )
    return latest
