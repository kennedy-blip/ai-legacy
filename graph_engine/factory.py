"""Construction of the configured :class:`GraphStore` backend."""

from __future__ import annotations

from typing import Literal

from graph_engine.store import GraphStore

GraphBackend = Literal["sqlite", "neo4j"]


def create_graph_store(
    backend: GraphBackend,
    *,
    sqlite_path: str = ":memory:",
    neo4j_uri: str = "bolt://localhost:7687",
    neo4j_user: str = "neo4j",
    neo4j_password: str = "",
    neo4j_database: str = "neo4j",
) -> GraphStore:
    """Instantiate a graph store. Backend drivers are imported lazily."""
    if backend == "sqlite":
        from graph_engine.sqlite_store import SqliteGraphStore  # noqa: PLC0415

        return SqliteGraphStore(sqlite_path)
    if backend == "neo4j":
        from graph_engine.neo4j_store import Neo4jGraphStore  # noqa: PLC0415

        return Neo4jGraphStore(neo4j_uri, neo4j_user, neo4j_password, database=neo4j_database)
    raise ValueError(f"unknown graph backend: {backend!r}")
