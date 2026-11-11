"""Fixtures for tests of whole instances: the HTTP API behind a test client, and the command line.

Each such test runs on the embedded SQLite store, and a second time on Neo4j when
``ARCHAEO_TEST_NEO4J_URI`` points at a server. A Neo4j database outlives the test process, so it
has to be empty when a test starts using it: the fixtures refuse to start otherwise, and remove
everything again when they finish.
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
from graph_engine.factory import create_graph_store
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


def _neo4j_settings() -> dict[str, Any]:
    return {
        "graph_backend": "neo4j",
        "neo4j_uri": NEO4J_URI,
        "neo4j_user": os.environ.get("ARCHAEO_TEST_NEO4J_USER", "neo4j"),
        "neo4j_password": os.environ.get("ARCHAEO_TEST_NEO4J_PASSWORD", "neo4j"),
    }


@contextmanager
def _neo4j() -> Iterator[GraphStore]:
    chosen = _neo4j_settings()
    store = create_graph_store(
        "neo4j",
        neo4j_uri=chosen["neo4j_uri"],
        neo4j_user=chosen["neo4j_user"],
        neo4j_password=chosen["neo4j_password"],
    )
    try:
        yield store
    finally:
        store.close()


def _owners(store: GraphStore) -> list[str]:
    """Ids of every repository an instance has recorded, including ones whose ingestion failed."""
    recorded = store.find_nodes(None, labels=[NodeLabel.REPOSITORY, NodeLabel.INGESTION_JOB], limit=10_000)
    return sorted({node.repository_id for node in recorded})


@contextmanager
def graph_database(backend: str) -> Iterator[dict[str, Any]]:
    """The settings that put an instance on ``backend``.

    A Neo4j database must be empty beforehand, so that nothing of anybody else's can be touched,
    and is emptied afterwards.
    """
    if backend == "sqlite":  # a file under the data directory of the instance
        yield {}
        return
    with _neo4j() as store:
        found = _owners(store)
    if found:
        pytest.fail(
            f"the Neo4j database at ARCHAEO_TEST_NEO4J_URI already holds {len(found)} repositories "
            f"({', '.join(found[:3])}). These tests need a database of their own.",
            pytrace=False,
        )
    try:
        yield _neo4j_settings()
    finally:
        with _neo4j() as store:
            for repository_id in _owners(store):
                store.delete_repository(repository_id)


@contextmanager
def running(settings: Settings) -> Iterator[TestClient]:
    """A started application, checked to be using the graph store that was asked for."""
    with TestClient(create_app(settings)) as client:
        graph = client.get(f"{API_PREFIX}/system").json()["components"]["graph"]
        assert (graph["status"], graph["backend"]) == ("ok", settings.graph_backend), graph
        yield client


def hosted_settings(data_dir: Path, **graph: Any) -> Settings:
    """What ``backend.vercel_app`` configures: read-only, demo from its snapshot, vectors in memory."""
    return Settings(
        data_dir=data_dir,
        read_only=True,
        demo_source="snapshot",
        vector_backend="memory",
        _env_file=None,
        **graph,
    )


@pytest.fixture(scope="module", params=GRAPH_BACKENDS)
def hosted(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The instance the public demo runs: no git, no ingestion, graph loaded from the snapshot."""
    with (
        graph_database(request.param) as graph,
        running(hosted_settings(tmp_path_factory.mktemp("hosted"), **graph)) as client,
    ):
        yield client
