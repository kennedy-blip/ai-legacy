"""Repository-level findings computed once per ingestion and stored on the Repository node."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from graph_engine.models import GraphNode
from graph_engine.schema import NodeLabel, RelType
from ingestion.graph_builder import BuiltGraph
from ingestion.resolver import DEFAULT_MIN_CONFIDENCE


def _brief(node: GraphNode, **extra: Any) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.qualified_name,
        "path": node.path,
        "line": node.line,
        **extra,
    }


def compute_insights(graph: BuiltGraph, *, top: int = 10) -> dict[str, Any]:
    """Rank the parts of the code base a newcomer should know about first."""
    functions = [node for node in graph.nodes.values() if node.label is NodeLabel.FUNCTION]
    product = [node for node in functions if not node.get("is_test")]
    tables = [node for node in graph.nodes.values() if node.label is NodeLabel.DATABASE_TABLE]

    readers: dict[str, set[str]] = defaultdict(set)
    writers: dict[str, set[str]] = defaultdict(set)
    triggers: Counter[str] = Counter()
    for edge in graph.edges.values():
        if edge.confidence < DEFAULT_MIN_CONFIDENCE:
            continue
        source = graph.nodes.get(edge.source)
        if edge.type is RelType.FIRES_TRIGGER:
            triggers[edge.source] += 1
        elif source is not None and source.label is NodeLabel.FUNCTION:
            if edge.type is RelType.READS_FROM_DB:
                readers[edge.target].add(edge.source)
            elif edge.type is RelType.MUTATES_TABLE:
                writers[edge.target].add(edge.source)

    entry_kinds = Counter(str(node.get("entry_kind")) for node in product if node.get("is_entry_point"))
    dead = [node for node in product if node.get("dead_candidate")]
    # "Oldest untouched logic": skip accessors, constructors and one-liners, which are old everywhere.
    dated = [
        node
        for node in product
        if node.get("last_modified_at")
        and not node.get("is_boilerplate")
        and node.get("kind") != "constructor"
        and int(node.get("loc") or 0) >= 5
    ]
    silos = [
        node
        for node in product
        if node.get("author_count") == 1
        and int(node.get("loc") or 0) >= 15
        and not node.get("is_boilerplate")
    ]
    silo_owners = Counter(str(node.get("primary_author")) for node in silos)

    def ranked(nodes: list[GraphNode], key: str) -> list[dict[str, Any]]:
        best = sorted(nodes, key=lambda n: (-int(n.get(key) or 0), n.qualified_name, n.id))[:top]
        return [
            _brief(node, **{key: int(node.get(key) or 0)}) for node in best if int(node.get(key) or 0) > 0
        ]

    return {
        "most_called": ranked(product, "fan_in"),
        "most_complex": ranked(product, "complexity"),
        "largest": ranked(product, "loc"),
        "entry_points": dict(entry_kinds),
        "entry_point_count": sum(entry_kinds.values()),
        "dead_candidates": {
            "count": len(dead),
            "lines": sum(int(node.get("loc") or 0) for node in dead),
            "largest": ranked(dead, "loc"),
        },
        "hot_tables": [
            {
                "id": table.id,
                "name": table.name,
                "readers": len(readers[table.id]),
                "writers": len(writers[table.id]),
                "triggers": triggers[table.id],
                "declared": bool(table.get("declared")),
            }
            for table in sorted(
                tables, key=lambda t: (-(len(readers[t.id]) + 2 * len(writers[t.id])), t.name)
            )[:top]
            if readers[table.id] or writers[table.id]
        ],
        "undeclared_tables": sorted(table.name for table in tables if not table.get("declared"))[:50],
        "oldest_code": [
            _brief(node, last_modified_at=node.get("last_modified_at"), last_author=node.get("last_author"))
            for node in sorted(dated, key=lambda n: (str(n.get("last_modified_at")), n.id))[:top]
        ],
        "single_author_code": {
            "functions": len(silos),
            "owners": [{"name": name, "functions": count} for name, count in silo_owners.most_common(top)],
        },
    }
