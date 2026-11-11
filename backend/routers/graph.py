from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status

from backend import serializers
from backend.deps import Repository, State, node_in_repository, source_reader
from graph_engine import algorithms, locator
from graph_engine.models import GraphNode
from graph_engine.schema import NodeLabel
from parsers.registry import language_for_path

router = APIRouter(tags=["graph"])

NodeId = Annotated[str, Query(min_length=3, max_length=64, pattern=r"^[a-z]+_[0-9a-f]+$")]
_SEARCHABLE = (
    NodeLabel.FUNCTION,
    NodeLabel.CLASS,
    NodeLabel.DATABASE_TABLE,
    NodeLabel.FILE,
    NodeLabel.DATABASE_QUERY,
    NodeLabel.COMMIT,
)
_MAX_SOURCE_LINES = 2000


def _labels(raw: str | None) -> list[NodeLabel]:
    if not raw:
        return list(_SEARCHABLE)
    try:
        return [NodeLabel(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown label: {error}") from error


@router.get("/repositories/{repository_id}/search")
def search(
    repository: Repository,
    state: State,
    q: str = Query(min_length=1, max_length=200),
    labels: str | None = Query(default=None, max_length=120, description="Comma-separated node labels"),
    semantic: bool = Query(default=False, description="Add semantic matches after name matches"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Find nodes by name, by ``File.ext:line`` location and optionally by meaning."""
    store, repository_id = state.container.store, repository.repository_id
    wanted = _labels(labels)
    results: dict[str, dict[str, Any]] = {}

    def add(node: GraphNode, match: str, score: float | None = None) -> None:
        if node.id not in results and len(results) < limit:
            results[node.id] = {**serializers.node_brief(node), "match": match, "score": score}

    location = locator.parse_location(q.strip())
    if location is not None:
        for place in locator.resolve_location(store, repository_id, location[0], location[1]):
            add(place.scope or place.file, "location")
    for node in store.search_nodes(repository_id, q.strip(), labels=wanted, limit=limit):
        add(node, "name")
    index = state.container.index
    if semantic and index is not None and len(results) < limit:
        hits = index.search(repository_id, q, limit=limit, labels=[label.value for label in wanted])
        found = {node.id: node for node in store.get_nodes([hit.node_id for hit in hits])}
        for hit in hits:
            if hit.node_id in found:
                add(found[hit.node_id], "semantic", round(hit.score, 4))
    return {"query": q, "results": list(results.values())}


@router.get("/repositories/{repository_id}/files")
def list_files(
    repository: Repository,
    state: State,
    limit: int = Query(default=2000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Parsed files, ordered by path, for a file explorer."""
    nodes = state.container.store.find_nodes(
        repository.repository_id, labels=[NodeLabel.FILE], limit=limit + 1, offset=offset
    )
    files = [
        {
            "id": node.id,
            "path": node.path,
            "language": node.get("language"),
            "lines": node.get("line_count"),
            "is_test": bool(node.get("is_test")),
            "parse_error": node.get("parse_error"),
        }
        for node in nodes[:limit]
    ]
    files.sort(key=lambda item: str(item["path"]))
    return {"files": files, "has_more": len(nodes) > limit}


@router.get("/repositories/{repository_id}/entry-points")
def entry_points(
    repository: Repository,
    state: State,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """Where execution enters the code base: HTTP handlers, mains, scheduled jobs, message consumers."""
    nodes = state.container.store.find_nodes(
        repository.repository_id, labels=[NodeLabel.FUNCTION], where={"is_entry_point": True}, limit=limit + 1
    )
    nodes = [node for node in nodes if not node.get("is_test")]
    nodes.sort(
        key=lambda node: (
            str(node.get("entry_kind") or ""),
            str(node.get("route") or ""),
            node.qualified_name,
        )
    )
    return {
        "entry_points": [serializers.node_brief(node) for node in nodes[:limit]],
        "has_more": len(nodes) > limit,
    }


@router.get("/repositories/{repository_id}/nodes/{node_id}")
def get_node(repository: Repository, node_id: str, state: State) -> dict[str, Any]:
    """A node with its neighbourhood: callers, callees, data access, history."""
    node_in_repository(state, repository, node_id)
    context = algorithms.node_context(state.container.store, node_id)
    if context is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")
    return serializers.node_context(context)


@router.get("/repositories/{repository_id}/nodes/{node_id}/source")
def node_source(
    repository: Repository,
    node_id: str,
    state: State,
    context: int = Query(default=0, ge=0, le=200, description="Extra lines before and after"),
) -> dict[str, Any]:
    """Source text of a node's line span."""
    node = node_in_repository(state, repository, node_id)
    path, start, end = node.path, node.start_line, node.end_line
    if node.label is NodeLabel.FILE:
        start, end = 1, _MAX_SOURCE_LINES
    if path is None or start is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this node has no source location")
    return _source(
        repository, path, max(start - context, 1), (end or start) + context, focus=(start, end or start)
    )


@router.get("/repositories/{repository_id}/source")
def file_source(
    repository: Repository,
    path: str = Query(min_length=1, max_length=1000),
    start: int = Query(default=1, ge=1),
    end: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Lines ``start..end`` of a repository file (at most 2000 lines per request)."""
    last = min(end or start + _MAX_SOURCE_LINES - 1, start + _MAX_SOURCE_LINES - 1)
    return _source(repository, path, start, last, focus=None)


def _source(
    repository: GraphNode, path: str, start: int, end: int, *, focus: tuple[int, int] | None
) -> dict[str, Any]:
    reader = source_reader(repository)
    if not reader.available:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the source checkout of this repository is no longer on the server; re-ingest it to read code",
        )
    lines = reader.lines(path)
    if lines is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found in the repository checkout")
    end = min(end, len(lines), start + _MAX_SOURCE_LINES - 1)
    return {
        "path": path,
        "language": language_for_path(path),
        "start_line": start,
        "end_line": end,
        "total_lines": len(lines),
        "focus": {"start_line": focus[0], "end_line": focus[1]} if focus else None,
        "text": "\n".join(lines[start - 1 : end]),
        "head_commit": repository.get("head_commit"),
    }


@router.get("/repositories/{repository_id}/call-chain")
def call_chain(
    repository: Repository,
    state: State,
    node_id: NodeId,
    direction: Literal["upstream", "downstream", "both"] = "both",
    depth: int = Query(default=3, ge=1, le=8),
    min_confidence: float = Query(default=algorithms.DEFAULT_MIN_CONFIDENCE, ge=0.0, le=1.0),
    include_db: bool = True,
    include_runtime: bool = False,
    max_nodes: int = Query(default=250, ge=10, le=1500),
) -> dict[str, Any]:
    """Callers and/or callees of a node up to ``depth`` hops, with the tables they touch."""
    node_in_repository(state, repository, node_id)
    chain = algorithms.call_chain(
        state.container.store,
        node_id,
        direction=direction,
        max_hops=depth,
        min_confidence=min_confidence,
        include_db=include_db,
        include_runtime=include_runtime,
        max_nodes=max_nodes,
    )
    if chain is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")
    return serializers.call_chain(chain)


@router.get("/repositories/{repository_id}/sequence")
def sequence(
    repository: Repository,
    state: State,
    node_id: NodeId,
    depth: int = Query(default=5, ge=1, le=10),
    max_steps: int = Query(default=80, ge=5, le=400),
    min_confidence: float = Query(default=algorithms.DEFAULT_MIN_CONFIDENCE, ge=0.0, le=1.0),
) -> dict[str, Any]:
    """Statically derived sequence of calls and data access starting at a function."""
    node = node_in_repository(state, repository, node_id)
    if node.label is not NodeLabel.FUNCTION:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "a sequence starts at a function")
    diagram = algorithms.sequence(
        state.container.store, node_id, max_depth=depth, max_steps=max_steps, min_confidence=min_confidence
    )
    if diagram is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")
    return serializers.sequence(diagram)


@router.get("/repositories/{repository_id}/lineage")
def lineage(
    repository: Repository,
    state: State,
    node_id: NodeId,
    depth: int = Query(default=6, ge=1, le=10),
    min_confidence: float = Query(default=algorithms.DEFAULT_MIN_CONFIDENCE, ge=0.0, le=1.0),
    max_nodes: int = Query(default=250, ge=10, le=1500),
) -> dict[str, Any]:
    """Data lineage: entry points -> logic -> queries -> tables -> trigger side effects."""
    node_in_repository(state, repository, node_id)
    result = algorithms.lineage(
        state.container.store, node_id, max_hops=depth, min_confidence=min_confidence, max_nodes=max_nodes
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")
    return serializers.lineage(result)


@router.get("/repositories/{repository_id}/topology")
def topology(
    repository: Repository,
    state: State,
    depth: int | None = Query(
        default=None, ge=1, le=8, description="Directory depth; automatic when omitted"
    ),
    max_groups: int = Query(default=40, ge=5, le=120),
) -> dict[str, Any]:
    """Module-level map of the system: directories, tables and the dependencies between them."""
    result = algorithms.topology(
        state.container.store, repository.repository_id, depth=depth, max_groups=max_groups
    )
    return serializers.topology(result)
