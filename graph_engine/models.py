"""Store-agnostic property-graph value objects."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from graph_engine.schema import NodeLabel, RelType

#: Keys held in dedicated :class:`GraphNode` fields; they may not appear in ``properties``.
RESERVED_NODE_KEYS: Final = frozenset({"id", "label", "repository_id"})
#: Keys held in dedicated :class:`GraphEdge` fields; they may not appear in ``properties``.
RESERVED_EDGE_KEYS: Final = frozenset({"source", "target", "type", "repository_id"})

_SCALARS: Final = (str, bool, int, float)


def clean_properties(properties: Mapping[str, Any], *, reserved: frozenset[str]) -> dict[str, Any]:
    """Validate and normalise a property map to what *every* backend can persist.

    Neo4j properties are scalars or homogeneous lists of scalars, and a ``null``
    property is indistinguishable from an absent one. Both stores funnel writes
    through this function so the embedded store can never accept data that the
    production store would reject or silently alter.

    * ``None`` values are dropped.
    * Tuples become lists; lists must contain only scalars of one kind
      (``int`` and ``float`` may mix and are widened to ``float``).
    * Nested mappings/lists raise ``TypeError`` - JSON-encode them into a
      ``*_json`` string property instead.
    """
    cleaned: dict[str, Any] = {}
    for key, value in properties.items():
        if key in reserved:
            raise ValueError(f"property {key!r} is reserved and must be set through its dedicated field")
        if value is None:
            continue
        if isinstance(value, _SCALARS):
            cleaned[key] = value
            continue
        if isinstance(value, (list, tuple)):
            cleaned[key] = _clean_list(key, value)
            continue
        raise TypeError(
            f"property {key!r} has unsupported type {type(value).__name__}; "
            "encode nested values as a JSON string"
        )
    return cleaned


def _clean_list(key: str, values: Iterable[Any]) -> list[Any]:
    items = list(values)
    if not items:
        return items
    kinds = {type(item) for item in items}
    if not all(issubclass(kind, _SCALARS) for kind in kinds):
        raise TypeError(f"list property {key!r} may only contain scalars; encode nested values as JSON")
    if kinds == {int, float}:
        return [float(item) for item in items]
    if len(kinds) > 1:
        raise TypeError(f"list property {key!r} mixes types {sorted(kind.__name__ for kind in kinds)}")
    return items


@dataclass(slots=True)
class GraphNode:
    """A node in the code knowledge graph."""

    id: str
    label: NodeLabel
    repository_id: str
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.properties = clean_properties(self.properties, reserved=RESERVED_NODE_KEYS)

    def get(self, key: str, default: Any = None) -> Any:
        return self.properties.get(key, default)

    @property
    def name(self) -> str:
        return str(self.properties.get("name", ""))

    @property
    def qualified_name(self) -> str:
        return str(self.properties.get("qualified_name") or self.properties.get("name", ""))

    @property
    def path(self) -> str | None:
        value = self.properties.get("path")
        return str(value) if value is not None else None

    @property
    def start_line(self) -> int | None:
        value = self.properties.get("start_line")
        return int(value) if value is not None else None

    @property
    def end_line(self) -> int | None:
        value = self.properties.get("end_line")
        return int(value) if value is not None else None

    @property
    def line(self) -> int | None:
        """Citation anchor: the line of the declared name.

        ``start_line`` can be earlier because a scope's extent includes its leading
        annotations / decorators.
        """
        value = self.properties.get("line")
        return int(value) if value is not None else self.start_line

    @property
    def location(self) -> str | None:
        """``basename:line`` citation key, e.g. ``OrderProcessor.java:214``."""
        if self.path is None or self.line is None:
            return None
        return f"{self.path.rsplit('/', 1)[-1]}:{self.line}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label.value,
            "repository_id": self.repository_id,
            **self.properties,
        }


@dataclass(slots=True)
class GraphEdge:
    """A directed, typed relationship. ``(source, type, target)`` is its identity."""

    source: str
    target: str
    type: RelType
    repository_id: str
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.properties = clean_properties(self.properties, reserved=RESERVED_EDGE_KEYS)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.type.value, self.target)

    @property
    def confidence(self) -> float:
        """Static-analysis confidence in ``[0, 1]``; edges without one are certain."""
        return float(self.properties.get("confidence", 1.0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "type": self.type.value,
            "repository_id": self.repository_id,
            **self.properties,
        }


@dataclass(slots=True)
class Hop:
    """One relationship followed from ``origin`` during an expansion.

    ``node`` is the endpoint that is *not* ``origin`` (for a self-loop it is the
    origin itself).
    """

    origin: str
    edge: GraphEdge
    node: GraphNode


@dataclass(slots=True)
class SubGraph:
    """Result of a bounded traversal."""

    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], GraphEdge] = field(default_factory=dict)
    #: Minimum number of hops from any start node.
    depths: dict[str, int] = field(default_factory=dict)
    #: True when ``max_nodes`` stopped the traversal before the frontier was exhausted.
    truncated: bool = False

    def merge(self, other: SubGraph) -> None:
        """Union ``other`` into this sub-graph, keeping the smaller depth per node."""
        for node_id, node in other.nodes.items():
            self.nodes.setdefault(node_id, node)
            depth = other.depths.get(node_id)
            if depth is not None and depth < self.depths.get(node_id, depth + 1):
                self.depths[node_id] = depth
        self.edges.update(other.edges)
        self.truncated = self.truncated or other.truncated

    def out_edges(self, node_id: str, rel_type: RelType | None = None) -> list[GraphEdge]:
        return [
            edge
            for edge in self.edges.values()
            if edge.source == node_id and (rel_type is None or edge.type == rel_type)
        ]

    def in_edges(self, node_id: str, rel_type: RelType | None = None) -> list[GraphEdge]:
        return [
            edge
            for edge in self.edges.values()
            if edge.target == node_id and (rel_type is None or edge.type == rel_type)
        ]


@dataclass(slots=True)
class GraphCounts:
    """Per-repository cardinalities by node label and relationship type."""

    nodes: dict[str, int] = field(default_factory=dict)
    edges: dict[str, int] = field(default_factory=dict)

    @property
    def total_nodes(self) -> int:
        return sum(self.nodes.values())

    @property
    def total_edges(self) -> int:
        return sum(self.edges.values())
