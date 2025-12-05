# fastflowtransform/errors.py

from __future__ import annotations

from collections.abc import Iterable


class FastFlowTransformError(Exception):
    """
    Base class for all FastFlowTransform errors.

    Attributes:
        message: Human-readable error message.
        code: Optional short error code (e.g., 'CFG001', 'DAG002').
        hint: Optional human hint with remediation steps.
    """

    def __init__(self, message: str, *, code: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n\nHint:\n{self.hint}"
        return self.message


class DependencyNotFoundError(FastFlowTransformError):
    """Raised when a model depends on another model that does not exist."""

    def __init__(self, missing_map: dict[str, list[str]]):
        parts = []
        for depender, deps in missing_map.items():
            parts.append(f"{depender} → missing: {', '.join(sorted(deps))}")

        msg = (
            "❌ Missing model dependency.\n"
            + "\n".join(parts)
            + (
                "\n\nHints:\n"
                "• Check file names under models/ "
                "(node name = file stem, e.g. users.ff.sql → 'users.ff').\n"
                "• Ensure ref('…') matches the exact node name.\n"
                "• If it's a Python model, set @model(name='…')."
            )
        )

        super().__init__(msg)
        self.missing_map = missing_map


class ModelCycleError(FastFlowTransformError):
    """
    Raised when a cycle is detected in the model DAG.

    Args:
        affected_nodes: Nodes that couldn't be ordered due to the cycle.
    """

    def __init__(self, affected_nodes: Iterable[str]):
        affected = sorted(set(affected_nodes))
        msg = "Cycle detected in DAG. Affected models: " + ", ".join(affected)
        hint = (
            "Check for circular refs in your models:\n"
            "• Ensure A does not ref B while B (directly or indirectly) refs A.\n"
            "• Break the cycle by removing or refactoring one dependency.\n"
            "• If a ref is conditional in SQL, ensure the parse phase still sees the correct deps."
        )
        super().__init__(msg, code="DAG_CYCLE", hint=hint)
        self.affected_nodes = affected


class ModuleLoadError(FastFlowTransformError):
    """Raised when a Python model module cannot be loaded."""

    pass


class ModelConfigError(FastFlowTransformError):
    """
    Raised when a model's {{ config(...) }} (or @model(meta=...)) is malformed
    or fails schema validation.

    Typical causes:
      - Syntax errors in the config(...) header
      - Non-literal expressions in values (must be JSON/Python literals)
      - Unknown/forbidden keys
      - Wrong types for documented fields
    """

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        field: str | None = None,
        hint: str | None = None,
        code: str = "CFG_PARSE",
    ):
        prefix = f"{path}: " if path else ""
        scope = f"config.{field}: " if field else "config: "
        super().__init__(f"{prefix}{scope}{message}".rstrip(), code=code, hint=hint)
        self.path = path
        self.field = field


class ProfileConfigError(FastFlowTransformError):
    """Profile/configuration error with a short, actionable hint."""

    def __init__(self, message: str):
        # keep to a single line for CLI readability
        super().__init__(message.replace("\n", " ").strip())


class ContractsConfigError(FastFlowTransformError):
    """
    Raised when a contracts.yml (project-level or per-table) is malformed.
    """

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        hint: str | None = None,
        code: str = "CONTRACTS_PARSE",
    ):
        prefix = f"{path}: " if path else ""
        super().__init__(prefix + message, code=code, hint=hint)
        self.path = path


class ModelExecutionError(Exception):
    """Raised when a model fails to execute/render on the engine.
    Carries friendly context for CLI formatting.
    """

    def __init__(
        self,
        node_name: str,
        relation: str,
        message: str,
        sql_snippet: str | None = None,
    ):
        self.node_name = node_name
        self.relation = relation
        self.sql_snippet = sql_snippet
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        """
        Control how this error is rendered in the CLI.

        The run CLI uses traceback.format_exception_only(type(e), e),
        which calls str(e), so this is the single place we need to adjust.
        """
        base = self.message

        # prepend relation if we have it
        if self.relation:
            base = f"{self.relation}: {base}"

        if self.sql_snippet:
            return f"{base}\n\n[SQL]\n{self.sql_snippet}"

        return base
