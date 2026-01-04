import pandas as pd

from fastflowtransform import engine_model


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


@engine_model(
    only="duckdb",
    name="mart_latest_signup",
    materialized="table",
    tags=[
        "example:basic_demo",
        "scope:mart",
        "engine:duckdb",
    ],
    deps=["users_clean.ff"],
    require={"users_clean.ff": ["user_id", "email", "email_domain", "signup_date"]},
)
def build(users_clean: pd.DataFrame) -> pd.DataFrame:
    """Return the latest signup per email domain using pandas (DuckDB)."""
    latest = (
        users_clean.sort_values("signup_date", ascending=False)
        .drop_duplicates("email_domain")
        .loc[:, ["email_domain", "user_id", "email", "signup_date"]]
        .rename(
            columns={
                "user_id": "latest_user_id",
                "email": "latest_email",
                "signup_date": "latest_signup_date",
            }
        )
        .reset_index(drop=True)
    )
    return latest
