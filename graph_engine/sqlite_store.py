"""Embedded graph store backed by SQLite.

This is the zero-infrastructure backend used for local development, tests and
single-node deployments. It implements exactly the same primitive contract as
:class:`graph_engine.neo4j_store.Neo4jGraphStore`; traversal logic lives in the
shared :class:`graph_engine.store.GraphStore` base class.

Concurrency model: file-backed databases use WAL mode with one connection per
thread, so API readers are never blocked by an ingestion writer. In-memory
databases use a single connection serialised by a lock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from graph_engine.models import GraphCounts, GraphEdge, GraphNode, Hop
from graph_engine.schema import Direction, NodeLabel, RelType, is_safe_identifier
from graph_engine.store import FilterValue, GraphStore, GraphStoreError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id                   TEXT PRIMARY KEY,
    label                TEXT NOT NULL,
    repository_id        TEXT NOT NULL,
    name                 TEXT,
    name_lower           TEXT,
    qualified_name_lower TEXT,
    path                 TEXT,
    start_line           INTEGER,
    end_line             INTEGER,
    props                TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS nodes_repo_label ON nodes(repository_id, label);
CREATE INDEX IF NOT EXISTS nodes_repo_name  ON nodes(repository_id, name_lower);
CREATE INDEX IF NOT EXISTS nodes_repo_qname ON nodes(repository_id, qualified_name_lower);
CREATE INDEX IF NOT EXISTS nodes_repo_path  ON nodes(repository_id, path);
CREATE INDEX IF NOT EXISTS nodes_label      ON nodes(label);

CREATE TABLE IF NOT EXISTS edges (
    source        TEXT NOT NULL,
    type          TEXT NOT NULL,
    target        TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 1.0,
    props         TEXT NOT NULL,
    PRIMARY KEY (source, type, target)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS edges_target ON edges(target, type);
"""

_NODE_COLUMNS = "id, label, repository_id, props"
#: SQLite's default host-parameter limit is 32766; stay far below it.
_IN_CHUNK = 500
_WRITE_CHUNK = 2000

_UPSERT_NODE = """
INSERT INTO nodes (id, label, repository_id, name, name_lower, qualified_name_lower,
                   path, start_line, end_line, props)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    label = excluded.label,
    repository_id = excluded.repository_id,
    name = excluded.name,
    name_lower = excluded.name_lower,
    qualified_name_lower = excluded.qualified_name_lower,
    path = excluded.path,
    start_line = excluded.start_line,
    end_line = excluded.end_line,
    props = excluded.props
"""

# Mirrors Cypher's ``MATCH (a) MATCH (b) MERGE (a)-[r]->(b)``: an edge with a
# missing endpoint is skipped rather than creating a dangling reference.
_UPSERT_EDGE = """
INSERT INTO edges (source, type, target, repository_id, confidence, props)
SELECT ?, ?, ?, ?, ?, ?
WHERE EXISTS (SELECT 1 FROM nodes WHERE id = ?) AND EXISTS (SELECT 1 FROM nodes WHERE id = ?)
ON CONFLICT(source, type, target) DO UPDATE SET
    repository_id = excluded.repository_id,
    confidence = excluded.confidence,
    props = excluded.props
"""


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


def _lower(value: Any) -> str | None:
    return str(value).lower() if value is not None else None


def _node_from_row(row: Sequence[Any]) -> GraphNode:
    return GraphNode(id=row[0], label=NodeLabel(row[1]), repository_id=row[2], properties=json.loads(row[3]))


def _edge_from_row(row: Sequence[Any]) -> GraphEdge:
    return GraphEdge(
        source=row[0],
        type=RelType(row[1]),
        target=row[2],
        repository_id=row[3],
        properties=json.loads(row[4]),
    )


class SqliteGraphStore(GraphStore):
    """Property graph persisted in a single SQLite database file."""

    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        self._memory = str(path) == ":memory:"
        self._path = str(path)
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._closed = False
        if not self._memory:
            Path(self._path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._shared: sqlite3.Connection | None = self._open() if self._memory else None

    # ------------------------------------------------------------------ plumbing

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30.0, check_same_thread=False)
        if not self._memory:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
        with self._connections_lock:
            self._connections.append(connection)
        return connection

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise GraphStoreError("graph store is closed")
        if self._shared is not None:
            return self._shared
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._open()
            self._local.connection = connection
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        if self._memory:
            with self._write_lock:
                yield self._connection()
        else:
            yield self._connection()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self._connection()
            try:
                with connection:  # commits on success, rolls back on error
                    yield connection
            except sqlite3.Error as exc:
                raise GraphStoreError(f"sqlite write failed: {exc}") from exc

    def _query(self, sql: str, parameters: Sequence[Any] = ()) -> list[Any]:
        try:
            with self._read() as connection:
                return connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise GraphStoreError(f"sqlite query failed: {exc}") from exc

    # ------------------------------------------------------------------ lifecycle

    def ensure_schema(self) -> None:
        with self._write() as connection:
            connection.executescript(_SCHEMA)

    def ping(self) -> dict[str, Any]:
        version = self._query("SELECT sqlite_version()")[0][0]
        return {"backend": "sqlite", "version": version, "path": self._path}

    def close(self) -> None:
        self._closed = True
        with self._connections_lock:
            for connection in self._connections:
                try:
                    connection.close()
                except sqlite3.Error:  # pragma: no cover - best effort on shutdown
                    continue
            self._connections.clear()
        self._shared = None

    # --------------------------------------------------------------------- writes

    def upsert_nodes(self, nodes: Iterable[GraphNode]) -> int:
        rows = [
            (
                node.id,
                node.label.value,
                node.repository_id,
                node.properties.get("name"),
                _lower(node.properties.get("name")),
                _lower(node.properties.get("qualified_name")),
                node.properties.get("path"),
                node.properties.get("start_line"),
                node.properties.get("end_line"),
                json.dumps(node.properties, separators=(",", ":"), ensure_ascii=False),
            )
            for node in nodes
        ]
        if not rows:
            return 0
        with self._write() as connection:
            for chunk in _chunks(rows, _WRITE_CHUNK):
                connection.executemany(_UPSERT_NODE, chunk)
        return len(rows)

    def upsert_edges(self, edges: Iterable[GraphEdge]) -> int:
        rows = [
            (
                edge.source,
                edge.type.value,
                edge.target,
                edge.repository_id,
                edge.confidence,
                json.dumps(edge.properties, separators=(",", ":"), ensure_ascii=False),
                edge.source,
                edge.target,
            )
            for edge in edges
        ]
        if not rows:
            return 0
        written = 0
        with self._write() as connection:
            for chunk in _chunks(rows, _WRITE_CHUNK):
                before = connection.total_changes
                connection.executemany(_UPSERT_EDGE, chunk)
                written += connection.total_changes - before
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
        written = 0
        with self._write() as connection:
            for edge in edges:
                row = connection.execute(
                    "SELECT props FROM edges WHERE source = ? AND type = ? AND target = ?",
                    (edge.source, edge.type.value, edge.target),
                ).fetchone()
                if row is None:
                    merged = dict(edge.properties)
                else:
                    merged = json.loads(row[0])
                    merged.update({k: v for k, v in edge.properties.items() if k not in special})
                    for key in additive:
                        merged[key] = merged.get(key, 0) + edge.properties.get(key, 0)
                    for key in maximum:
                        incoming = edge.properties.get(key)
                        if (incoming or 0) > (merged.get(key) or 0):
                            merged[key] = incoming
                before = connection.total_changes
                connection.execute(
                    _UPSERT_EDGE,
                    (
                        edge.source,
                        edge.type.value,
                        edge.target,
                        edge.repository_id,
                        float(merged.get("confidence", 1.0)),
                        json.dumps(merged, separators=(",", ":"), ensure_ascii=False),
                        edge.source,
                        edge.target,
                    ),
                )
                written += connection.total_changes - before
        return written

    def delete_repository(self, repository_id: str, *, keep_labels: Collection[NodeLabel] = ()) -> None:
        keep = sorted(label.value for label in keep_labels)
        doomed = "SELECT id FROM nodes WHERE repository_id = ?"
        parameters: list[Any] = [repository_id]
        if keep:
            doomed += f" AND label NOT IN ({_placeholders(len(keep))})"
            parameters.extend(keep)
        with self._write() as connection:
            connection.execute(
                f"DELETE FROM edges WHERE source IN ({doomed}) OR target IN ({doomed})",  # noqa: S608
                parameters + parameters,
            )
            connection.execute(f"DELETE FROM nodes WHERE id IN ({doomed})", parameters)  # noqa: S608

    def delete_edges(self, repository_id: str, rel_types: Collection[RelType]) -> int:
        types = sorted(rel_type.value for rel_type in rel_types)
        if not types:
            return 0
        with self._write() as connection:
            cursor = connection.execute(
                f"""
                DELETE FROM edges
                WHERE type IN ({_placeholders(len(types))})
                  AND source IN (SELECT id FROM nodes WHERE repository_id = ?)
                """,  # noqa: S608
                [*types, repository_id],
            )
            return int(cursor.rowcount)

    # ---------------------------------------------------------------------- reads

    def get_nodes(self, node_ids: Sequence[str]) -> list[GraphNode]:
        unique = list(dict.fromkeys(node_ids))
        found: list[GraphNode] = []
        for chunk in _chunks(unique, _IN_CHUNK):
            rows = self._query(
                f"SELECT {_NODE_COLUMNS} FROM nodes WHERE id IN ({_placeholders(len(chunk))})",  # noqa: S608
                list(chunk),
            )
            found.extend(_node_from_row(row) for row in rows)
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
        clauses: list[str] = []
        parameters: list[Any] = []
        if repository_id is not None:
            clauses.append("repository_id = ?")
            parameters.append(repository_id)
        if labels is not None:
            values = sorted(label.value for label in labels)
            if not values:
                return []
            clauses.append(f"label IN ({_placeholders(len(values))})")
            parameters.extend(values)
        if name is not None:
            clauses.append("name_lower = ?")
            parameters.append(name.lower())
        if qualified_name is not None:
            clauses.append("qualified_name_lower = ?")
            parameters.append(qualified_name.lower())
        if path is not None:
            clauses.append("path = ?")
            parameters.append(path)
        for key, value in (where or {}).items():
            if not is_safe_identifier(key):
                raise ValueError(f"unsafe property name in filter: {key!r}")
            # json_extract yields 1/0 for JSON booleans, matching how sqlite3 binds bool.
            clauses.append(f"json_extract(props, '$.{key}') = ?")
            parameters.append(value)
        condition = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(
            f"SELECT {_NODE_COLUMNS} FROM nodes {condition} ORDER BY COALESCE(name, ''), id LIMIT ? OFFSET ?",  # noqa: S608
            [*parameters, limit, offset],
        )
        return [_node_from_row(row) for row in rows]

    def search_nodes(
        self,
        repository_id: str,
        text: str,
        *,
        labels: Collection[NodeLabel] | None = None,
        limit: int = 25,
    ) -> list[GraphNode]:
        needle = text.strip().lower()
        if not needle:
            return []
        clauses = [
            "repository_id = ?",
            "(instr(name_lower, ?) > 0 OR instr(COALESCE(qualified_name_lower, ''), ?) > 0)",
        ]
        parameters: list[Any] = [repository_id, needle, needle]
        if labels is not None:
            values = sorted(label.value for label in labels)
            if not values:
                return []
            clauses.append(f"label IN ({_placeholders(len(values))})")
            parameters.extend(values)
        rows = self._query(
            f"""
            SELECT {_NODE_COLUMNS} FROM nodes
            WHERE {" AND ".join(clauses)}
            ORDER BY CASE
                         WHEN name_lower = ? THEN 0
                         WHEN substr(name_lower, 1, length(?)) = ? THEN 1
                         ELSE 2
                     END,
                     length(name), name, id
            LIMIT ?
            """,  # noqa: S608
            [*parameters, needle, needle, needle, limit],
        )
        return [_node_from_row(row) for row in rows]

    def nodes_at(
        self,
        repository_id: str,
        path: str,
        line: int,
        *,
        labels: Collection[NodeLabel] | None = None,
    ) -> list[GraphNode]:
        clauses = ["repository_id = ?", "path = ?", "start_line <= ?", "end_line >= ?"]
        parameters: list[Any] = [repository_id, path, line, line]
        if labels is not None:
            values = sorted(label.value for label in labels)
            if not values:
                return []
            clauses.append(f"label IN ({_placeholders(len(values))})")
            parameters.extend(values)
        rows = self._query(
            f"""
            SELECT {_NODE_COLUMNS} FROM nodes
            WHERE {" AND ".join(clauses)}
            ORDER BY (end_line - start_line), start_line DESC, id
            """,  # noqa: S608
            parameters,
        )
        return [_node_from_row(row) for row in rows]

    def list_edges(self, repository_id: str, rel_types: Collection[RelType]) -> list[GraphEdge]:
        types = sorted(rel_type.value for rel_type in rel_types)
        if not types:
            return []
        rows = self._query(
            f"""
            SELECT e.source, e.type, e.target, e.repository_id, e.props
            FROM edges e JOIN nodes n ON n.id = e.source
            WHERE n.repository_id = ? AND e.type IN ({_placeholders(len(types))})
            """,  # noqa: S608
            [repository_id, *types],
        )
        return [_edge_from_row(row) for row in rows]

    def expand(
        self,
        node_ids: Sequence[str],
        rel_types: Collection[RelType],
        direction: Direction,
        *,
        min_confidence: float = 0.0,
    ) -> list[Hop]:
        types = sorted(rel_type.value for rel_type in rel_types)
        origins = list(dict.fromkeys(node_ids))
        if not types or not origins:
            return []
        hops: list[Hop] = []
        seen: set[tuple[str, tuple[str, str, str]]] = set()
        for outgoing in (True, False):
            if outgoing and direction is Direction.IN:
                continue
            if not outgoing and direction is Direction.OUT:
                continue
            origin_column, other_column = ("source", "target") if outgoing else ("target", "source")
            for chunk in _chunks(origins, _IN_CHUNK):
                rows = self._query(
                    f"""
                    SELECT e.source, e.type, e.target, e.repository_id, e.props,
                           n.id, n.label, n.repository_id, n.props
                    FROM edges e JOIN nodes n ON n.id = e.{other_column}
                    WHERE e.{origin_column} IN ({_placeholders(len(chunk))})
                      AND e.type IN ({_placeholders(len(types))})
                      AND e.confidence >= ?
                    """,  # noqa: S608
                    [*chunk, *types, min_confidence],
                )
                for row in rows:
                    edge = _edge_from_row(row[:5])
                    origin = edge.source if outgoing else edge.target
                    marker = (origin, edge.key)
                    if marker in seen:  # a self-loop is returned by both directions
                        continue
                    seen.add(marker)
                    hops.append(Hop(origin=origin, edge=edge, node=_node_from_row(row[5:])))
        return hops

    def counts(self, repository_id: str) -> GraphCounts:
        node_rows = self._query(
            "SELECT label, COUNT(*) FROM nodes WHERE repository_id = ? GROUP BY label", [repository_id]
        )
        edge_rows = self._query(
            """
            SELECT e.type, COUNT(*)
            FROM edges e JOIN nodes n ON n.id = e.source
            WHERE n.repository_id = ?
            GROUP BY e.type
            """,
            [repository_id],
        )
        return GraphCounts(
            nodes={row[0]: row[1] for row in node_rows}, edges={row[0]: row[1] for row in edge_rows}
        )
