"""Conversion of graph objects into JSON payloads."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Final

from backend.jobs import JobRecord
from graph_engine.algorithms import (
    CallChain,
    Lineage,
    Neighbour,
    NodeContext,
    SequenceDiagram,
    Topology,
)
from graph_engine.models import GraphCounts, GraphEdge, GraphNode, SubGraph

#: Server-side details that are never sent to clients.
_PRIVATE: Final = frozenset({"workspace_path"})
#: Properties included in lists, search results and graph views. The node endpoint returns everything.
_BRIEF: Final = (
    # identity and location
    "name", "qualified_name", "kind", "language", "path", "line", "start_line", "end_line", "signature",
    "class_id", "class_name", "file_id", "directory", "line_count",
    # what the views colour and filter by
    "is_entry_point", "entry_kind", "http_method", "route", "is_test", "is_boilerplate", "dead_candidate",
    "complexity", "loc", "fan_in", "fan_out", "query_count",
    # history
    "last_modified_at", "last_author", "primary_author", "author_count",
    # tables and queries
    "declared", "operation", "dynamic", "tables_read", "tables_written", "function_id", "function_name",
    # commits
    "hash", "message", "authored_at", "author_name", "pr_number", "pr_title", "pr_url", "tickets",
)  # fmt: skip


def node_brief(node: GraphNode) -> dict[str, Any]:
    """Node without bulky properties - for lists, search results and graph views."""
    payload: dict[str, Any] = {"id": node.id, "label": node.label.value}
    properties = node.properties
    for key in _BRIEF:
        if key in properties:
            payload[key] = properties[key]
    payload["location"] = node.location
    return payload


def node_full(node: GraphNode) -> dict[str, Any]:
    payload = node.to_dict()
    for key in _PRIVATE:
        payload.pop(key, None)
    for key in [key for key in payload if key.endswith("_json")]:
        try:
            payload[key.removesuffix("_json")] = json.loads(payload.pop(key))
        except (TypeError, ValueError):
            payload.pop(key, None)
    payload["location"] = node.location
    return payload


def edge(item: GraphEdge) -> dict[str, Any]:
    payload = item.to_dict()
    payload["id"] = "|".join(item.key)
    payload["confidence"] = item.confidence
    return payload


def subgraph(graph: SubGraph) -> dict[str, Any]:
    return {
        "nodes": [node_brief(node) for node in graph.nodes.values()],
        "edges": [edge(item) for item in graph.edges.values()],
        "truncated": graph.truncated,
    }


def neighbour(item: Neighbour) -> dict[str, Any]:
    payload = edge(item.edge)
    return {"node": node_brief(item.node), "edge": payload}


#: Shown as the source of the bundled demo instead of the server path of its generated checkout.
DEMO_SOURCE_LABEL: Final = "examples/demo-monolith"


def repository(node: GraphNode, counts: GraphCounts | None = None, *, detail: bool = False) -> dict[str, Any]:
    is_demo = bool(node.get("is_demo"))
    payload: dict[str, Any] = {
        "id": node.repository_id,
        "name": node.name,
        "source": DEMO_SOURCE_LABEL if is_demo else node.get("source"),
        "source_kind": "bundled" if is_demo else node.get("source_kind"),
        "branch": node.get("branch"),
        "head_commit": node.get("head_commit"),
        "status": node.get("status"),
        "ingested_at": node.get("ingested_at"),
        "languages": node.get("languages") or [],
        "files": node.get("file_count", 0),
        "functions": node.get("function_count", 0),
        "classes": node.get("class_count", 0),
        "tables": node.get("table_count", 0),
        "lines": node.get("line_count", 0),
        "is_demo": is_demo,
    }
    if detail:
        payload["warnings"] = node.get("warnings") or []
        for key in ("stats", "insights"):
            raw = node.get(f"{key}_json")
            payload[key] = json.loads(raw) if raw else {}
    if counts is not None:
        payload["graph"] = {
            "nodes": counts.nodes,
            "edges": counts.edges,
            "total_nodes": counts.total_nodes,
            "total_edges": counts.total_edges,
        }
    return payload


def job(record: JobRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["warnings"] = list(record.warnings)
    payload["active"] = record.active
    return payload


def call_chain(chain: CallChain) -> dict[str, Any]:
    payload = subgraph(chain.graph)
    payload.update(root=node_brief(chain.root), levels=chain.levels, start_ids=chain.start_ids)
    return payload


def lineage(result: Lineage) -> dict[str, Any]:
    payload = subgraph(result.graph)
    payload.update(root=node_brief(result.root), layers=result.layers)
    return payload


def sequence(diagram: SequenceDiagram) -> dict[str, Any]:
    return {
        "root": node_brief(diagram.root),
        "participants": [asdict(participant) for participant in diagram.participants],
        "steps": [asdict(step) for step in diagram.steps],
        "truncated": diagram.truncated,
    }


def topology(result: Topology) -> dict[str, Any]:
    return {
        "depth": result.depth,
        "groups": [asdict(group) for group in result.groups],
        "links": [asdict(link) for link in result.links],
    }


def node_context(context: NodeContext) -> dict[str, Any]:
    return {
        "node": node_full(context.node),
        "summary": context.summary(),
        "containers": [node_brief(node) for node in context.containers],
        "members": [node_brief(node) for node in context.members],
        "queries": [node_full(node) for node in context.queries],
        **{
            name: [neighbour(item) for item in getattr(context, name)]
            for name in (
                "callers",
                "callees",
                "reads",
                "writes",
                "readers",
                "writers",
                "triggers",
                "supertypes",
                "subtypes",
                "commits",
                "observed_callers",
                "observed_callees",
            )
        },
    }
