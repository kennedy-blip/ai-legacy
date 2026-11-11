"""HTTP-level fixtures: whole applications behind a test client.

Every test that uses one of these applications runs on the embedded SQLite store, and a second
time on Neo4j when ``ARCHAEO_TEST_NEO4J_URI`` points at a server. Neo4j outlives the process, so
that database must be empty when a module starts: the fixtures refuse to start otherwise, and
remove what they created when they finish.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.demo import DEMO_REPOSITORY_ID
from backend.main import API_PREFIX, create_app
from backend.settings import Settings
from graph_engine.schema import NodeLabel
from graph_engine.store import GraphStore

REPOSITORY = f"{API_PREFIX}/repositories/{DEMO_REPOSITORY_ID}"

NEO4J_URI = os.environ.get("ARCHAEO_TEST_NEO4J_URI")

GRAPH_BACKENDS: list[object] = [
    "sqlite",
    pytest.param(
        "neo4j",
        marks=[
            pytest.mark.neo4j,
            pytest.mark.skipif(not NEO4J_URI, reason="ARCHAEO_TEST_NEO4J_URI not set"),
        ],
    ),
]


def graph_settings(backend: str) -> dict[str, Any]:
    """The settings that select the graph store a test instance runs on."""
    if backend == "sqlite":
        return {}
    return {
        "graph_backend": "neo4j",
        "neo4j_uri": NEO4J_URI,
        "neo4j_user": os.environ.get("ARCHAEO_TEST_NEO4J_USER", "neo4j"),
        "neo4j_password": os.environ.get("ARCHAEO_TEST_NEO4J_PASSWORD", "neo4j"),
    }


def hosted_settings(data_dir: Path, backend: str = "sqlite") -> Settings:
    """What ``backend.vercel_app`` configures: read-only, demo from its snapshot, vectors in memory."""
    return Settings(
        data_dir=data_dir,
        read_only=True,
        demo_source="snapshot",
        vector_backend="memory",
        _env_file=None,
        **graph_settings(backend),
    )


def _owners(store: GraphStore) -> set[str]:
    """Ids of every repository an instance has recorded, including ones whose ingestion failed."""
    recorded = store.find_nodes(None, labels=[NodeLabel.REPOSITORY, NodeLabel.INGESTION_JOB], limit=10_000)
    return {node.repository_id for node in recorded}


@contextmanager
def running(settings: Settings) -> Iterator[TestClient]:
    """A started application; on Neo4j it starts from, and leaves behind, an empty database."""
    shared = settings.graph_backend == "neo4j"
    app = create_app(settings)
    with TestClient(app) as client:
        store: GraphStore = app.state.archaeo.container.store
        try:
            yield client
        finally:
            if shared:
                for repository_id in _owners(store):
                    store.delete_repository(repository_id)


def require_empty_database(backend: str) -> None:
    """Stop before touching a Neo4j database that already holds somebody's repositories."""
    if backend != "neo4j":
        return
    from graph_engine.factory import create_graph_store

    chosen = graph_settings(backend)
    store = create_graph_store(
        "neo4j",
        neo4j_uri=chosen["neo4j_uri"],
        neo4j_user=chosen["neo4j_user"],
        neo4j_password=chosen["neo4j_password"],
    )
    try:
        found = sorted(_owners(store))
    finally:
        store.close()
    if found:
        pytest.fail(
            f"the Neo4j database at ARCHAEO_TEST_NEO4J_URI already holds {len(found)} repositories "
            f"({', '.join(found[:3])}); these tests need a database of their own and delete nothing "
            "they did not create",
            pytrace=False,
        )


@pytest.fixture(scope="module", params=GRAPH_BACKENDS)
def hosted(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The instance the public demo runs: no git, no ingestion, graph loaded from the snapshot."""
    backend: str = request.param
    require_empty_database(backend)
    with running(hosted_settings(tmp_path_factory.mktemp("hosted"), backend)) as client:
        yield client
