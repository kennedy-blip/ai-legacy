"""Wires settings into concrete services. One container per process (API server or worker)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.settings import Settings
from graph_engine.factory import create_graph_store
from graph_engine.store import GraphStore
from ingestion.pipeline import IngestionOptions, IngestionPipeline
from rag_engine.embeddings import Embedder, HashingEmbedder, OpenAICompatibleEmbedder
from rag_engine.engine import RagEngine
from rag_engine.indexer import RepositoryIndexer
from rag_engine.llm import LlmConfigurationError, build_chat_model
from rag_engine.vector_index import VectorIndex, create_vector_index

logger = logging.getLogger(__name__)


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "openai":
        key = settings.embedding_api_key or settings.llm_api_key
        return OpenAICompatibleEmbedder(
            settings.embedding_model,
            api_key=key.get_secret_value() if key else None,
            base_url=settings.embedding_base_url,
            dimension=settings.embedding_dimension,
        )
    return HashingEmbedder(settings.embedding_dimension or 384)


@dataclass(slots=True)
class Container:
    settings: Settings
    store: GraphStore
    index: VectorIndex | None
    rag: RagEngine
    pipeline: IngestionPipeline
    #: Problems found while wiring that do not prevent start-up (shown by ``/health``).
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, settings: Settings) -> Container:
        warnings: list[str] = []
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if settings.graph_backend == "sqlite":
            settings.graph_path.parent.mkdir(parents=True, exist_ok=True)
        store = create_graph_store(
            settings.graph_backend,
            sqlite_path=str(settings.graph_path),
            neo4j_uri=settings.neo4j_uri,
            neo4j_user=settings.neo4j_user,
            neo4j_password=settings.neo4j_password.get_secret_value(),
            neo4j_database=settings.neo4j_database,
        )
        store.ensure_schema()

        index: VectorIndex | None = None
        if settings.vector_enabled:
            try:
                index = create_vector_index(
                    build_embedder(settings),
                    backend=settings.vector_backend,
                    location=settings.qdrant_location,
                    api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
                )
                if not index.persistent and settings.embedding_provider != "hashing":
                    warnings.append(
                        "the in-process vector index re-embeds every repository at start-up; "
                        "configure Qdrant to persist vectors from a paid embedding model"
                    )
            except Exception as error:  # a broken vector store must not take the graph API down
                logger.exception("vector index unavailable")
                warnings.append(f"semantic index disabled: {error.__class__.__name__}: {str(error)[:200]}")

        llm = None
        config = settings.llm_config()
        try:
            llm = build_chat_model(config)
        except LlmConfigurationError as error:
            logger.error("language model disabled: %s", error)
            warnings.append(f"language model disabled: {error}")

        options = IngestionOptions(
            workspace_dir=settings.workspace_path,
            allowed_local_roots=settings.local_roots,
            allowed_git_hosts=tuple(settings.allowed_git_hosts),
            max_files=settings.max_files,
            max_file_bytes=settings.max_file_bytes,
            max_commits=settings.max_commits,
            blame=settings.blame,
            blame_budget_seconds=settings.blame_budget_seconds,
            github_enrichment=settings.github_enrichment,
            github_token=settings.github_token.get_secret_value() if settings.github_token else None,
        )
        indexer = RepositoryIndexer(store, index) if index is not None else None
        return cls(
            settings=settings,
            store=store,
            index=index,
            rag=RagEngine(store, index, llm=llm, llm_label=config.label),
            pipeline=IngestionPipeline(store, options, indexer=indexer),
            warnings=warnings,
        )

    def health(self) -> dict[str, Any]:
        """Component status; never raises."""
        components: dict[str, Any] = {}
        try:
            ping = self.store.ping()
            # Where the database lives is nobody's business but the operator's.
            for private in ("path", "uri", "database"):
                ping.pop(private, None)
            components["graph"] = {"status": "ok", **ping}
        except Exception as error:
            components["graph"] = {"status": "error", "error": str(error)[:300]}
        if self.index is None:
            components["vectors"] = {"status": "disabled"}
        else:
            try:
                components["vectors"] = {"status": "ok", **self.index.ping()}
            except Exception as error:
                components["vectors"] = {"status": "error", "error": str(error)[:300]}
        components["llm"] = {
            "status": "ok" if self.rag.llm is not None else "disabled",
            "mode": self.rag.mode,
            "model": self.rag.llm_label,
        }
        return components

    def close(self) -> None:
        for closer in (self.store.close, self.index.close if self.index is not None else None):
            if closer is not None:
                try:
                    closer()
                except Exception:
                    logger.warning("error while closing a store", exc_info=True)
