"""Portable export and import of one repository's graph.

A snapshot is a JSON-lines document: a header line, then one node or relationship
per line in a stable order, so two exports of the same graph are byte-identical.
Paths ending in ``.gz`` are gzip-compressed.

Server-side details stay behind: the checkout path is not exported (the importer
states where the sources live) and ingestion-job records are skipped. Snapshots let
a graph built where git and workers are available be served somewhere they are not.
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

from graph_engine.models import GraphEdge, GraphNode
from graph_engine.schema import RUNTIME_RELS, NodeLabel, RelType, make_id
from graph_engine.store import GraphStore

FORMAT: Final = "archaeo-graph-snapshot"
VERSION: Final = 1

#: Never exported: where the checkout lives on the exporting machine.
_PRIVATE_PROPERTIES: Final = frozenset({"workspace_path"})
#: Wall-clock values, left out of a reproducible export so identical graphs give identical files.
_VOLATILE_REPOSITORY: Final = frozenset({"ingested_at", "duration_seconds"})
_VOLATILE_STATS: Final = frozenset({"duration_seconds"})
_VOLATILE_RUNTIME: Final = frozenset({"first_seen", "last_seen"})
_SKIPPED_LABELS: Final = frozenset({NodeLabel.INGESTION_JOB})
_PAGE: Final = 500


class SnapshotError(ValueError):
    """The file is not a snapshot this version can read."""


@dataclass(slots=True)
class SnapshotSummary:
    repository_id: str
    name: str
    nodes: int
    edges: int


@contextmanager
def _open(path: Path, mode: str) -> Iterator[IO[str]]:
    if path.suffix == ".gz":
        # mtime=0 keeps the compressed bytes reproducible.
        with (
            gzip.GzipFile(path, mode + "b", mtime=0) as raw,
            io.TextIOWrapper(raw, encoding="utf-8", newline="\n") as handle,
        ):
            yield handle
    else:
        with path.open(mode, encoding="utf-8", newline="\n") as handle:
            yield handle


def _dump(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def _nodes(store: GraphStore, repository_id: str) -> Iterator[GraphNode]:
    for label in NodeLabel:
        if label in _SKIPPED_LABELS:
            continue
        offset = 0
        while True:
            page = store.find_nodes(repository_id, labels=[label], limit=_PAGE, offset=offset)
            yield from page
            if len(page) < _PAGE:
                break
            offset += _PAGE


def export_repository(
    store: GraphStore,
    repository_id: str,
    target: Path,
    *,
    include_runtime: bool = True,
    overrides: Mapping[str, Any] | None = None,
    reproducible: bool = False,
) -> SnapshotSummary:
    """Write the graph of ``repository_id`` to ``target``.

    ``overrides`` replaces properties of the repository node in the export, e.g. to
    record a portable ``source`` instead of a path on this machine (``None`` removes
    a property). ``include_runtime=False`` leaves out relationships observed by
    runtime telemetry. ``reproducible=True`` drops wall-clock values (when the
    ingestion ran and how long it took, when telemetry was first and last seen), so
    the file changes only when the graph does.
    """
    root_id = make_id(NodeLabel.REPOSITORY, repository_id)
    root = store.get_node(root_id)
    if root is None:
        raise LookupError(f"unknown repository: {repository_id}")

    nodes = sorted(_nodes(store, repository_id), key=lambda node: (node.label.value, node.id))
    rel_types = [rel for rel in RelType if include_runtime or rel not in RUNTIME_RELS]
    edges = sorted(store.list_edges(repository_id, rel_types), key=lambda edge: edge.key)

    target.parent.mkdir(parents=True, exist_ok=True)
    with _open(target, "w") as handle:
        handle.write(
            _dump(
                {
                    "kind": "header",
                    "format": FORMAT,
                    "version": VERSION,
                    "repository_id": repository_id,
                    "name": root.name,
                    "nodes": len(nodes),
                    "edges": len(edges),
                }
            )
        )
        for node in nodes:
            properties = {
                key: value for key, value in node.properties.items() if key not in _PRIVATE_PROPERTIES
            }
            if node.id == root_id:
                if reproducible:
                    properties = _without_clock(properties)
                properties.update(overrides or {})
                properties = {key: value for key, value in properties.items() if value is not None}
            handle.write(
                _dump({"kind": "node", "id": node.id, "label": node.label.value, "properties": properties})
            )
        for edge in edges:
            edge_properties = edge.properties
            if reproducible and edge.type in RUNTIME_RELS:
                edge_properties = {
                    key: value for key, value in edge_properties.items() if key not in _VOLATILE_RUNTIME
                }
            handle.write(
                _dump(
                    {
                        "kind": "edge",
                        "source": edge.source,
                        "target": edge.target,
                        "type": edge.type.value,
                        "properties": edge_properties,
                    }
                )
            )
    return SnapshotSummary(repository_id, root.name, len(nodes), len(edges))


def _without_clock(properties: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in properties.items() if key not in _VOLATILE_REPOSITORY}
    raw = cleaned.get("stats_json")
    if isinstance(raw, str):
        try:
            stats = json.loads(raw)
        except ValueError:
            return cleaned
        if isinstance(stats, dict):
            for key in _VOLATILE_STATS:
                stats.pop(key, None)
            cleaned["stats_json"] = json.dumps(stats, sort_keys=True)
    return cleaned


def read_header(source: Path) -> dict[str, Any]:
    """The header of a snapshot, validated."""
    with _open(source, "r") as handle:
        return _header(handle.readline(), source)


def _header(line: str, source: Path) -> dict[str, Any]:
    try:
        header = json.loads(line)
    except ValueError as error:
        raise SnapshotError(f"{source}: not a graph snapshot") from error
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise SnapshotError(f"{source}: not a graph snapshot")
    if header.get("version") != VERSION:
        raise SnapshotError(f"{source}: unsupported snapshot version {header.get('version')!r}")
    if not isinstance(header.get("repository_id"), str) or not header["repository_id"]:
        raise SnapshotError(f"{source}: snapshot header has no repository id")
    return header


def import_snapshot(
    store: GraphStore,
    source: Path,
    *,
    workspace_path: str | Path | None = None,
    replace: bool = True,
) -> SnapshotSummary:
    """Load a snapshot into ``store``.

    ``workspace_path`` is the directory holding the repository's files on this
    machine; without it the graph is complete but source text cannot be shown.
    ``replace`` removes an existing graph of the same repository first. The
    repository node is marked ``from_snapshot`` so clients can tell a loaded graph
    from one analysed on this instance.
    """
    with _open(source, "r") as handle:
        header = _header(handle.readline(), source)
        repository_id: str = header["repository_id"]
        root_id = make_id(NodeLabel.REPOSITORY, repository_id)
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        try:
            for number, line in enumerate(handle, start=2):
                if not line.strip():
                    continue
                record = json.loads(line)
                properties = dict(record.get("properties") or {})
                if record["kind"] == "node":
                    if record["id"] == root_id:
                        properties.pop("workspace_path", None)
                        properties["from_snapshot"] = True
                        if workspace_path is not None:
                            properties["workspace_path"] = str(workspace_path)
                    nodes.append(
                        GraphNode(record["id"], NodeLabel(record["label"]), repository_id, properties)
                    )
                elif record["kind"] == "edge":
                    edges.append(
                        GraphEdge(
                            record["source"],
                            record["target"],
                            RelType(record["type"]),
                            repository_id,
                            properties,
                        )
                    )
                else:
                    raise SnapshotError(f"{source}:{number}: unknown record kind {record['kind']!r}")
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, SnapshotError):
                raise
            raise SnapshotError(f"{source}:{number}: malformed record ({error})") from error

    if not any(node.id == root_id for node in nodes):
        raise SnapshotError(f"{source}: snapshot does not contain its repository node")
    if replace:
        store.delete_repository(repository_id)
    store.upsert_nodes(nodes)
    store.upsert_edges(edges)
    return SnapshotSummary(repository_id, str(header.get("name") or repository_id), len(nodes), len(edges))
