# src/fastflowtransform/dag.py
import heapq
import re
from collections import defaultdict
from typing import Any

from fastflowtransform.core import Node, relation_for
from fastflowtransform.errors import DependencyNotFoundError, ModelCycleError


def topo_sort(nodes: dict[str, Node]) -> list[str]:
    missing = {
        n.name: sorted({d for d in n.deps if d not in nodes})
        for n in nodes.values()
        if any(d not in nodes for d in n.deps)
    }
    if missing:
        raise DependencyNotFoundError(missing)

    indeg = {k: 0 for k in nodes}
    out: dict[str, set[str]] = defaultdict(set)
    for n in nodes.values():
        for d in set(n.deps):
            out[d].add(n.name)
            indeg[n.name] += 1

    heap = [k for k, deg in indeg.items() if deg == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        u = heapq.heappop(heap)
        order.append(u)
        for v in sorted(out.get(u, ())):
            indeg[v] -= 1
            if indeg[v] == 0:
                heapq.heappush(heap, v)

    if len(order) != len(nodes):
        cyclic = [k for k, deg in indeg.items() if deg > 0]
        raise ModelCycleError(f"Cycle detected among nodes: {', '.join(sorted(cyclic))}")
    return order


def levels(nodes: dict[str, Node]) -> list[list[str]]:
    """
    Returns a level-wise topological ordering.
    - Each inner list contains nodes with no prerequisites inside the remaining
      graph (i.e. eligible to run in parallel).
    - Ordering within a level is lexicographically stable.
    - Validation for missing deps/cycles matches topo_sort.
    """
    # Fehlende Deps einsammeln (nur Modell-Refs; sources sind keine Nodes)
    missing = {
        n.name: sorted({d for d in (n.deps or []) if d not in nodes})
        for n in nodes.values()
        if any(d not in nodes for d in (n.deps or []))
    }
    if missing:
        raise DependencyNotFoundError(missing)

    indeg = {k: 0 for k in nodes}
    out: dict[str, set[str]] = defaultdict(set)
    for n in nodes.values():
        for d in set(n.deps or []):
            out[d].add(n.name)
            indeg[n.name] += 1

    # Start-Level: alle 0-Indegree
    current = sorted([k for k, deg in indeg.items() if deg == 0])
    lvls: list[list[str]] = []
    seen_count = 0

    while current:
        lvls.append(current)
        next_zero: set[str] = set()
        for u in current:
            seen_count += 1
            for v in sorted(out.get(u, ())):
                indeg[v] -= 1
                if indeg[v] == 0:
                    next_zero.add(v)
        current = sorted(next_zero)

    if seen_count != len(nodes):
        cyclic = [k for k, deg in indeg.items() if deg > 0]
        raise ModelCycleError(f"Cycle detected among nodes: {', '.join(sorted(cyclic))}")
    return lvls


def _mm_id(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return "_" + s if s and s[0].isdigit() else (s or "_node")


def _quote_label(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _source_node_id(source_name: str, table_name: str) -> str:
    return _mm_id(f"src_{source_name}_{table_name}")


def mermaid(
    nodes: dict[str, Node],
    source_links: dict[tuple[str, str], dict[str, str]] | None = None,
    model_source_refs: dict[str, list[tuple[str, str]]] | None = None,
) -> str:
    lines = [
        "flowchart TD",
        "  classDef sql fill:#e8f1ff,stroke:#5b8def,color:#0a1f44;",
        "  classDef py  fill:#e9fbf1,stroke:#2bb673,color:#0b2e1f;",
    ]
    if source_links:
        lines.append("  classDef source fill:#fef3c7,stroke:#f59e0b,color:#78350f;")

    # Nodes
    for n in sorted(nodes.values(), key=lambda x: x.name):
        nid = _mm_id(n.name)
        phys = relation_for(n.name)
        label = _quote_label(f"{n.name}<br/>({phys})")
        if n.kind == "python":
            lines.append(f"  {nid}({label})")
            lines.append(f"  class {nid} py;")
        else:
            lines.append(f"  {nid}[{label}]")
            lines.append(f"  class {nid} sql;")
        lines.append(f'  click {nid} "{n.name}.html" "View model"')

    source_ids: dict[tuple[str, str], str] = {}
    if source_links:
        for (src_name, tbl_name), meta in sorted(source_links.items(), key=lambda x: x[0]):
            sid = _source_node_id(src_name, tbl_name)
            label = _quote_label(meta.get("label") or f"{src_name}.{tbl_name}")
            lines.append(f"  {sid}[[{label}]]")
            lines.append(f"  class {sid} source;")
            link = meta.get("file")
            if link:
                lines.append(f'  click {sid} "{link}" "View source"')
            source_ids[(src_name, tbl_name)] = sid

    # Edges
    for n in nodes.values():
        tgt = _mm_id(n.name)
        for d in n.deps:
            if d in nodes:
                lines.append(f"  {_mm_id(d)} --> {tgt}")
        if model_source_refs and source_ids:
            for ref in model_source_refs.get(n.name, []):
                ref_id = source_ids.get(ref)
                if ref_id is None:
                    continue
                sid = ref_id
                lines.append(f"  {sid} --> {tgt}")

    lines.append("")
    return "\n".join(lines)


# --- SPA Graph (layout computed server-side) -----------------------------


def _clamp(n: float, lo: float, hi: float) -> float:
    return lo if n < lo else hi if n > hi else n


def _approx_node_size(title: str, subtitle: str | None) -> tuple[int, int]:
    """
    Approximate label width without font measurement.
    Produces stable, decent-looking boxes for SVG render.
    """
    title = title or ""
    subtitle = subtitle or ""
    longest = max(len(title), len(subtitle))
    # ~7px per char + padding
    w = int(_clamp(longest * 7.2 + 56, 170, 380))
    h = 52 if subtitle else 44
    return w, h


def _barycenter_order(
    level: list[str],
    parents: dict[str, list[str]],
    prev_pos: dict[str, int],
) -> list[str]:
    """
    Order nodes in a level by the average position of their parents in the previous level.
    Stable fallback to name.
    """

    def key(nm: str) -> tuple[float, str]:
        ps = [p for p in parents.get(nm, []) if p in prev_pos]
        if not ps:
            return (1e12, nm)
        avg = sum(prev_pos[p] for p in ps) / max(1, len(ps))
        return (avg, nm)

    return sorted(level, key=key)


_SRC_PREFIX = "__src__:"


def _collect_source_keys(
    *,
    sources_by_key: dict[tuple[str, str], Any],
    model_source_refs: dict[str, list[tuple[str, str]]],
) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []

    for refs in model_source_refs.values():
        for key in refs or []:
            if key in sources_by_key and key not in seen:
                seen.add(key)
                out.append(key)

    out.sort(key=lambda k: (k[0], k[1]))
    return out


def _build_parents(nodes: dict[str, "Node"]) -> dict[str, list[str]]:
    return {nm: [d for d in (n.deps or []) if d in nodes] for nm, n in nodes.items()}


def _build_ordered_levels(
    *,
    lvls: list[list[str]],
    parents: dict[str, list[str]],
    model_rank_offset: int,
    source_keys: list[tuple[str, str]],
) -> dict[int, list[str]]:
    ordered_levels: dict[int, list[str]] = {}
    prev_positions: dict[str, int] = {}

    if source_keys:
        ordered_levels[0] = [f"{_SRC_PREFIX}{s}.{t}" for (s, t) in source_keys]

    for i, lvl in enumerate(lvls):
        r = i + model_rank_offset
        ordered = sorted(lvl) if i == 0 else _barycenter_order(lvl, parents, prev_positions)
        ordered_levels[r] = ordered
        prev_positions = {nm: idx for idx, nm in enumerate(ordered)}

    return ordered_levels


def _rank_xy(
    *,
    r: int,
    idx: int,
    rank_len: int,
    max_count: int,
    rank_spacing: int,
    node_spacing: int,
    padding: int,
) -> tuple[int, int]:
    y0 = padding + int((max_count - rank_len) * node_spacing * 0.5)
    x = padding + r * rank_spacing
    y = y0 + idx * node_spacing
    return x, y


def _model_payload(
    *,
    nm: str,
    nodes: dict[str, "Node"],
    r: int,
    idx: int,
    rank_len: int,
    max_count: int,
    rank_spacing: int,
    node_spacing: int,
    padding: int,
) -> dict[str, Any]:
    n = nodes[nm]
    rel = relation_for(nm)
    mat = (getattr(n, "meta", {}) or {}).get("materialized", "table")
    w, h = _approx_node_size(nm, rel)
    x, y = _rank_xy(
        r=r,
        idx=idx,
        rank_len=rank_len,
        max_count=max_count,
        rank_spacing=rank_spacing,
        node_spacing=node_spacing,
        padding=padding,
    )

    return {
        "id": f"m:{nm}",
        "kind": "model",
        "name": nm,
        "route": f"#/model/{nm}",
        "type": getattr(n, "kind", "sql"),
        "materialized": mat,
        "relation": rel,
        "rank": r,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
    }


def _source_payload(
    *,
    src: str,
    tbl: str,
    sources_by_key: dict[tuple[str, str], Any],
    r: int,
    idx: int,
    rank_len: int,
    max_count: int,
    rank_spacing: int,
    node_spacing: int,
    padding: int,
) -> dict[str, Any]:
    doc = sources_by_key.get((src, tbl))
    rel = (
        getattr(doc, "relation", None)
        or (doc.get("relation") if isinstance(doc, dict) else None)
        or f"{src}.{tbl}"
    )
    label = f"{src}.{tbl}"
    w, h = _approx_node_size(label, rel)
    x, y = _rank_xy(
        r=r,
        idx=idx,
        rank_len=rank_len,
        max_count=max_count,
        rank_spacing=rank_spacing,
        node_spacing=node_spacing,
        padding=padding,
    )

    return {
        "id": f"s:{src}.{tbl}",
        "kind": "source",
        "source_name": src,
        "table_name": tbl,
        "route": f"#/source/{src}/{tbl}",
        "relation": rel,
        "rank": r,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
    }


def _emit_nodes(
    *,
    nodes: dict[str, "Node"],
    sources_by_key: dict[tuple[str, str], Any],
    ordered_levels: dict[int, list[str]],
    max_count: int,
    rank_spacing: int,
    node_spacing: int,
    padding: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    for r, names in ordered_levels.items():
        rank_len = len(names)
        for idx, item in enumerate(names):
            if item.startswith(_SRC_PREFIX):
                raw = item.split(":", 1)[1]
                src, tbl = raw.split(".", 1)
                out.append(
                    _source_payload(
                        src=src,
                        tbl=tbl,
                        sources_by_key=sources_by_key,
                        r=r,
                        idx=idx,
                        rank_len=rank_len,
                        max_count=max_count,
                        rank_spacing=rank_spacing,
                        node_spacing=node_spacing,
                        padding=padding,
                    )
                )
            else:
                out.append(
                    _model_payload(
                        nm=item,
                        nodes=nodes,
                        r=r,
                        idx=idx,
                        rank_len=rank_len,
                        max_count=max_count,
                        rank_spacing=rank_spacing,
                        node_spacing=node_spacing,
                        padding=padding,
                    )
                )

    return out


def _emit_edges(
    *,
    nodes: dict[str, "Node"],
    sources_by_key: dict[tuple[str, str], Any],
    model_source_refs: dict[str, list[tuple[str, str]]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # model deps
    for nm, n in nodes.items():
        for d in n.deps or []:
            if d in nodes:
                out.append({"from": f"m:{d}", "to": f"m:{nm}", "kind": "dep"})

    # sources -> models
    for nm, refs in model_source_refs.items():
        for src, tbl in refs or []:
            if (src, tbl) in sources_by_key:
                out.append({"from": f"s:{src}.{tbl}", "to": f"m:{nm}", "kind": "source"})

    return out


def _apply_direction(direction: str, out_nodes: list[dict[str, Any]]) -> str:
    d = direction.upper()
    if d == "TB":
        for n in out_nodes:
            n["x"], n["y"] = n["y"], n["x"]
    return d


def _bounds(out_nodes: list[dict[str, Any]], padding: int) -> dict[str, int]:
    if not out_nodes:
        return {"minx": 0, "miny": 0, "maxx": 0, "maxy": 0, "width": padding, "height": padding}

    minx = min(n["x"] for n in out_nodes)
    miny = min(n["y"] for n in out_nodes)
    maxx = max(n["x"] + n["w"] for n in out_nodes)
    maxy = max(n["y"] + n["h"] for n in out_nodes)

    return {
        "minx": int(minx),
        "miny": int(miny),
        "maxx": int(maxx),
        "maxy": int(maxy),
        "width": int(maxx - minx + padding),
        "height": int(maxy - miny + padding),
    }


def spa_graph(
    nodes: dict[str, "Node"],
    *,
    sources_by_key: dict[tuple[str, str], Any] | None = None,
    model_source_refs: dict[str, list[tuple[str, str]]] | None = None,
    direction: str = "LR",  # LR or TB
    rank_spacing: int = 280,
    node_spacing: int = 84,
    padding: int = 24,
) -> dict[str, Any]:
    """
    Build a lightweight graph payload for the SPA:
    - Layout computed here (no JS graph libs needed)
    - Browser renders SVG + pan/zoom + navigation

    sources_by_key values may be SourceDoc or dict-like; we only access:
      .source_name, .table_name, .relation
    """
    sources_by_key = sources_by_key or {}
    model_source_refs = model_source_refs or {}

    # Levels for models only (sources aren't Nodes)
    lvls = levels(nodes)  # raises on cycles/missing deps like topo_sort

    source_keys = _collect_source_keys(
        sources_by_key=sources_by_key,
        model_source_refs=model_source_refs,
    )
    has_sources = bool(source_keys)
    model_rank_offset = 1 if has_sources else 0

    parents = _build_parents(nodes)
    ordered_levels = _build_ordered_levels(
        lvls=lvls,
        parents=parents,
        model_rank_offset=model_rank_offset,
        source_keys=source_keys,
    )
    max_count = max((len(v) for v in ordered_levels.values()), default=1)

    out_nodes = _emit_nodes(
        nodes=nodes,
        sources_by_key=sources_by_key,
        ordered_levels=ordered_levels,
        max_count=max_count,
        rank_spacing=rank_spacing,
        node_spacing=node_spacing,
        padding=padding,
    )
    out_edges = _emit_edges(
        nodes=nodes,
        sources_by_key=sources_by_key,
        model_source_refs=model_source_refs,
    )

    normalized_direction = _apply_direction(direction, out_nodes)
    bounds = _bounds(out_nodes, padding)

    return {
        "direction": normalized_direction,
        "nodes": out_nodes,
        "edges": out_edges,
        "bounds": bounds,
    }
