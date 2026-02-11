from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastflowtransform.executors.postgres import PostgresExecutor
from fastflowtransform.logging import warn


def _json_dumps(v: Any) -> str:
    # Be tolerant: artifacts should be JSON-serializable, but keep it robust.
    return json.dumps(v, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class PostgresArtifactsConfig:
    dsn: str
    db_schema: str
    mode: str = "files"  # files|db|both


class PostgresArtifactsStore:
    """
    Stores artifacts in Postgres in a schema isolated from model execution.

    - raw JSONB: ff_artifacts_raw
    - exploded "records": ff_artifacts_records (still JSONB, but row-wise)
    """

    def __init__(self, *, dsn: str, schema: str) -> None:
        self._ex = PostgresExecutor(dsn=dsn, schema=schema)
        self._schema = schema
        self._tables_ready = False

    @property
    def schema(self) -> str:
        return self._schema

    def ensure_tables(self) -> None:
        if self._tables_ready:
            return

        # DDL is "maintenance": no budget/stat recording.
        self._ex._execute_sql_maintenance(
            """
            CREATE TABLE IF NOT EXISTS ff_artifacts_runs (
              run_id       text PRIMARY KEY,
              inserted_at  timestamptz NOT NULL DEFAULT now(),
              env_name     text NULL,
              model_engine text NULL,
              meta         jsonb NOT NULL DEFAULT '{}'::jsonb
            );
            """
        )

        self._ex._execute_sql_maintenance(
            """
            CREATE TABLE IF NOT EXISTS ff_artifacts_raw (
              run_id       text NOT NULL,
              artifact_type text NOT NULL,
              inserted_at  timestamptz NOT NULL DEFAULT now(),
              payload      jsonb NOT NULL,
              PRIMARY KEY (run_id, artifact_type)
            );
            """
        )

        self._ex._execute_sql_maintenance(
            """
            CREATE TABLE IF NOT EXISTS ff_artifacts_records (
              run_id        text NOT NULL,
              artifact_type text NOT NULL,
              record_type   text NOT NULL,
              record_id     text NOT NULL,
              inserted_at   timestamptz NOT NULL DEFAULT now(),
              payload       jsonb NOT NULL,
              PRIMARY KEY (run_id, artifact_type, record_type, record_id)
            );
            """
        )

        self._ex._execute_sql_maintenance(
            "CREATE INDEX IF NOT EXISTS idx_ff_artifacts_records_run "
            + "ON ff_artifacts_records (run_id);"
        )
        self._ex._execute_sql_maintenance(
            "CREATE INDEX IF NOT EXISTS idx_ff_artifacts_records_type "
            + "ON ff_artifacts_records (artifact_type, record_type);"
        )
        self._ex._execute_sql_maintenance(
            "CREATE INDEX IF NOT EXISTS idx_ff_artifacts_records_id "
            + "ON ff_artifacts_records (record_id);"
        )

        self._tables_ready = True

    def new_run_id(self) -> str:
        return uuid.uuid4().hex

    def upsert_run(
        self,
        *,
        run_id: str,
        env_name: str | None,
        model_engine: str | None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.ensure_tables()
        payload = meta or {}
        sql = """
        INSERT INTO ff_artifacts_runs (run_id, env_name, model_engine, meta)
        VALUES (:run_id, :env_name, :model_engine, CAST(:meta AS jsonb))
        ON CONFLICT (run_id) DO UPDATE
        SET env_name = EXCLUDED.env_name,
            model_engine = EXCLUDED.model_engine,
            meta = EXCLUDED.meta
        """
        self._ex._execute_sql_maintenance(
            sql,
            {
                "run_id": run_id,
                "env_name": env_name,
                "model_engine": model_engine,
                "meta": _json_dumps(payload),
            },
        )

    def write_artifact(
        self,
        *,
        run_id: str,
        artifact_type: str,
        payload: Any,
        explode_records: bool = True,
    ) -> None:
        self.ensure_tables()

        # 1) raw
        raw_sql = """
        INSERT INTO ff_artifacts_raw (run_id, artifact_type, payload)
        VALUES (:run_id, :artifact_type, CAST(:payload AS jsonb))
        ON CONFLICT (run_id, artifact_type) DO UPDATE
        SET payload = EXCLUDED.payload,
            inserted_at = now()
        """
        self._ex._execute_sql_maintenance(
            raw_sql,
            {
                "run_id": run_id,
                "artifact_type": artifact_type,
                "payload": _json_dumps(payload),
            },
        )

        # 2) exploded records
        if not explode_records:
            return

        rows = list(self._explode_to_records(artifact_type, payload))
        if not rows:
            return

        rec_sql = """
        INSERT INTO ff_artifacts_records (run_id, artifact_type, record_type, record_id, payload)
        VALUES (:run_id, :artifact_type, :record_type, :record_id, CAST(:payload AS jsonb))
        ON CONFLICT (run_id, artifact_type, record_type, record_id) DO UPDATE
        SET payload = EXCLUDED.payload,
            inserted_at = now()
        """
        for record_type, record_id, rec_payload in rows:
            self._ex._execute_sql_maintenance(
                rec_sql,
                {
                    "run_id": run_id,
                    "artifact_type": artifact_type,
                    "record_type": record_type,
                    "record_id": record_id,
                    "payload": _json_dumps(rec_payload),
                },
            )

    def _explode_to_records(
        self, artifact_type: str, payload: Any
    ) -> Iterable[tuple[str, str, Any]]:
        """
        Convert "big JSON" artifacts into row-wise JSONB records.
        This is intentionally schema-light (no guessing your artifact schema),
        but still gives you proper relational tables for querying.

        Heuristics:
          - dict with 'results': list -> ('result', <unique-ish id>, item)
          - dict with common dict blocks (nodes/sources/macros/...) -> (singular, key, value)
          - list -> ('item', str(idx), item)
        """
        if isinstance(payload, dict):
            # 1) results list (common pattern: run/test results)
            results = payload.get("results")
            if isinstance(results, list):
                for i, item in enumerate(results):
                    rid = None
                    if isinstance(item, dict):
                        rid = (
                            item.get("unique_id")
                            or item.get("node_id")
                            or item.get("name")
                            or item.get("node")
                        )
                    yield ("result", str(rid) if rid else str(i), item)

            # 2) common dict blocks (manifest-like)
            for key in (
                "nodes",
                "sources",
                "macros",
                "metrics",
                "exposures",
                "selectors",
                "parent_map",
                "child_map",
            ):
                block = payload.get(key)
                if isinstance(block, dict):
                    rtype = key[:-1] if key.endswith("s") else key
                    for k, v in block.items():
                        yield (rtype, str(k), v)

            return

        if isinstance(payload, list):
            for i, item in enumerate(payload):
                yield ("item", str(i), item)

    # -------- safe wrapper (warn + continue policy) --------

    def safe_call(self, fn_name: str, fn: Callable, *args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            warn(f"[artifacts-db] {fn_name} failed (schema={self.schema}): {exc}")

    # -------- read helpers --------

    def get_latest_run_id(self, env_name: str | None, model_engine: str | None) -> str | None:
        """
        Return the latest run_id for a given env_name + model_engine.
        """
        self.ensure_tables()
        clauses: list[str] = []
        params: dict[str, Any] = {}

        if env_name is None:
            clauses.append("env_name IS NULL")
        else:
            clauses.append("env_name = :env_name")
            params["env_name"] = env_name

        if model_engine is None:
            clauses.append("model_engine IS NULL")
        else:
            clauses.append("model_engine = :model_engine")
            params["model_engine"] = model_engine

        where_sql = " AND ".join(clauses) if clauses else "1=1"
        sql = f"""
        SELECT run_id
        FROM ff_artifacts_runs
        WHERE {where_sql}
        ORDER BY inserted_at DESC
        LIMIT 1
        """
        row = self._ex._execute_sql_maintenance(sql, params).fetchone()
        return str(row[0]) if row and row[0] else None

    def get_artifact(self, run_id: str, artifact_type: str) -> Any | None:
        """
        Return the raw JSON payload for a run_id + artifact_type, or None.
        """
        self.ensure_tables()
        sql = """
        SELECT payload
        FROM ff_artifacts_raw
        WHERE run_id = :run_id AND artifact_type = :artifact_type
        LIMIT 1
        """
        row = self._ex._execute_sql_maintenance(
            sql, {"run_id": run_id, "artifact_type": artifact_type}
        ).fetchone()
        return row[0] if row else None

    def get_latest_artifact(
        self, artifact_type: str, env_name: str | None, model_engine: str | None
    ) -> Any | None:
        """
        Return the raw JSON payload for the latest run (env_name + engine scoped).
        """
        run_id = self.get_latest_run_id(env_name, model_engine)
        if not run_id:
            return None
        return self.get_artifact(run_id, artifact_type)


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()
