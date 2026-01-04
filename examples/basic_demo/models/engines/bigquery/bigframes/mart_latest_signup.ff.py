from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastflowtransform import engine_model

if TYPE_CHECKING:
    import bigframes.pandas as bpd
    from bigframes.pandas import DataFrame as BFDataFrame
else:
    bpd: Any = None

    class BFDataFrame:  # pragma: no cover - placeholder for runtime type hints
        ...


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


def _get_bigframes() -> Any:
    try:
        import bigframes.pandas as bpd_mod
    except Exception as exc:  # pragma: no cover - optional dep guard
        raise RuntimeError(
            "bigframes is required for this model. Install fastflowtransform[bigquery_bf]."
        ) from exc
    return bpd_mod


@engine_model(
    env_match={
        "FF_ENGINE": "bigquery",
        "FF_ENGINE_VARIANT": "bigframes",
    },
    name="mart_latest_signup",
    materialized="table",
    tags=[
        "example:basic_demo",
        "scope:mart",
        "engine:bigquery",
    ],
    deps=["users_clean.ff"],
    require={"users_clean.ff": ["user_id", "email", "email_domain", "signup_date"]},
)
def build(users_clean: BFDataFrame) -> BFDataFrame:
    _get_bigframes()
    latest = (
        users_clean.sort_values("signup_date", ascending=False)
        .drop_duplicates(subset="email_domain")
        .loc[:, ["email_domain", "user_id", "email", "signup_date"]]
        .rename(
            columns={
                "user_id": "latest_user_id",
                "email": "latest_email",
                "signup_date": "latest_signup_date",
            }
        )  # type: ignore[arg-type]
    )
    return latest
