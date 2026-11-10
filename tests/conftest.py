"""Shared fixtures.

The synthetic demo repository (``examples/demo-monolith``) doubles as the test
corpus: it is replayed into a real git repository once per session and ingested
through the real pipeline, so most tests exercise parse -> resolve -> graph ->
history exactly as production does.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from graph_engine.models import GraphNode
from graph_engine.schema import NodeLabel
from graph_engine.sqlite_store import SqliteGraphStore
from graph_engine.store import GraphStore
from ingestion.pipeline import IngestionOptions, IngestionPipeline, IngestionRequest, IngestionResult
from rag_engine.embeddings import HashingEmbedder
from rag_engine.indexer import RepositoryIndexer
from rag_engine.source import SourceReader
from rag_engine.vector_index import VectorIndex

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_DEMO = REPO_ROOT / "examples" / "demo-monolith" / "build_history.py"


def build_demo(target: Path) -> Path:
    subprocess.run([sys.executable, str(BUILD_DEMO), str(target), "--force"], check=True, capture_output=True)
    return target


@pytest.fixture(scope="session")
def demo_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The demo monolith as a git repository with its full replayed history."""
    return build_demo(tmp_path_factory.mktemp("demo") / "core-billing-monolith")


@dataclass
class Corpus:
    """An ingested repository with everything needed to query it."""

    store: GraphStore
    index: VectorIndex
    repository_id: str
    root: Path
    result: IngestionResult

    @property
    def reader(self) -> SourceReader:
        return SourceReader(self.root)

    def node(self, qualified_name: str, label: NodeLabel = NodeLabel.FUNCTION) -> GraphNode:
        found = self.store.find_nodes(self.repository_id, labels=[label], qualified_name=qualified_name)
        assert len(found) == 1, f"{qualified_name}: expected one {label.value}, found {len(found)}"
        return found[0]

    def function(self, qualified_name: str) -> GraphNode:
        return self.node(qualified_name)

    def table(self, name: str) -> GraphNode:
        found = self.store.find_nodes(self.repository_id, labels=[NodeLabel.DATABASE_TABLE], name=name)
        assert len(found) == 1, f"table {name}: found {len(found)}"
        return found[0]

    def commit(self, subject_fragment: str) -> GraphNode:
        commits = self.store.find_nodes(self.repository_id, labels=[NodeLabel.COMMIT], limit=500)
        found = [commit for commit in commits if subject_fragment in str(commit.get("message"))]
        assert len(found) == 1, f"commit {subject_fragment!r}: found {len(found)}"
        return found[0]


def ingest(source: Path, *, name: str | None = None, blame: bool = True) -> Corpus:
    store = SqliteGraphStore(":memory:")
    store.ensure_schema()
    index = VectorIndex(HashingEmbedder())
    options = IngestionOptions(
        workspace_dir=source.parent / "_workspace",
        allowed_local_roots=(source.parent,),
        blame=blame,
        parse_workers=0,
    )
    pipeline = IngestionPipeline(store, options, indexer=RepositoryIndexer(store, index))
    result = pipeline.run(IngestionRequest(source=str(source), name=name))
    return Corpus(store, index, result.repository_id, source, result)


@pytest.fixture(scope="session")
def corpus(demo_repo: Path) -> Iterator[Corpus]:
    """The ingested demo. Session-scoped: treat it as read-only."""
    built = ingest(demo_repo)
    yield built
    built.store.close()
    built.index.close()
