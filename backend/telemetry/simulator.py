"""Synthetic runtime traffic for demonstrations.

Generates plausible traces by walking the static call graph from the repository's
entry points. The result is **not** an observation of a running system; edges it
produces are tagged ``agent: "simulated"`` and the UI labels them as such. Its
purpose is to show the runtime overlay without deploying an agent.

To make the overlay interesting, calls that static analysis resolved only through
dynamic dispatch (``implementation`` / ``subclass``) are narrowed to a single
target per trace, the way a real execution would pick one implementation.
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

from graph_engine.models import GraphEdge, GraphNode
from graph_engine.schema import NodeLabel, RelType
from graph_engine.store import GraphStore

_DYNAMIC = frozenset({"implementation", "subclass"})
_MAX_DEPTH = 8


def simulate_events(
    store: GraphStore, repository_id: str, *, traces: int = 200, seed: int | None = None
) -> list[dict[str, Any]]:
    """Collector events for ``traces`` simulated requests."""
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    entries = store.find_nodes(
        repository_id, labels=[NodeLabel.FUNCTION], where={"is_entry_point": True}, limit=500
    )
    entries = [node for node in entries if not node.get("is_test")]
    if not entries:
        return []
    names: dict[str, GraphNode] = {}
    calls: dict[str, list[GraphEdge]] = defaultdict(list)
    data: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in store.list_edges(
        repository_id, [RelType.CALLS, RelType.READS_FROM_DB, RelType.MUTATES_TABLE]
    ):
        if edge.confidence < 0.5:
            continue
        (calls if edge.type is RelType.CALLS else data)[edge.source].append(edge)
    wanted = {edge.source for edges in calls.values() for edge in edges}
    wanted |= {edge.target for edges in calls.values() for edge in edges}
    wanted |= {edge.target for edges in data.values() for edge in edges} | set(data)
    ids = sorted(wanted | {node.id for node in entries})
    for start in range(0, len(ids), 500):
        for node in store.get_nodes(ids[start : start + 500]):
            names[node.id] = node

    def symbol(node_id: str) -> str | None:
        node = names.get(node_id)
        if node is None or not node.path or not node.line:
            return None
        return f"{node.path}:{node.line}"  # a location is unambiguous even for overloads

    events: list[dict[str, Any]] = []
    # Popular endpoints get most of the traffic, as in production.
    weights = [1.0 / (rank + 1) for rank in range(len(entries))]
    for _ in range(traces):
        entry = rng.choices(entries, weights=weights, k=1)[0]
        stack = [(entry.id, 0)]
        visited: set[str] = set()
        while stack:
            current, depth = stack.pop()
            if current in visited or depth > _MAX_DEPTH:
                continue
            visited.add(current)
            caller = symbol(current)
            if caller is None:
                continue
            for edge in data.get(current, ()):
                table = names.get(edge.target)
                if table is None or table.label is not NodeLabel.DATABASE_TABLE:
                    continue
                operations = edge.properties.get("operations") or ["SELECT"]
                events.append(
                    {
                        "type": "query",
                        "function": caller,
                        "table": table.name,
                        "operation": operations[0],
                        "duration_ms": round(rng.lognormvariate(0.6, 0.7), 2),
                    }
                )
            dynamic: dict[int, list[GraphEdge]] = defaultdict(list)
            for edge in calls.get(current, ()):
                lines = edge.properties.get("lines") or [0]
                if edge.properties.get("resolution") in _DYNAMIC:
                    dynamic[lines[0]].append(edge)
                    continue
                if rng.random() < 0.85:  # not every branch is taken on every request
                    callee = symbol(edge.target)
                    if callee is not None:
                        events.append(
                            {
                                "type": "call",
                                "caller": caller,
                                "callee": callee,
                                "duration_ms": round(rng.lognormvariate(1.0, 0.8), 2),
                                "error": rng.random() < 0.01,
                            }
                        )
                        stack.append((edge.target, depth + 1))
            for candidates in dynamic.values():
                chosen = rng.choice(candidates)
                callee = symbol(chosen.target)
                if callee is not None:
                    events.append(
                        {
                            "type": "call",
                            "caller": caller,
                            "callee": callee,
                            "duration_ms": round(rng.lognormvariate(1.0, 0.8), 2),
                        }
                    )
                    stack.append((chosen.target, depth + 1))
    return events
