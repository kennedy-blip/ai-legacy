"""Abstract graph store.

Design note
-----------
Backends implement only *primitive* operations: batched upserts, exact-match
lookups and a **single-hop** :meth:`GraphStore.expand`. Everything multi-hop -
:meth:`GraphStore.traverse` and the retrieval algorithms built on top of it in
:mod:`graph_engine.algorithms` - is written once against this interface.

Two consequences follow:

* N-hop traversal runs as one frontier-batched query per hop (a breadth-first
  search with global node uniqueness). Cost is linear in the edges touched, which
  keeps latency predictable on hub nodes where variable-length path patterns
  (``-[:CALLS*1..N]->``) enumerate every distinct path.
* The embedded SQLite store and the Neo4j store cannot drift apart in traversal
  semantics, because neither implements them. A shared contract test-suite
  (``tests/integration/test_graph_store_contract.py``) pins the primitive
  behaviour of both.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import Any

from graph_engine.models import GraphCounts, GraphEdge, GraphNode, Hop, SubGraph
from graph_engine.schema import Direction, NodeLabel, RelType

NodeFilter = Callable[[GraphNode], bool]
EdgeFilter = Callable[[GraphEdge], bool]
#: Scalar property values usable in an equality filter.
FilterValue = str | int | bool


class GraphStoreError(RuntimeError):
    """Raised when the backing graph database cannot serve a request."""


class GraphStore(ABC):
    """Persistence interface for the code knowledge graph."""

    # ------------------------------------------------------------------ lifecycle

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create constraints and indexes. Must be idempotent."""

    @abstractmethod
    def ping(self) -> dict[str, Any]:
        """Return backend/health information or raise :class:`GraphStoreError`."""

    @abstractmethod
    def close(self) -> None:
        """Release connections."""

    # --------------------------------------------------------------------- writes

    @abstractmethod
    def upsert_nodes(self, nodes: Iterable[GraphNode]) -> int:
        """Insert or fully replace nodes by ``id``. Returns the number written."""

    @abstractmethod
    def upsert_edges(self, edges: Iterable[GraphEdge]) -> int:
        """Insert or fully replace edges by ``(source, type, target)``.

        Edges whose source or target node does not exist are skipped. Returns the
        number of edges actually written.
        """

    @abstractmethod
    def accumulate_edges(
        self,
        edges: Iterable[GraphEdge],
        *,
        additive: Sequence[str],
        maximum: Sequence[str] = (),
        preserve: Sequence[str] = (),
    ) -> int:
        """Merge edges with counter semantics (used for runtime telemetry).

        For an edge that already exists, properties named in ``additive`` are
        summed, those in ``maximum`` keep the larger value, those in ``preserve``
        keep their existing value, and every other property is overwritten. A new
        edge is created with the given properties as-is. Edges with a missing
        endpoint are skipped. Returns the number of edges written.
        """

    @abstractmethod
    def delete_repository(self, repository_id: str, *, keep_labels: Collection[NodeLabel] = ()) -> None:
        """Delete every node (and attached edge) of a repository except ``keep_labels``."""

    @abstractmethod
    def delete_edges(self, repository_id: str, rel_types: Collection[RelType]) -> int:
        """Delete every edge of the given types whose *source* node is in the repository.

        Nodes are untouched. Returns the number of edges removed.
        """

    # ---------------------------------------------------------------------- reads

    @abstractmethod
    def get_nodes(self, node_ids: Sequence[str]) -> list[GraphNode]:
        """Fetch nodes by id. Unknown ids are omitted; order is unspecified."""

    def get_node(self, node_id: str) -> GraphNode | None:
        found = self.get_nodes([node_id])
        return found[0] if found else None

    @abstractmethod
    def find_nodes(
        self,
        repository_id: str | None,
        *,
        labels: Collection[NodeLabel] | None = None,
        name: str | None = None,
        qualified_name: str | None = None,
        path: str | None = None,
        where: Mapping[str, FilterValue] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[GraphNode]:
        """Exact-match lookup, ordered by ``(name, id)``.

        ``name`` and ``qualified_name`` compare case-insensitively; ``path``
        compares exactly. ``where`` adds equality filters on arbitrary scalar
        properties (keys must be plain identifiers). ``repository_id=None``
        searches every repository.
        """

    @abstractmethod
    def search_nodes(
        self,
        repository_id: str,
        text: str,
        *,
        labels: Collection[NodeLabel] | None = None,
        limit: int = 25,
    ) -> list[GraphNode]:
        """Case-insensitive substring search over ``name`` and ``qualified_name``.

        Ordered by: exact name match, then name prefix match, then other matches;
        ties broken by shorter name, then name, then id.
        """

    @abstractmethod
    def nodes_at(
        self,
        repository_id: str,
        path: str,
        line: int,
        *,
        labels: Collection[NodeLabel] | None = None,
    ) -> list[GraphNode]:
        """Nodes in file ``path`` whose line span contains ``line``, innermost first."""

    @abstractmethod
    def list_edges(self, repository_id: str, rel_types: Collection[RelType]) -> list[GraphEdge]:
        """Every edge of the given types whose *source* node is in the repository."""

    @abstractmethod
    def expand(
        self,
        node_ids: Sequence[str],
        rel_types: Collection[RelType],
        direction: Direction,
        *,
        min_confidence: float = 0.0,
    ) -> list[Hop]:
        """Follow one hop from each of ``node_ids``.

        ``Direction.OUT`` follows edges whose source is the origin, ``IN`` those
        whose target is the origin and ``BOTH`` the union. Edges whose
        ``confidence`` property is below ``min_confidence`` are ignored (a missing
        confidence counts as 1.0).
        """

    @abstractmethod
    def counts(self, repository_id: str) -> GraphCounts:
        """Node counts by label and edge counts by type for one repository."""

    # ------------------------------------------------------------ derived queries

    def traverse(
        self,
        start_ids: Sequence[str],
        rel_types: Collection[RelType],
        *,
        direction: Direction = Direction.OUT,
        max_hops: int = 4,
        min_confidence: float = 0.0,
        max_nodes: int = 2000,
        node_filter: NodeFilter | None = None,
        edge_filter: EdgeFilter | None = None,
    ) -> SubGraph:
        """Breadth-first N-hop traversal with global node uniqueness.

        Returns the sub-graph *induced* on the reached nodes for ``rel_types``:
        every qualifying edge between two reached nodes is included, not just the
        BFS tree, so cycles and cross edges are visible to callers.

        ``node_filter`` decides whether a newly discovered node is admitted (start
        nodes always are); a rejected node is neither returned nor expanded.
        ``edge_filter`` decides whether an edge may be followed at all.
        """
        if max_hops < 0:
            raise ValueError("max_hops must be >= 0")
        result = SubGraph()
        if not start_ids or not rel_types:
            for node in self.get_nodes(list(dict.fromkeys(start_ids))):
                result.nodes[node.id] = node
                result.depths[node.id] = 0
            return result

        for node in sorted(self.get_nodes(list(dict.fromkeys(start_ids))), key=lambda n: n.id):
            result.nodes[node.id] = node
            result.depths[node.id] = 0

        frontier = list(result.nodes)
        for depth in range(1, max_hops + 1):
            if not frontier:
                break
            next_frontier: list[str] = []
            for hop in self._ordered_hops(frontier, rel_types, direction, min_confidence):
                if edge_filter is not None and not edge_filter(hop.edge):
                    continue
                neighbour = hop.node
                if neighbour.id not in result.nodes:
                    if node_filter is not None and not node_filter(neighbour):
                        continue
                    if len(result.nodes) >= max_nodes:
                        result.truncated = True
                        continue
                    result.nodes[neighbour.id] = neighbour
                    result.depths[neighbour.id] = depth
                    next_frontier.append(neighbour.id)
                result.edges[hop.edge.key] = hop.edge
            frontier = next_frontier

        # ``frontier`` now holds the nodes discovered on the final hop. Their edges to
        # other *already reached* nodes belong to the induced sub-graph, so close it.
        if frontier:
            for hop in self._ordered_hops(frontier, rel_types, direction, min_confidence):
                if hop.node.id not in result.nodes:
                    continue
                if edge_filter is not None and not edge_filter(hop.edge):
                    continue
                result.edges[hop.edge.key] = hop.edge
        return result

    def _ordered_hops(
        self,
        frontier: Sequence[str],
        rel_types: Collection[RelType],
        direction: Direction,
        min_confidence: float,
    ) -> list[Hop]:
        """Expand a frontier and return hops in a backend-independent order.

        A deterministic order makes ``max_nodes`` truncation reproducible: the same
        query always keeps the same nodes, whichever store answers it.
        """
        hops = self.expand(frontier, rel_types, direction, min_confidence=min_confidence)
        hops.sort(key=lambda h: (h.edge.type.value, h.node.name, h.node.id, h.origin))
        return hops
