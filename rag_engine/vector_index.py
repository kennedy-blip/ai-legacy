"""Hybrid (dense + sparse) vector index: shared types, the in-process backend and the factory.

Two interchangeable backends answer the same queries:

``MemoryVectorIndex``
    Pure Python and in process. Nothing is persisted: the application rebuilds it
    from the graph at start-up. It needs no extra package and suits repositories up
    to a few tens of thousands of scopes.

``QdrantVectorIndex`` (:mod:`rag_engine.qdrant_index`)
    Qdrant, either embedded in a directory or as a server. Persists vectors and
    scales past what brute force can serve.

Both rank by the same recipe: dense cosine candidates and sparse lexical candidates
(IDF weighted), fused by reciprocal rank.
"""

from __future__ import annotations

import importlib.util
import math
import threading
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from operator import mul
from typing import Any, Final, Literal

from rag_engine.embeddings import Embedder, sparse_vector

VectorBackend = Literal["auto", "memory", "qdrant"]

#: Reciprocal-rank-fusion constant, the value Qdrant uses: a candidate at 0-based rank r scores 1 / (r + 2).
_RRF_K: Final = 2
#: Each ranking contributes this many times the requested limit before fusion.
_PREFETCH_FACTOR: Final = 3


class VectorIndexError(RuntimeError):
    """The vector database rejected a request or is unreachable."""


@dataclass(slots=True)
class IndexDocument:
    """One embeddable scope."""

    node_id: str
    repository_id: str
    label: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VectorHit:
    node_id: str
    score: float
    label: str
    payload: dict[str, Any]


class VectorIndex(ABC):
    """Stores scope embeddings and answers hybrid similarity queries."""

    embedder: Embedder
    #: ``memory``, ``qdrant-embedded`` or ``qdrant``.
    backend: str
    #: False when the vectors live only in this process and must be rebuilt after a restart.
    persistent: bool = True

    @abstractmethod
    def ensure_collection(self) -> None:
        """Create the underlying storage when it does not exist yet."""

    @abstractmethod
    def ping(self) -> dict[str, Any]:
        """Backend description; raises :class:`VectorIndexError` when unreachable."""

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def upsert(self, documents: Sequence[IndexDocument]) -> int:
        """Embed and store ``documents``; an existing entry for the same node is replaced."""

    @abstractmethod
    def delete_repository(self, repository_id: str) -> None: ...

    @abstractmethod
    def count(self, repository_id: str) -> int: ...

    @abstractmethod
    def search(
        self,
        repository_id: str,
        query: str,
        *,
        limit: int = 12,
        labels: Collection[str] | None = None,
    ) -> list[VectorHit]:
        """Hybrid search: dense and sparse candidates fused by reciprocal rank."""


# ------------------------------------------------------------------ in-process backend

_sumprod: Callable[[Sequence[float], Sequence[float]], float] | None = getattr(math, "sumprod", None)


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    if _sumprod is not None:  # Python 3.12+: one C call instead of a Python-level loop
        return _sumprod(left, right)
    return float(sum(map(mul, left, right)))


@dataclass(slots=True)
class _Point:
    label: str
    dense: list[float]
    sparse: dict[int, float]
    payload: dict[str, Any]


class MemoryVectorIndex(VectorIndex):
    """Brute-force hybrid index held in process memory.

    Dense vectors are L2-normalised on write so cosine similarity is a dot product.
    Sparse scoring applies the same BM25-style IDF Qdrant uses, computed over the
    repository being searched.
    """

    backend = "memory"
    persistent = False

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self._lock = threading.RLock()
        self._points: dict[str, dict[str, _Point]] = {}
        #: Per repository: number of stored documents containing each sparse term.
        self._frequencies: dict[str, Counter[int]] = {}

    def ensure_collection(self) -> None:
        return None

    def ping(self) -> dict[str, Any]:
        with self._lock:
            points = sum(len(points) for points in self._points.values())
        return {"backend": self.backend, "points": points, "embedder": self.embedder.name}

    def close(self) -> None:
        with self._lock:
            self._points.clear()
            self._frequencies.clear()

    # ------------------------------------------------------------------- writes

    def upsert(self, documents: Sequence[IndexDocument]) -> int:
        if not documents:
            return 0
        try:
            dense = self.embedder.embed_documents([document.text for document in documents])
        except Exception as error:
            raise VectorIndexError(f"embedding failed: {error}") from error
        with self._lock:
            for document, vector in zip(documents, dense, strict=True):
                points = self._points.setdefault(document.repository_id, {})
                frequencies = self._frequencies.setdefault(document.repository_id, Counter())
                previous = points.get(document.node_id)
                if previous is not None:
                    frequencies.subtract(previous.sparse.keys())
                indices, values = sparse_vector(document.text)
                sparse = dict(zip(indices, values, strict=True))
                frequencies.update(sparse.keys())
                points[document.node_id] = _Point(
                    document.label,
                    _normalised(vector),
                    sparse,
                    {
                        **document.payload,
                        "node_id": document.node_id,
                        "repository_id": document.repository_id,
                        "label": document.label,
                    },
                )
        return len(documents)

    def delete_repository(self, repository_id: str) -> None:
        with self._lock:
            self._points.pop(repository_id, None)
            self._frequencies.pop(repository_id, None)

    # -------------------------------------------------------------------- reads

    def count(self, repository_id: str) -> int:
        with self._lock:
            return len(self._points.get(repository_id, ()))

    def search(
        self,
        repository_id: str,
        query: str,
        *,
        limit: int = 12,
        labels: Collection[str] | None = None,
    ) -> list[VectorHit]:
        if not query.strip() or limit <= 0:
            return []
        try:  # outside the lock: a remote embedder may take a while
            dense_query = _normalised(self.embedder.embed_query(query))
        except Exception as error:
            raise VectorIndexError(f"embedding failed: {error}") from error
        wanted = set(labels) if labels else None
        with self._lock:
            stored = self._points.get(repository_id, {})
            total = len(stored)
            candidates = [
                (node_id, point)
                for node_id, point in stored.items()
                if wanted is None or point.label in wanted
            ]
            frequencies = dict(self._frequencies.get(repository_id, {}))
        if not candidates:
            return []

        prefetch = limit * _PREFETCH_FACTOR
        by_meaning = sorted(
            ((_dot(dense_query, point.dense), node_id) for node_id, point in candidates),
            key=lambda item: (-item[0], item[1]),
        )[:prefetch]
        rankings = [[node_id for _, node_id in by_meaning]]

        indices, values = sparse_vector(query, query=True)
        if indices:
            weights = {
                index: value * _idf(total, frequencies.get(index, 0))
                for index, value in zip(indices, values, strict=True)
            }
            lexical = []
            for node_id, point in candidates:
                score = sum(
                    weight * point.sparse[index] for index, weight in weights.items() if index in point.sparse
                )
                if score > 0.0:
                    lexical.append((score, node_id))
            lexical.sort(key=lambda item: (-item[0], item[1]))
            rankings.append([node_id for _, node_id in lexical[:prefetch]])

        fused: dict[str, float] = {}
        for ranking in rankings:
            for rank, node_id in enumerate(ranking):
                fused[node_id] = fused.get(node_id, 0.0) + 1.0 / (rank + _RRF_K)
        best = sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [
            VectorHit(node_id, score, stored[node_id].label, dict(stored[node_id].payload))
            for node_id, score in best
        ]


def _normalised(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(_dot(vector, vector))
    if norm == 0.0:
        return [0.0] * len(vector)
    return [value / norm for value in vector]


def _idf(total: int, containing: int) -> float:
    return math.log((total - containing + 0.5) / (containing + 0.5) + 1.0)


# ---------------------------------------------------------------------------- factory


def qdrant_available() -> bool:
    return importlib.util.find_spec("qdrant_client") is not None


def create_vector_index(
    embedder: Embedder,
    *,
    backend: VectorBackend = "auto",
    location: str | None = None,
    api_key: str | None = None,
) -> VectorIndex:
    """Instantiate the configured backend.

    ``location`` is the URL of a Qdrant server, a directory for embedded Qdrant or
    ``":memory:"``. With ``backend="auto"`` a server URL always selects Qdrant; a
    directory selects embedded Qdrant when the ``qdrant-client`` package is installed
    and the in-process index otherwise.
    """
    is_server = bool(location) and str(location).startswith(("http://", "https://"))
    if backend == "memory" or (backend == "auto" and not is_server and not qdrant_available()):
        return MemoryVectorIndex(embedder)
    if not qdrant_available():
        raise VectorIndexError(
            "the Qdrant backend needs the qdrant-client package: pip install qdrant-client"
        )
    from rag_engine.qdrant_index import QdrantVectorIndex  # noqa: PLC0415 - optional dependency

    return QdrantVectorIndex(embedder, location=location or ":memory:", api_key=api_key)
