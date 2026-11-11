"""The in-process vector index, and its agreement with embedded Qdrant on the same documents."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from rag_engine.embeddings import HashingEmbedder
from rag_engine.vector_index import (
    IndexDocument,
    MemoryVectorIndex,
    VectorIndex,
    create_vector_index,
    qdrant_available,
)
from tests.conftest import Corpus

DOCUMENTS = [
    IndexDocument("fn_fee", "billing", "Function", "late fee calculation with grace period and cap"),
    IndexDocument("fn_pay", "billing", "Function", "apply payment to invoice and post ledger entry"),
    IndexDocument("fn_export", "billing", "Function", "export ledger rows to csv file page by page"),
    IndexDocument("cmt_fee", "billing", "Commit", "late fees rounded half even after auditor finding"),
    IndexDocument("fn_other", "warehouse", "Function", "late fee for overdue pallet storage"),
]


@pytest.fixture
def index() -> Iterator[MemoryVectorIndex]:
    built = MemoryVectorIndex(HashingEmbedder())
    built.upsert(DOCUMENTS)
    yield built
    built.close()


def _ids(index: VectorIndex, query: str, **options: object) -> list[str]:
    return [hit.node_id for hit in index.search("billing", query, **options)]  # type: ignore[arg-type]


def test_search_ranks_by_shared_vocabulary(index: MemoryVectorIndex) -> None:
    assert set(_ids(index, "late fee")[:2]) == {"fn_fee", "cmt_fee"}
    assert _ids(index, "export ledger csv")[0] == "fn_export"


def test_search_stays_inside_the_repository(index: MemoryVectorIndex) -> None:
    assert "fn_other" not in _ids(index, "late fee pallet storage")
    assert [hit.node_id for hit in index.search("warehouse", "late fee")] == ["fn_other"]
    assert index.search("unknown", "late fee") == []


def test_label_filter_and_limit(index: MemoryVectorIndex) -> None:
    assert _ids(index, "late fee", labels=["Commit"]) == ["cmt_fee"]
    assert len(_ids(index, "ledger", limit=1)) == 1
    assert _ids(index, "   ") == []


def test_upsert_replaces_and_delete_removes(index: MemoryVectorIndex) -> None:
    assert index.count("billing") == 4
    index.upsert([IndexDocument("fn_fee", "billing", "Function", "currency conversion table lookup")])
    assert index.count("billing") == 4
    assert _ids(index, "late fees rounded")[0] == "cmt_fee"
    assert _ids(index, "currency conversion")[0] == "fn_fee"

    index.delete_repository("billing")
    assert index.count("billing") == 0
    assert index.count("warehouse") == 1


def test_hits_carry_their_payload(index: MemoryVectorIndex) -> None:
    index.upsert(
        [IndexDocument("fn_x", "billing", "Function", "reconcile nightly totals", {"path": "Job.java"})]
    )
    hit = index.search("billing", "reconcile nightly")[0]
    assert (hit.node_id, hit.label, hit.payload["path"]) == ("fn_x", "Function", "Job.java")
    assert hit.payload["repository_id"] == "billing"


def test_auto_backend_without_a_server_never_needs_the_network() -> None:
    built = create_vector_index(HashingEmbedder(), backend="memory")
    assert isinstance(built, MemoryVectorIndex)
    assert built.ping()["backend"] == "memory"


@pytest.mark.skipif(not qdrant_available(), reason="qdrant-client is not installed")
def test_memory_index_agrees_with_embedded_qdrant(corpus: Corpus) -> None:
    """Both backends implement the same hybrid search; on the demo they must find the same code."""
    from rag_engine.indexer import RepositoryIndexer

    qdrant = create_vector_index(HashingEmbedder(), backend="qdrant", location=":memory:")
    try:
        qdrant.ensure_collection()
        RepositoryIndexer(corpus.store, qdrant).index_repository(corpus.repository_id, corpus.root)
        assert qdrant.count(corpus.repository_id) == corpus.index.count(corpus.repository_id)
        for query in (
            "late fee grace period",
            "idempotency guard gateway callback",
            "which code writes the ledger",
            "nightly reconciliation",
            "tax adjustment 2013",
        ):
            ours = [hit.node_id for hit in corpus.index.search(corpus.repository_id, query, limit=5)]
            theirs = [hit.node_id for hit in qdrant.search(corpus.repository_id, query, limit=5)]
            # Equal scores are ordered differently by the two implementations, so the
            # comparison allows neighbours to swap places.
            assert ours[0] in theirs[:2] and theirs[0] in ours[:2], f"{query}: {ours} vs {theirs}"
            assert len(set(ours) & set(theirs)) >= 4, f"{query}: {ours} vs {theirs}"
    finally:
        qdrant.close()
