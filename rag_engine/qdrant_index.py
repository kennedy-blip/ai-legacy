"""Qdrant backend of the vector index (embedded directory or server).

Imported lazily by :func:`rag_engine.vector_index.create_vector_index`, so the
``qdrant-client`` package is only needed when this backend is selected.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Final

from qdrant_client import QdrantClient, models

from rag_engine.embeddings import Embedder, sparse_vector
from rag_engine.vector_index import IndexDocument, VectorHit, VectorIndex, VectorIndexError

_NAMESPACE: Final = uuid.UUID("6f1d7a52-0b6e-4c57-9a55-2f3f0c6f8a11")
_DENSE: Final = "dense"
_SPARSE: Final = "lexical"
_UPSERT_BATCH: Final = 128


def point_id(node_id: str) -> str:
    """Qdrant point ids must be UUIDs or integers; derive one deterministically."""
    return str(uuid.uuid5(_NAMESPACE, node_id))


def collection_name(prefix: str, embedder: Embedder) -> str:
    """One collection per vector space, so switching the embedder never mixes vectors."""
    slug = re.sub(r"[^a-z0-9]+", "_", embedder.name.lower()).strip("_")[:40]
    digest = hashlib.blake2b(embedder.name.encode(), digest_size=3).hexdigest()
    return f"{prefix}_{slug}_{digest}"


class QdrantVectorIndex(VectorIndex):
    """Vector index on Qdrant.

    ``location`` is ``":memory:"``, a directory for Qdrant's embedded (single
    process) mode, or the URL of a Qdrant server. Embedded mode is not safe for
    concurrent use, so every operation on it is serialised here.
    """

    def __init__(
        self,
        embedder: Embedder,
        *,
        location: str = ":memory:",
        api_key: str | None = None,
        prefix: str = "archaeo",
        client: QdrantClient | None = None,
    ) -> None:
        self.embedder = embedder
        self.location = location
        self.is_server = location.startswith(("http://", "https://"))
        self.backend = "qdrant" if self.is_server else "qdrant-embedded"
        if client is not None:
            self._client = client
        elif self.is_server:
            self._client = QdrantClient(url=location, api_key=api_key or None, timeout=30)
        elif location == ":memory:":
            self._client = QdrantClient(location=":memory:")
        else:
            self._client = QdrantClient(path=location)
        self._lock = threading.RLock()
        self._prefix = prefix
        self._collection: str | None = None
        self._ready = False

    # ---------------------------------------------------------------- lifecycle

    @contextmanager
    def _guard(self) -> Iterator[QdrantClient]:
        if self.is_server:
            yield self._client
        else:
            with self._lock:
                yield self._client

    @property
    def collection(self) -> str:
        if self._collection is None:
            self._collection = collection_name(self._prefix, self.embedder)
        return self._collection

    def ensure_collection(self) -> None:
        if self._ready:
            return
        try:
            with self._guard() as client:
                if not client.collection_exists(self.collection):
                    client.create_collection(
                        self.collection,
                        vectors_config={
                            _DENSE: models.VectorParams(
                                size=self.embedder.dimension, distance=models.Distance.COSINE
                            )
                        },
                        sparse_vectors_config={
                            _SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)
                        },
                    )
                    if self.is_server:  # payload indexes are a no-op (and warn) in embedded mode
                        for key in ("repository_id", "label"):
                            client.create_payload_index(
                                self.collection, field_name=key, field_schema=models.PayloadSchemaType.KEYWORD
                            )
        except Exception as error:
            raise VectorIndexError(f"cannot prepare vector collection: {error}") from error
        self._ready = True

    def ping(self) -> dict[str, Any]:
        try:
            with self._guard() as client:
                collections = client.get_collections().collections
        except Exception as error:
            raise VectorIndexError(f"vector database unreachable: {error}") from error
        return {
            "backend": self.backend,
            "collections": len(collections),
            "embedder": self.embedder.name,
        }

    def close(self) -> None:
        with self._lock:
            self._client.close()

    # ------------------------------------------------------------------- writes

    def upsert(self, documents: Sequence[IndexDocument]) -> int:
        """Embed and store ``documents``; an existing point for the same node is replaced."""
        if not documents:
            return 0
        self.ensure_collection()
        written = 0
        for start in range(0, len(documents), _UPSERT_BATCH):
            batch = documents[start : start + _UPSERT_BATCH]
            dense = self.embedder.embed_documents([document.text for document in batch])
            points = []
            for document, vector in zip(batch, dense, strict=True):
                indices, values = sparse_vector(document.text)
                points.append(
                    models.PointStruct(
                        id=point_id(document.node_id),
                        vector={_DENSE: vector, _SPARSE: models.SparseVector(indices=indices, values=values)},
                        payload={
                            **document.payload,
                            "node_id": document.node_id,
                            "repository_id": document.repository_id,
                            "label": document.label,
                        },
                    )
                )
            try:
                with self._guard() as client:
                    client.upsert(self.collection, points=points, wait=True)
            except Exception as error:
                raise VectorIndexError(f"vector upsert failed: {error}") from error
            written += len(points)
        return written

    def delete_repository(self, repository_id: str) -> None:
        try:
            with self._guard() as client:
                if not client.collection_exists(self.collection):
                    return
                client.delete(
                    self.collection,
                    points_selector=models.FilterSelector(filter=_filter(repository_id, None)),
                    wait=True,
                )
        except Exception as error:
            raise VectorIndexError(f"vector delete failed: {error}") from error

    # -------------------------------------------------------------------- reads

    def count(self, repository_id: str) -> int:
        try:
            with self._guard() as client:
                if not client.collection_exists(self.collection):
                    return 0
                return int(
                    client.count(self.collection, count_filter=_filter(repository_id, None), exact=True).count
                )
        except Exception as error:
            raise VectorIndexError(f"vector count failed: {error}") from error

    def search(
        self,
        repository_id: str,
        query: str,
        *,
        limit: int = 12,
        labels: Collection[str] | None = None,
    ) -> list[VectorHit]:
        """Hybrid search: dense and sparse candidates fused by reciprocal rank."""
        if not query.strip():
            return []
        self.ensure_collection()
        scope = _filter(repository_id, labels)
        indices, values = sparse_vector(query, query=True)
        prefetch = [
            models.Prefetch(
                query=self.embedder.embed_query(query), using=_DENSE, limit=limit * 3, filter=scope
            )
        ]
        if indices:
            prefetch.append(
                models.Prefetch(
                    query=models.SparseVector(indices=indices, values=values),
                    using=_SPARSE,
                    limit=limit * 3,
                    filter=scope,
                )
            )
        try:
            with self._guard() as client:
                response = client.query_points(
                    self.collection,
                    prefetch=prefetch,
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    limit=limit,
                    with_payload=True,
                )
        except Exception as error:
            raise VectorIndexError(f"vector search failed: {error}") from error
        hits: list[VectorHit] = []
        for point in response.points:
            payload = dict(point.payload or {})
            node_id = str(payload.get("node_id") or "")
            if node_id:
                hits.append(VectorHit(node_id, float(point.score), str(payload.get("label") or ""), payload))
        return hits


def _filter(repository_id: str, labels: Collection[str] | None) -> models.Filter:
    must: list[Any] = [
        models.FieldCondition(key="repository_id", match=models.MatchValue(value=repository_id))
    ]
    if labels:
        must.append(models.FieldCondition(key="label", match=models.MatchAny(any=sorted(labels))))
    return models.Filter(must=must)
