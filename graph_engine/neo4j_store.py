"""Neo4j implementation of :class:`graph_engine.store.GraphStore`.

All Cypher lives in :mod:`graph_engine.cypher_queries`; this module only binds
parameters, batches writes and maps records back to graph value objects.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from typing import Any

from neo4j import Driver, GraphDatabase, RoutingControl
from neo4j.exceptions import DriverError, Neo4jError

from graph_engine import cypher_queries as cq
from graph_engine.models import GraphCounts, GraphEdge, GraphNode, Hop
from graph_engine.schema import Direction, NodeLabel, RelType
from graph_engine.store import FilterValue, GraphStore, GraphStoreError

_WRITE_BATCH = 2000
_READ_BATCH = 5000
_DELETE_BATCH = 10_000


def _batches(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _node_from_props(props: Mapping[str, Any]) -> GraphNode:
    properties = dict(props)
    node_id = properties.pop("id")
    label = NodeLabel(properties.pop("label"))
    repository_id = properties.pop("repository_id")
    for key in cq.INTERNAL_NODE_KEYS:
        properties.pop(key, None)
    return GraphNode(id=node_id, label=label, repository_id=repository_id, properties=properties)


def _edge_from_record(source: str, target: str, rel_type: str, props: Mapping[str, Any]) -> GraphEdge:
    properties = dict(props)
    repository_id = properties.pop("repository_id", "")
    return GraphEdge(
        source=source,
        target=target,
        type=RelType(rel_type),
        repository_id=repository_id,
        properties=properties,
    )


def _node_row(node: GraphNode) -> dict[str, Any]:
    props: dict[str, Any] = {
        **node.properties,
        "id": node.id,
        "label": node.label.value,
        "repository_id": node.repository_id,
    }
    name = node.properties.get("name")
    if name is not None:
        props["name_lower"] = str(name).lower()
    qualified_name = node.properties.get("qualified_name")
    if qualified_name is not None:
        props["qualified_name_lower"] = str(qualified_name).lower()
    return {"id": node.id, "props": props}


def _edge_props(edge: GraphEdge) -> dict[str, Any]:
    return {**edge.properties, "repository_id": edge.repository_id}


class Neo4jGraphStore(GraphStore):
    """Code knowledge graph persisted in Neo4j (5.x or later)."""

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        database: str = "neo4j",
        driver: Driver | None = None,
    ) -> None:
        self._database = database
        self._uri = uri
        self._driver: Driver = driver or GraphDatabase.driver(uri, auth=(user, password))

    # ------------------------------------------------------------------ plumbing

    def _run(
        self, query: str, parameters: Mapping[str, Any] | None = None, *, write: bool = False
    ) -> list[Any]:
        try:
            result = self._driver.execute_query(
                # Query text is assembled exclusively from schema enums and validated
                # identifiers in cypher_queries; every data value is a bound parameter.
                query,
                parameters_=dict(parameters or {}),
                database_=self._database,
                routing_=RoutingControl.WRITE if write else RoutingControl.READ,
            )
        except (Neo4jError, DriverError) as exc:
            raise GraphStoreError(f"neo4j query failed: {exc}") from exc
        return list(result.records)

    # ------------------------------------------------------------------ lifecycle

    def ensure_schema(self) -> None:
        for statement in cq.schema_statements():
            self._run(statement, write=True)

    def ping(self) -> dict[str, Any]:
        try:
            self._driver.verify_connectivity()
            info = self._driver.get_server_info()
        except (Neo4jError, DriverError) as exc:
            raise GraphStoreError(f"neo4j is unreachable: {exc}") from exc
        return {"backend": "neo4j", "version": info.agent, "uri": self._uri, "database": self._database}

    def close(self) -> None:
        self._driver.close()

    # --------------------------------------------------------------------- writes

    def upsert_nodes(self, nodes: Iterable[GraphNode]) -> int:
        by_label: dict[NodeLabel, list[dict[str, Any]]] = defaultdict(list)
        for node in nodes:
            by_label[node.label].append(_node_row(node))
        written = 0
        for label, rows in by_label.items():
            query = cq.upsert_nodes(label)
            for batch in _batches(rows, _WRITE_BATCH):
                records = self._run(query, {"rows": list(batch)}, write=True)
                written += int(records[0]["written"]) if records else 0
        return written

    def upsert_edges(self, edges: Iterable[GraphEdge]) -> int:
        by_type: dict[RelType, list[dict[str, Any]]] = defaultdict(list)
        for edge in edges:
            by_type[edge.type].append(
                {"source": edge.source, "target": edge.target, "props": _edge_props(edge)}
            )
        written = 0
        for rel_type, rows in by_type.items():
            query = cq.upsert_edges(rel_type)
            for batch in _batches(rows, _WRITE_BATCH):
                records = self._run(query, {"rows": list(batch)}, write=True)
                written += int(records[0]["written"]) if records else 0
        return written

    def accumulate_edges(
        self,
        edges: Iterable[GraphEdge],
        *,
        additive: Sequence[str],
        maximum: Sequence[str] = (),
        preserve: Sequence[str] = (),
    ) -> int:
        special = set(additive) | set(maximum) | set(preserve)
        by_type: dict[RelType, list[dict[str, Any]]] = defaultdict(list)
        for edge in edges:
            props = _edge_props(edge)
            by_type[edge.type].append(
                {
                    "source": edge.source,
                    "target": edge.target,
                    "props": props,
                    "overwrite": {key: value for key, value in props.items() if key not in special},
                }
            )
        written = 0
        for rel_type, rows in by_type.items():
            query = cq.accumulate_edges(rel_type, additive, maximum)
            for batch in _batches(rows, _WRITE_BATCH):
                records = self._run(query, {"rows": list(batch)}, write=True)
                written += int(records[0]["written"]) if records else 0
        return written

    def delete_repository(self, repository_id: str, *, keep_labels: Collection[NodeLabel] = ()) -> None:
        query = cq.delete_repository_batch()
        parameters = {
            "repository_id": repository_id,
            "keep": sorted(label.value for label in keep_labels),
            "batch": _DELETE_BATCH,
        }
        while True:
            records = self._run(query, parameters, write=True)
            if not records or int(records[0]["deleted"]) == 0:
                return

    def delete_edges(self, repository_id: str, rel_types: Collection[RelType]) -> int:
        if not rel_types:
            return 0
        query = cq.delete_edges_batch(rel_types)
        parameters = {"repository_id": repository_id, "batch": _DELETE_BATCH}
        total = 0
        while True:
            records = self._run(query, parameters, write=True)
            deleted = int(records[0]["deleted"]) if records else 0
            total += deleted
            if deleted == 0:
                return total

    # ---------------------------------------------------------------------- reads

    def get_nodes(self, node_ids: Sequence[str]) -> list[GraphNode]:
        unique = list(dict.fromkeys(node_ids))
        found: list[GraphNode] = []
        query = cq.get_nodes()
        for batch in _batches(unique, _READ_BATCH):
            found.extend(
                _node_from_props(record["props"]) for record in self._run(query, {"ids": list(batch)})
            )
        return found

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
        if labels is not None and not labels:
            return []
        filters = dict(where or {})
        query = cq.find_nodes(
            by_repository=repository_id is not None,
            by_labels=labels is not None,
            by_name=name is not None,
            by_qualified_name=qualified_name is not None,
            by_path=path is not None,
            where_keys=tuple(filters),
        )
        parameters: dict[str, Any] = {"limit": limit, "offset": offset}
        if repository_id is not None:
            parameters["repository_id"] = repository_id
        if labels is not None:
            parameters["labels"] = sorted(label.value for label in labels)
        if name is not None:
            parameters["name"] = name.lower()
        if qualified_name is not None:
            parameters["qualified_name"] = qualified_name.lower()
        if path is not None:
            parameters["path"] = path
        for key, value in filters.items():
            parameters[f"where_{key}"] = value
        return [_node_from_props(record["props"]) for record in self._run(query, parameters)]

    def search_nodes(
        self,
        repository_id: str,
        text: str,
        *,
        labels: Collection[NodeLabel] | None = None,
        limit: int = 25,
    ) -> list[GraphNode]:
        needle = text.strip().lower()
        if not needle or (labels is not None and not labels):
            return []
        parameters: dict[str, Any] = {"repository_id": repository_id, "needle": needle, "limit": limit}
        if labels is not None:
            parameters["labels"] = sorted(label.value for label in labels)
        records = self._run(cq.search_nodes(by_labels=labels is not None), parameters)
        return [_node_from_props(record["props"]) for record in records]

    def nodes_at(
        self,
        repository_id: str,
        path: str,
        line: int,
        *,
        labels: Collection[NodeLabel] | None = None,
    ) -> list[GraphNode]:
        if labels is not None and not labels:
            return []
        parameters: dict[str, Any] = {"repository_id": repository_id, "path": path, "line": line}
        if labels is not None:
            parameters["labels"] = sorted(label.value for label in labels)
        records = self._run(cq.nodes_at(by_labels=labels is not None), parameters)
        return [_node_from_props(record["props"]) for record in records]

    def list_edges(self, repository_id: str, rel_types: Collection[RelType]) -> list[GraphEdge]:
        if not rel_types:
            return []
        records = self._run(cq.list_edges(rel_types), {"repository_id": repository_id})
        return [
            _edge_from_record(record["source"], record["target"], record["type"], record["props"])
            for record in records
        ]

    def expand(
        self,
        node_ids: Sequence[str],
        rel_types: Collection[RelType],
        direction: Direction,
        *,
        min_confidence: float = 0.0,
    ) -> list[Hop]:
        origins = list(dict.fromkeys(node_ids))
        if not origins or not rel_types:
            return []
        directions = (Direction.OUT, Direction.IN) if direction is Direction.BOTH else (direction,)
        hops: list[Hop] = []
        seen: set[tuple[str, tuple[str, str, str]]] = set()
        for single in directions:
            query = cq.expand(rel_types, single)
            for batch in _batches(origins, _READ_BATCH):
                for record in self._run(query, {"ids": list(batch), "min_confidence": min_confidence}):
                    edge = _edge_from_record(
                        record["source"], record["target"], record["type"], record["props"]
                    )
                    marker = (record["origin"], edge.key)
                    if marker in seen:  # a self-loop is returned by both directions
                        continue
                    seen.add(marker)
                    hops.append(
                        Hop(origin=record["origin"], edge=edge, node=_node_from_props(record["node"]))
                    )
        return hops

    def counts(self, repository_id: str) -> GraphCounts:
        parameters = {"repository_id": repository_id}
        nodes = {record["key"]: int(record["total"]) for record in self._run(cq.count_nodes(), parameters)}
        edges = {record["key"]: int(record["total"]) for record in self._run(cq.count_edges(), parameters)}
        return GraphCounts(nodes=nodes, edges=edges)
