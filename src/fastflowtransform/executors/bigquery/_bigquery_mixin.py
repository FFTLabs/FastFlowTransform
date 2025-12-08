# fastflowtransform/executors/_bigquery_mixin.py
from __future__ import annotations

from fastflowtransform.executors._sql_identifier import SqlIdentifierMixin
from fastflowtransform.typing import NotFound, bigquery


class BigQueryIdentifierMixin(SqlIdentifierMixin):
    """
    Mixin that provides common BigQuery helpers (identifier quoting, dataset creation).
    Expect subclasses to define: self.project, self.dataset, self.client.
    """

    project: str
    dataset: str
    client: bigquery.Client

    def _bq_quote(self, value: str) -> str:
        return value.replace("`", "\\`")

    def _quote_identifier(self, ident: str) -> str:
        return self._bq_quote(ident)

    def _default_schema(self) -> str | None:
        return self.dataset

    def _default_catalog(self) -> str | None:
        return self.project

    def _should_include_catalog(
        self, catalog: str | None, schema: str | None, *, explicit: bool
    ) -> bool:
        # BigQuery always expects a project + dataset.
        return True

    def _qualify_identifier(
        self,
        ident: str,
        *,
        schema: str | None = None,
        catalog: str | None = None,
        quote: bool = True,
    ) -> str:
        proj = self._clean_part(catalog) or self._default_catalog()
        dset = self._clean_part(schema) or self._default_schema()
        normalized = self._normalize_identifier(ident)
        parts = [proj, dset, normalized]
        if not quote:
            return ".".join(p for p in parts if p)
        return f"`{'.'.join(self._bq_quote(p) for p in parts if p)}`"

    def _qualified_identifier(
        self, relation: str, project: str | None = None, dataset: str | None = None
    ) -> str:
        return self._qualify_identifier(relation, schema=dataset, catalog=project)

    def _qualified_api_identifier(
        self, relation: str, project: str | None = None, dataset: str | None = None
    ) -> str:
        """
        Build an API-safe identifier (project.dataset.table) without backticks.
        """
        return self._qualify_identifier(
            relation,
            schema=dataset,
            catalog=project,
            quote=False,
        )

    def _ensure_dataset(self) -> None:
        ds_id = f"{self.project}.{self.dataset}"
        try:
            self.client.get_dataset(ds_id)
            return
        except NotFound:
            if not getattr(self, "allow_create_dataset", False):
                raise

        ds_obj = bigquery.Dataset(ds_id)
        if getattr(self, "location", None):
            ds_obj.location = self.location  # type: ignore[attr-defined]
        self.client.create_dataset(ds_obj, exists_ok=True)
