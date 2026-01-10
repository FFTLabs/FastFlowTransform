from fastflowtransform import engine_model
import pandas as pd


@engine_model(
    only="duckdb",
    name="fct_events_py_incremental",
    deps=["events_base.ff"],
)
def build(events_df: pd.DataFrame) -> pd.DataFrame:
    df = events_df.copy()
    df["value_x10"] = df["value"] * 10
    return df[["event_id", "updated_at", "value", "value_x10"]]
