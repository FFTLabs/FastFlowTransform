# fastflowtransform/executors/_sql_identifier.py
from __future__ import annotations

from typing import Any

from fastflowtransform.core import relation_for


class SqlIdentifierMixin:
    """
    Thin helper mixin for engines that qualify SQL identifiers with optional
    catalog/database and schema.

    Subclasses must implement `_quote_identifier` and may override the
    *_default_* / *_should_include_catalog methods to match engine quirks.
    """

    def _normalize_identifier(self, ident: str) -> str:
        """
        Normalize fastflowtransform's logical identifiers:
        - Strip `.ff` suffixes via relation_for
        - Leave other strings untouched
        """
        if not isinstance(ident, str):
            return ident
        return relation_for(ident) if ident.endswith(".ff") else ident

    def _clean_part(self, part: Any) -> str | None:
        if not isinstance(part, str):
            return None
        stripped = part.strip()
        return stripped or None

    def _quote_identifier(self, ident: str) -> str:  # pragma: no cover - abstract
        """Engine-specific quoting (e.g., \"name\" or `name`)."""
        raise NotImplementedError

    def _default_schema(self) -> str | None:
        return self._clean_part(getattr(self, "schema", None))

    def _default_catalog(self) -> str | None:
        return self._clean_part(getattr(self, "catalog", None))

    def _default_catalog_for_source(self, schema: str | None) -> str | None:
        """Hook to adjust catalog fallback for sources (override per engine)."""
        return self._default_catalog()

    def _should_include_catalog(
        self, catalog: str | None, schema: str | None, *, explicit: bool
    ) -> bool:
        """
        Decide whether to emit the catalog in a qualified identifier.

        explicit=True when the caller passed a catalog argument (as opposed to
        using defaults), so engines can honour explicit catalogs even if they
        normally omit them.
        """
        return bool(catalog)

    def _qualify_identifier(
        self,
        ident: str,
        *,
        schema: str | None = None,
        catalog: str | None = None,
        quote: bool = True,
    ) -> str:
        """
        Assemble a qualified identifier (catalog.schema.ident) with engine
        defaults and quoting.
        """
        normalized = self._normalize_identifier(ident)
        explicit_catalog = catalog is not None
        sch = self._clean_part(schema) or self._default_schema()
        cat = self._clean_part(catalog) if explicit_catalog else self._default_catalog()

        parts: list[str] = []
        if self._should_include_catalog(cat, sch, explicit=explicit_catalog) and cat:
            parts.append(cat)
        if sch:
            parts.append(sch)
        parts.append(normalized)

        if not quote:
            return ".".join(parts)
        return ".".join(self._quote_identifier(p) for p in parts)

    # ---- Identifier normalization helpers -----------------------------------
    def _normalize_table_identifier(self, table: str) -> tuple[str | None, str]:
        """
        Normalize a possibly qualified/quoted table identifier into (schema, table).

        - Strip simple quoting (`"`/`` ` ``) from each part.
        - Accept up to 3-part names (catalog.schema.table) and drop the catalog.
        - Return (schema, table) with schema possibly None.
        """
        raw_parts = [p for p in table.split(".") if p]
        parts = [p.strip().strip('`"') for p in raw_parts]

        if len(parts) >= 2:
            return parts[-2] or None, parts[-1]

        table_name = parts[0] if parts else table
        return None, table_name

    def _normalize_column_identifier(self, column: str) -> str:
        """Strip simple quoting from a column identifier."""
        return column.strip().strip('`"')

    # ---- Shared formatting hooks -----------------------------------------
    # def _format_relation_for_ref(self, name: str) -> str:
    #     return self._qualify_identifier(relation_for(name))

    def _pick_schema(self, cfg: dict[str, Any]) -> str | None:
        for key in ("schema", "dataset"):
            candidate = self._clean_part(cfg.get(key))
            if candidate:
                return candidate
        return self._default_schema()

    def _pick_catalog(self, cfg: dict[str, Any], schema: str | None) -> str | None:
        for key in ("catalog", "database", "project"):
            candidate = self._clean_part(cfg.get(key))
            if candidate:
                return candidate
        return self._default_catalog_for_source(schema)

    # ---- Unified formatting entrypoint -----------------------------------
    def _format_identifier(
        self,
        name: str,
        *,
        purpose: str,
        schema: str | None = None,
        catalog: str | None = None,
        quote: bool = True,
        source_cfg: dict[str, Any] | None = None,
        source_name: str | None = None,
        table_name: str | None = None,
    ) -> str:
        """
        Central formatter for all identifier use-cases.

        purpose:
          - "ref" / "this" / "test" / "seed" / "physical": qualify `name`
            using defaults and optional overrides.
          - "source": qualify based on a resolved source config (identifier +
            optional schema/catalog); rejects path-based sources here.
        """
        normalized = self._normalize_identifier(name)

        if purpose == "source":
            cfg = dict(source_cfg or {})
            if cfg.get("location"):
                raise NotImplementedError(
                    f"{getattr(self, 'engine_name', 'unknown')} executor "
                    "does not support path-based sources."
                )

            ident = cfg.get("identifier") or normalized
            if not ident:
                raise KeyError(
                    f"Source {source_name or '<unknown>'}.{table_name or '<unknown>'} "
                    "missing identifier"
                )
            sch = self._clean_part(schema) or self._pick_schema(cfg)
            cat = self._clean_part(catalog) or self._pick_catalog(cfg, sch)
            return self._qualify_identifier(ident, schema=sch, catalog=cat, quote=quote)

        if purpose in {"ref", "this", "test", "seed", "physical"}:
            sch = self._clean_part(schema)
            cat = self._clean_part(catalog)
            return self._qualify_identifier(normalized, schema=sch, catalog=cat, quote=quote)

        raise ValueError(f"Unknown identifier purpose: {purpose!r}")

    # ---- Default delegations using the unified formatter ------------------
    def _format_relation_for_ref(self, name: str) -> str:
        return self._format_identifier(name, purpose="ref")

    def _format_source_reference(
        self, cfg: dict[str, Any], source_name: str, table_name: str
    ) -> str:
        return self._format_identifier(
            cfg.get("identifier") or table_name,
            purpose="source",
            source_cfg=cfg,
            source_name=source_name,
            table_name=table_name,
        )

    def _format_test_table(self, table: str | None) -> str | None:
        table = super()._format_test_table(table)  # type: ignore[misc]
        if not isinstance(table, str):
            return table
        return self._format_identifier(table, purpose="test")

    def _this_identifier(self, node: Any) -> str:
        """
        Default {{ this }} identifier: reuse the formatter with logical name.
        """
        name = getattr(node, "name", node)
        return self._format_identifier(str(name), purpose="this")
