# fastflowtransform/lineage.py
from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from typing import Any

import sqlparse
from sqlparse.sql import (
    Function,
    Identifier,
    IdentifierList,
    Parenthesis,
    Statement,
    Token,
    TokenList,
)
from sqlparse.tokens import DML, Keyword, Name, Newline, Whitespace

LineageItem = dict[str, Any]
LineageMap = dict[str, list[LineageItem]]
Projection = Token | TokenList


# ---------------------------
# Public API
# ---------------------------


def infer_sql_lineage(rendered_sql: str, ref_map: dict[str, str] | None = None) -> LineageMap:
    """
    Infer column-level lineage for SQL:
      - CTE-aware (WITH ... AS (...))
      - tracks simple transforms (lower/cast/trim/upper/etc.)
      - expands CTE edges to base relations
      - does NOT emit placeholder unknown edges; if ambiguous/unresolved -> no edge
    """
    sql = (rendered_sql or "").strip()
    if not sql:
        return {}

    sql = _strip_to_query(sql)
    stmts = sqlparse.parse(sql)
    if not stmts:
        return {}

    # Use last statement (CREATE VIEW ...; SELECT ...; etc.)
    stmt = stmts[-1]

    # If statement still isn't query-like (e.g. CREATE ... AS SELECT ...),
    # strip again with token-based method and reparse.
    if not _contains_select(stmt):
        sql2 = _strip_to_query(str(stmt))
        stmts2 = sqlparse.parse(sql2)
        if not stmts2:
            return {}
        stmt = stmts2[-1]

    ctes, main_stmt = _split_ctes(stmt)

    # infer CTEs in order (CTEs can reference earlier CTEs)
    cte_lineage: dict[str, LineageMap] = {}
    for name, cte_sql in ctes:
        cte_map = infer_sql_lineage(cte_sql, ref_map=ref_map)
        # Expand references to already known CTEs inside this CTE (chained CTEs)
        cte_map = _expand_cte_edges(cte_map, cte_lineage)
        cte_lineage[name] = cte_map

    # infer main statement and expand through CTEs
    out = _infer_select_stmt(main_stmt, ref_map=ref_map)
    out = _expand_cte_edges(out, cte_lineage)
    return out


def parse_sql_lineage_overrides(rendered_sql: str) -> LineageMap:
    """
    Parse inline overrides from SQL comments.

    Supported:
      -- lineage: out_col <- relation.column
      -- lineage: out_col <- relation.column xform
      -- lineage: out_col <- relation.column, other_rel.other_col
      /* lineage:
            out_col <- relation.column xform
            other   <- rel.col
         */

    Also supports JSON:
      -- lineage-json: {"out_col":[{"from_relation":"t","from_column":"c","transformed":true}]}
      /* lineage-json: {...} */
    """
    sql = rendered_sql or ""
    if not sql.strip():
        return {}

    overrides: LineageMap = {}

    # JSON override blocks first (if present)
    for payload in _extract_comment_payloads(sql, keys=("lineage-json", "fft-lineage-json")):
        try:
            obj = json.loads(payload)
            if isinstance(obj, dict):
                parsed = _normalize_lineage_map(obj)
                overrides = merge_lineage(overrides, parsed)
        except Exception:
            # ignore malformed JSON override blocks
            pass

    # Text override lines
    for payload in _extract_comment_payloads(sql, keys=("lineage", "fft-lineage")):
        parsed = _parse_lineage_text_block(payload)
        overrides = merge_lineage(overrides, parsed)

    return overrides


def merge_lineage(base: LineageMap, overlay: LineageMap) -> LineageMap:
    """
    Union-merge two lineage maps with dedupe.
    """
    out: LineageMap = {k: list(v) for k, v in (base or {}).items()}
    for col, items in (overlay or {}).items():
        if not isinstance(items, list):
            continue
        out.setdefault(col, [])
        out[col].extend(items)
        out[col] = _dedupe_items(out[col])
    # drop empties
    return {k: v for k, v in out.items() if v}


def infer_py_lineage(py_source: str, ref_map: dict[str, str] | None = None) -> LineageMap:
    """
    Minimal python lineage:
      - If the file defines __lineage__ = {...} or LINEAGE = {...}, we literal-eval it.
      - Otherwise return {}.

    This keeps python models supported without forcing heavy AST/dataframe analysis.
    """
    src = py_source or ""
    if not src.strip():
        return {}

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "__lineage__",
                    "LINEAGE",
                    "lineage",
                }:
                    try:
                        val = ast.literal_eval(node.value)
                        if isinstance(val, dict):
                            lm = _normalize_lineage_map(val)
                            # Optional: apply ref_map rewriting of relations
                            if ref_map:
                                lm = _apply_ref_map(lm, ref_map)
                            return lm
                    except Exception:
                        pass
    return {}


# ---------------------------
# SQL inference internals
# ---------------------------

_SIMPLE_WRAPPER_FUNCS = {
    # common "simple" wrappers where arg lineage should be preserved
    "lower",
    "upper",
    "trim",
    "ltrim",
    "rtrim",
    "cast",
    "date",
    "timestamp",
    "coalesce",  # if multiple args, we keep all refs
    "nullif",
}

_CLAUSE_TERMINATORS = {
    "WHERE",
    "GROUP BY",
    "HAVING",
    "ORDER BY",
    "QUALIFY",
    "LIMIT",
    "FETCH",
    "UNION",
    "EXCEPT",
    "INTERSECT",
}


def _strip_to_query(sql: str) -> str:
    """
    Try to slice down to the query part for statements like:
      CREATE VIEW x AS WITH ... SELECT ...
      CREATE TABLE x AS SELECT ...
    """
    s = sql.strip()

    # Prefer "... AS WITH|SELECT"
    m = re.search(r"\bAS\s+(WITH|SELECT)\b", s, flags=re.IGNORECASE)
    if m:
        return s[m.start(1) :].strip()

    # Otherwise start at first WITH or SELECT
    m2 = re.search(r"\b(WITH|SELECT)\b", s, flags=re.IGNORECASE)
    if m2:
        return s[m2.start(1) :].strip()

    return s


def _contains_select(stmt: Statement) -> bool:
    return any(t.ttype is DML and t.value.lower() == "select" for t in stmt.flatten())


def _split_ctes(stmt: Statement) -> tuple[list[tuple[str, str]], Statement]:
    """
    If stmt begins with WITH (or WITH RECURSIVE), extract (cte_name, cte_sql) in order
    and return the main SELECT statement. Otherwise returns ([], stmt).

    Fix: when reconstructing the "main" SQL, preserve whitespace from the original
    token stream (joining the no-whitespace stream breaks parsing: e.g. "selectcol").
    """
    tokens_no_ws = [t for t in stmt.tokens if not _is_ws(t)]
    if not tokens_no_ws:
        return [], stmt

    # Robust WITH detection across sqlparse versions
    if tokens_no_ws[0].value.lower() != "with":
        return [], stmt

    # Handle optional "RECURSIVE"
    start = 1
    if (
        len(tokens_no_ws) > 1
        and tokens_no_ws[1].ttype is Keyword
        and tokens_no_ws[1].value.upper() == "RECURSIVE"
    ):
        start = 2

    # Find the first TOP-LEVEL SELECT token after the CTE definitions.
    select_idx = None
    for i in range(start, len(tokens_no_ws)):
        t = tokens_no_ws[i]
        if t.ttype is DML and t.value.lower() == "select":
            select_idx = i
            break
    if select_idx is None:
        return [], stmt

    # Collect CTE identifiers between WITH[..] and main SELECT
    cte_tokens = tokens_no_ws[start:select_idx]
    cte_defs: list[Identifier] = []
    for ct in cte_tokens:
        if isinstance(ct, IdentifierList):
            for it in ct.get_identifiers():
                if isinstance(it, Identifier):
                    cte_defs.append(it)
        elif isinstance(ct, Identifier):
            cte_defs.append(ct)

    ctes: list[tuple[str, str]] = []
    for ident in cte_defs:
        name = ident.get_real_name() or ident.get_name()
        if not name:
            continue
        parens = [t for t in ident.tokens if isinstance(t, Parenthesis)]
        if not parens:
            continue
        inner = parens[0].value.strip()
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1].strip()
        if inner:
            ctes.append((name, inner))

    # Reconstruct main SQL from the ORIGINAL token stream (preserve whitespace)
    sel_tok = tokens_no_ws[select_idx]
    orig_tokens = list(stmt.tokens)
    try:
        orig_sel_idx = next(i for i, t in enumerate(orig_tokens) if t is sel_tok)
    except StopIteration:
        orig_sel_idx = None

    if orig_sel_idx is None:
        main_sql = str(stmt).strip()
    else:
        main_sql = "".join(t.value for t in orig_tokens[orig_sel_idx:]).strip()

    main_parsed = sqlparse.parse(main_sql)
    main_stmt = main_parsed[0] if main_parsed else stmt
    return ctes, main_stmt


def _infer_select_stmt(stmt: Statement, ref_map: dict[str, str] | None = None) -> LineageMap:
    """
    Infer lineage for a single SELECT statement (no CTE expansion here).
    """
    # Find SELECT list + FROM clause
    projections = _get_projections(stmt)
    alias_map, base_relations = _get_from_sources(stmt)

    # allow externally provided mapping (e.g. ref('x') -> schema.table)
    if ref_map:
        alias_map.update(ref_map)

    out: LineageMap = {}

    for proj in projections:
        out_col = _projection_output_name(proj)
        if not out_col:
            continue

        refs = _extract_column_refs(proj)
        if not refs:
            # no column references found -> no lineage edge
            continue

        transformed = _is_transformed_projection(proj)

        edges: list[LineageItem] = []
        for parent, col in refs:
            rel = None
            if parent:
                rel = alias_map.get(parent) or parent
            # only safe: single base relation
            elif len(base_relations) == 1:
                rel = base_relations[0]

            if rel and col:
                edges.append({"from_relation": rel, "from_column": col, "transformed": transformed})

        if edges:
            out[out_col] = _dedupe_items(edges)

    return out


def _get_projections(stmt: Statement) -> list[Projection]:
    """
    Returns a list of projection tokens from the top-level SELECT.
    """
    tokens = [t for t in stmt.tokens if not _is_ws(t)]
    seen_select = False
    projs: list[Projection] = []

    for t in tokens:
        if t.ttype is DML and t.value.lower() == "select":
            seen_select = True
            continue
        if not seen_select:
            continue
        if t.ttype is Keyword and t.value.upper() == "FROM":
            break

        if isinstance(t, IdentifierList):
            for it in t.get_identifiers():
                if isinstance(it, (Identifier, Function, Parenthesis, TokenList)):
                    projs.append(it)
                elif isinstance(it, Token) and it.ttype is Name:
                    projs.append(it)  # bare column name
        elif isinstance(t, (Identifier, Function, Parenthesis)):
            projs.append(t)
        elif isinstance(t, Token) and t.ttype is Name:
            projs.append(t)  # bare column name

    # fallback: if sqlparse didn't group things nicely, reparse select list by string
    if not projs:
        select_list = _select_list_text(stmt)
        for expr in _split_top_level_commas(select_list):
            tmp = sqlparse.parse(f"SELECT {expr} FROM __dummy__")
            if tmp:
                projs.extend(_get_projections(tmp[0]))

    return projs


def _select_list_text(stmt: Statement) -> str:
    """
    Extract SELECT list as raw text between SELECT and first top-level FROM.
    """
    s = str(stmt)
    # crude but effective for fallback; we only use when sqlparse didn't group
    m = re.search(r"\bselect\b", s, flags=re.IGNORECASE)
    if not m:
        return ""
    start = m.end()
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and s[i : i + 4].lower() == "from":
            return s[start:i].strip()
    return ""


def _split_top_level_commas(s: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_single = False

    i = 0
    while i < len(s):
        ch = s[i]

        # basic single-quote string skipping
        if ch == "'" and (i == 0 or s[i - 1] != "\\"):
            in_single = not in_single

        if not in_single:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)

        if ch == "," and depth == 0 and not in_single:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
        else:
            buf.append(ch)

        i += 1

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _get_from_sources(stmt: Statement) -> tuple[dict[str, str], list[str]]:
    """
    Parse top-level FROM/JOIN sources into:
      - alias_map: alias -> relation (and relation -> relation)
      - base_relations: list of relations encountered (FROM/JOIN order)
    """
    from_clause = _extract_from_clause_text(stmt)
    if not from_clause:
        return {}, []

    # Match: FROM rel [AS alias] ; JOIN rel [AS alias]
    # rel supports schema.table and quoted identifiers.
    pat = re.compile(
        r"\b(from|join)\s+"
        r"(?P<rel>(?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[a-zA-Z_][\w\$]*)(?:\.(?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[a-zA-Z_][\w\$]*))*)"
        r"(?:\s+(?:as\s+)?(?P<alias>[a-zA-Z_][\w\$]*))?",
        flags=re.IGNORECASE,
    )

    alias_map: dict[str, str] = {}
    base: list[str] = []
    for m in pat.finditer(from_clause):
        rel = _strip_ident_quotes(m.group("rel"))
        alias = m.group("alias")
        if not rel:
            continue
        base.append(rel)
        alias_map.setdefault(rel, rel)
        if alias:
            alias_map[alias] = rel

    return alias_map, base


def _extract_from_clause_text(stmt: Statement) -> str:
    """
    Extract raw FROM ... part until next clause terminator.
    """
    s = str(stmt)
    m = re.search(r"\bfrom\b", s, flags=re.IGNORECASE)
    if not m:
        return ""
    start = m.start()

    # stop at next clause terminator keyword outside parentheses
    depth = 0
    i = start
    end = len(s)
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)

        if depth == 0:
            # check terminators
            for term in _CLAUSE_TERMINATORS:
                if s[i : i + len(term)].upper() == term:
                    end = i
                    i = len(s)
                    break
        i += 1

    return s[start:end].strip()


def _projection_output_name(proj: Projection) -> str | None:
    """
    Determine output column name for a projection:
      - prefer alias
      - else, for simple identifiers return column name
      - otherwise None (we need an alias to attach lineage reliably)
    """
    if isinstance(proj, Identifier):
        return proj.get_alias() or proj.get_name() or proj.get_real_name()
    if isinstance(proj, Token) and proj.ttype is Name:
        return proj.value
    return None


def _is_transformed_projection(proj: Projection) -> bool:
    """
    Heuristic transform flag:
      - False only for direct column references (optionally qualified),
        with alias == column if present
      - True otherwise (functions, ops, casts, renames, etc.)
    """
    if isinstance(proj, Identifier):
        # direct a.b or b
        alias = proj.get_alias()
        col = proj.get_real_name()
        parent = proj.get_parent_name()

        # Remove alias portion (e.g., "AS alias" or trailing alias) before checking for direct refs
        expr_tokens = list(proj.tokens)
        if alias:
            cut = None
            for i, t in enumerate(expr_tokens):
                if t.ttype is Keyword and t.value.upper() == "AS":
                    cut = i
                    break
            if cut is not None:
                expr_tokens = expr_tokens[:cut]
            else:
                alias_norm = alias.strip("\"'").lower()
                for i in range(len(expr_tokens) - 1, -1, -1):
                    t = expr_tokens[i]
                    if (
                        isinstance(t, Token)
                        and t.ttype is Name
                        and t.value.strip("\"'").lower() == alias_norm
                    ):
                        expr_tokens = expr_tokens[:i]
                        break

        base_expr = "".join(t.value for t in expr_tokens).strip()
        direct = col and (
            base_expr == col or (parent and base_expr == f"{parent}.{col}") or base_expr == f"{col}"
        )
        if direct and (alias is None or alias == col):
            return False

        # function inside identifier => transformed
        if any(isinstance(t, Function) for t in proj.tokens):
            return True

        # rename-only is still "transformed" per docs UI expectation
        if alias and col and alias != col:
            return True

    # default: transformed
    return True


def _extract_column_refs(tok: Any) -> list[tuple[str | None, str]]:
    """
    Extract column references as (parent/table_alias, column_name).

    Fix: don't treat output aliases as input column refs (e.g. "... as first_signup").
    """
    refs: list[tuple[str | None, str]] = []

    def walk_children(node: Any) -> list[tuple[str | None, str]]:
        out: list[tuple[str | None, str]] = []
        for child in getattr(node, "tokens", []) or []:
            out.extend(_extract_column_refs(child))
        return out

    def handle_name(node: Token) -> list[tuple[str | None, str]]:
        return [(None, node.value)]

    def handle_identifier(node: Identifier) -> list[tuple[str | None, str]]:
        alias = node.get_alias()

        is_expr = any(isinstance(t, (Function, Parenthesis)) for t in node.tokens) or any(
            getattr(t, "is_group", False) for t in node.tokens
        )
        if is_expr:
            expr_tokens = list(node.tokens)

            if alias:
                cut = None
                for i, t in enumerate(expr_tokens):
                    if t.ttype is Keyword and t.value.upper() == "AS":
                        cut = i
                        break

                if cut is not None:
                    expr_tokens = expr_tokens[:cut]
                else:
                    alias_norm = alias.strip("\"'").lower()
                    for i in range(len(expr_tokens) - 1, -1, -1):
                        t = expr_tokens[i]
                        if (
                            isinstance(t, Token)
                            and t.ttype is Name
                            and t.value.strip("\"'").lower() == alias_norm
                        ):
                            expr_tokens = expr_tokens[:i]
                            break

            out: list[tuple[str | None, str]] = []
            for t in expr_tokens:
                out.extend(_extract_column_refs(t))
            return out

        col = node.get_real_name()
        parent = node.get_parent_name()
        return [(parent, col)] if col else []

    def handle_identifier_list(node: IdentifierList) -> list[tuple[str | None, str]]:
        out: list[tuple[str | None, str]] = []
        for it in node.get_identifiers():
            out.extend(_extract_column_refs(it))
        return out

    def handle_function(node: Function) -> list[tuple[str | None, str]]:
        fn_name = (node.get_name() or "").lower()
        out: list[tuple[str | None, str]] = []
        for t in node.tokens:
            if isinstance(t, Identifier) and (t.get_name() or "").lower() == fn_name:
                continue
            out.extend(_extract_column_refs(t))
        return out

    handlers: dict[type[Any], Callable[[Any], list[tuple[str | None, str]]]] = {
        Identifier: handle_identifier,
        IdentifierList: handle_identifier_list,
        Function: handle_function,
        Parenthesis: walk_children,
    }

    # Bare Name token (rarely used directly in projections, but kept for compatibility)
    if isinstance(tok, Token) and tok.ttype is Name:
        return handle_name(tok)

    for typ, fn in handlers.items():
        if isinstance(tok, typ):
            return fn(tok)

    if getattr(tok, "is_group", False):
        return walk_children(tok)

    return refs


def _expand_cte_edges(lin: LineageMap, cte_lineage: dict[str, LineageMap]) -> LineageMap:
    """
    Replace edges pointing to a CTE relation with that CTE's own lineage for that column.
    """
    if not lin:
        return {}

    out: LineageMap = {}
    for out_col, edges in lin.items():
        expanded: list[LineageItem] = []
        for e in edges or []:
            rel = str(e.get("from_relation") or "")
            col = str(e.get("from_column") or "")
            if rel in cte_lineage and col in (cte_lineage[rel] or {}):
                for sub in cte_lineage[rel][col]:
                    expanded.append(
                        {
                            "from_relation": sub.get("from_relation"),
                            "from_column": sub.get("from_column"),
                            "transformed": bool(e.get("transformed"))
                            or bool(sub.get("transformed")),
                        }
                    )
            else:
                expanded.append(e)
        expanded = [x for x in expanded if x.get("from_relation") and x.get("from_column")]
        if expanded:
            out[out_col] = _dedupe_items(expanded)
    return out


# ---------------------------
# Override parsing internals
# ---------------------------


def _extract_comment_payloads(sql: str, keys: tuple[str, ...]) -> list[str]:
    """
    Extract payloads from comments like:
      -- key: payload
      /* key: payload */
    Returns a list of payload strings (not including the key).
    """
    out: list[str] = []

    # line comments
    for key in keys:
        line_pat = re.compile(rf"--\s*{re.escape(key)}\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
        out.extend(m.group(1).strip() for m in line_pat.finditer(sql))

    # block comments
    for key in keys:
        block_pat = re.compile(rf"/\*\s*{re.escape(key)}\s*:\s*(.*?)\*/", re.IGNORECASE | re.DOTALL)
        out.extend(m.group(1).strip() for m in block_pat.finditer(sql))

    return out


_LINEAGE_TEXT_LINE = re.compile(
    r"^\s*(?P<out>[a-zA-Z_][\w\$]*)\s*(?:<-|<=|=|:)\s*(?P<srcs>.+?)\s*$"
)

_SRC_REF = re.compile(
    r"(?P<rel>(?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[a-zA-Z_][\w\$]*)(?:\.(?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[a-zA-Z_][\w\$]*))*)"
    r"\.(?P<col>[a-zA-Z_][\w\$]*)"
)


def _parse_lineage_text_block(block: str) -> LineageMap:
    """
    Parse a multi-line block of text overrides.
    """
    out: LineageMap = {}
    for raw_line in (block or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _LINEAGE_TEXT_LINE.match(line)
        if not m:
            continue

        out_col = m.group("out")
        srcs = m.group("srcs")

        # xform flag
        xform = False
        if re.search(r"\b(xform|transformed)\b", srcs, flags=re.IGNORECASE):
            xform = True
            srcs = re.sub(r"\b(xform|transformed)\b", "", srcs, flags=re.IGNORECASE).strip()

        items: list[LineageItem] = []
        for src in re.split(r"\s*,\s*", srcs):
            sm = _SRC_REF.search(src)
            if not sm:
                continue
            rel = _strip_ident_quotes(sm.group("rel"))
            col = sm.group("col")
            items.append({"from_relation": rel, "from_column": col, "transformed": xform})

        if items:
            out[out_col] = _dedupe_items(out.get(out_col, []) + items)

    return out


def _normalize_lineage_map(obj: Any) -> LineageMap:
    """
    Accepts either:
      {"col":[{...},{...}], "col2":[...]}
    or:
      {"col": {...single...}}
    and normalizes to LineageMap.
    """
    if not isinstance(obj, dict):
        return {}

    out: LineageMap = {}
    for k, v in obj.items():
        if isinstance(v, dict):
            out[str(k)] = [v]
        elif isinstance(v, list):
            out[str(k)] = [x for x in v if isinstance(x, dict)]
    for k in list(out.keys()):
        out[k] = _dedupe_items(out[k])
    return out


def _apply_ref_map(lm: LineageMap, ref_map: dict[str, str]) -> LineageMap:
    out: LineageMap = {}
    for col, items in (lm or {}).items():
        new_items: list[LineageItem] = []
        for it in items:
            rel = it.get("from_relation")
            if isinstance(rel, str) and rel in ref_map:
                item_copy = dict(it)
                item_copy["from_relation"] = ref_map[rel]
                new_items.append(item_copy)
            else:
                new_items.append(it)
        out[col] = _dedupe_items(new_items)
    return out


# ---------------------------
# Utilities
# ---------------------------


def _dedupe_items(items: list[LineageItem]) -> list[LineageItem]:
    seen = set()
    out: list[LineageItem] = []
    for it in items or []:
        rel = it.get("from_relation")
        col = it.get("from_column")
        if not rel or not col:
            continue
        key = (
            str(rel),
            str(col),
            bool(it.get("transformed")),
            str(it.get("confidence") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _strip_ident_quotes(s: str) -> str:
    """
    Remove simple quoting from identifiers: `x`, "x", [x]
    Leaves internal dots intact.
    """
    s = (s or "").strip()
    if not s:
        return s

    def strip_one(part: str) -> str:
        part = part.strip()
        if part.startswith("`") and part.endswith("`"):
            return part[1:-1]
        if part.startswith('"') and part.endswith('"'):
            return part[1:-1]
        if part.startswith("[") and part.endswith("]"):
            return part[1:-1]
        return part

    return ".".join(strip_one(p) for p in s.split("."))


def _is_ws(t: Any) -> bool:
    return (
        t is None
        or t.ttype in (Whitespace, Newline)
        or (hasattr(t, "is_whitespace") and t.is_whitespace)
    )
