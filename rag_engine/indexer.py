"""Builds the semantic index of a repository from its graph and source text."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

from graph_engine.models import GraphNode
from graph_engine.schema import NodeLabel
from graph_engine.store import GraphStore
from rag_engine.source import SourceReader
from rag_engine.vector_index import IndexDocument, VectorIndex

logger = logging.getLogger(__name__)

_PAGE: Final = 2000
_MAX_CODE_LINES: Final = 80
_MAX_TEXT: Final = 6000
#: Labels that receive a vector. Files are covered by their classes and functions.
INDEXED_LABELS: Final = (
    NodeLabel.FUNCTION,
    NodeLabel.CLASS,
    NodeLabel.DATABASE_TABLE,
    NodeLabel.DATABASE_QUERY,
    NodeLabel.COMMIT,
)


def _lines(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)


def describe(node: GraphNode, reader: SourceReader) -> str | None:
    """The text embedded for ``node``: what it is, what it says about itself, and its code."""
    label = node.label
    if label is NodeLabel.FUNCTION:
        if node.get("is_boilerplate"):
            return None
        route = None
        if node.get("is_entry_point"):
            target = f"{node.get('http_method') or ''} {node.get('route') or ''}".strip()
            route = f"entry point: {node.get('entry_kind')} {target}".strip()
        code = ""
        if node.path and node.start_line and node.end_line:
            code = reader.text(node.path, node.start_line, node.end_line, max_lines=_MAX_CODE_LINES)
        text = _lines(
            f"{node.get('kind') or 'function'} {node.qualified_name} in {node.path}",
            node.get("signature"),
            node.get("docstring"),
            route,
            code,
        )
    elif label is NodeLabel.CLASS:
        text = _lines(
            f"{node.get('kind') or 'class'} {node.qualified_name} in {node.path}",
            node.get("docstring"),
            "extends " + ", ".join(node.get("bases") or []) if node.get("bases") else None,
            "implements " + ", ".join(node.get("interfaces") or []) if node.get("interfaces") else None,
            "fields: " + ", ".join(node.get("fields") or []) if node.get("fields") else None,
            f"maps table {node.get('table')}" if node.get("table") else None,
        )
    elif label is NodeLabel.DATABASE_TABLE:
        text = _lines(
            f"database {node.get('kind') or 'table'} {node.name}",
            "columns: " + ", ".join(node.get("columns") or []) if node.get("columns") else None,
            node.get("ddl"),
        )
    elif label is NodeLabel.DATABASE_QUERY:
        text = _lines(
            f"SQL {node.get('operation') or 'statement'} in {node.get('function_name')} ({node.path})",
            node.get("sql"),
        )
    elif label is NodeLabel.COMMIT:
        tickets = node.get("tickets") or []
        text = _lines(
            f"commit {node.name} by {node.get('author_name')} on {str(node.get('authored_at') or '')[:10]}",
            node.get("message"),
            node.get("body"),
            "tickets: " + ", ".join(tickets) if tickets else None,
            node.get("pr_title"),
            node.get("pr_body"),
        )
    else:
        return None
    text = text.strip()
    return text[:_MAX_TEXT] if text else None


def _payload(node: GraphNode) -> dict[str, Any]:
    return {
        "name": node.qualified_name,
        "path": node.path,
        "line": node.line,
        "is_test": bool(node.get("is_test")),
    }


class RepositoryIndexer:
    """Implements :class:`ingestion.pipeline.Indexer` on top of a :class:`VectorIndex`."""

    def __init__(self, store: GraphStore, index: VectorIndex) -> None:
        self.store = store
        self.index = index

    def _nodes(self, repository_id: str) -> Iterator[GraphNode]:
        for label in INDEXED_LABELS:
            offset = 0
            while True:
                page = self.store.find_nodes(repository_id, labels=[label], limit=_PAGE, offset=offset)
                yield from page
                if len(page) < _PAGE:
                    break
                offset += _PAGE

    def index_repository(
        self,
        repository_id: str,
        source_root: Path | None,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Replace the vectors of ``repository_id``. Returns the number of scopes embedded."""
        reader = SourceReader(source_root)
        documents: list[IndexDocument] = []
        for node in self._nodes(repository_id):
            text = describe(node, reader)
            if text:
                documents.append(
                    IndexDocument(node.id, repository_id, node.label.value, text, _payload(node))
                )
        self.index.ensure_collection()
        self.index.delete_repository(repository_id)
        total = len(documents)
        written = 0
        for start in range(0, total, 256):
            written += self.index.upsert(documents[start : start + 256])
            if progress is not None:
                progress(written, total)
        logger.info("indexed %d scopes of %s", written, repository_id)
        return written
