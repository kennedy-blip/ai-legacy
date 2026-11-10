"""Resolves human-style references to graph nodes.

``OrderProcessor.java:304`` becomes a file position with its enclosing scope;
``PaymentService.applyPayment`` becomes the nodes carrying that name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from graph_engine.models import GraphNode
from graph_engine.schema import NodeLabel
from graph_engine.store import GraphStore

#: ``File.ext:123`` or ``dir/File.ext:123-140`` (the range end is optional).
LOCATION_RE: Final = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)*[\w-]+(?:\.[\w-]+)*\.[A-Za-z][A-Za-z0-9]{0,5})"
    r"(?::|#L|\s+line\s+)(\d{1,6})(?:\s*[-\u2013]\s*L?(\d{1,6}))?"
)
_SCOPE_LABELS: Final = (
    NodeLabel.FUNCTION,
    NodeLabel.CLASS,
    NodeLabel.DATABASE_QUERY,
    NodeLabel.DATABASE_TABLE,
)
_SYMBOL_LABELS: Final = (NodeLabel.FUNCTION, NodeLabel.CLASS, NodeLabel.DATABASE_TABLE, NodeLabel.FILE)


@dataclass(slots=True)
class Location:
    """A file position resolved against the graph."""

    path: str
    line: int
    end_line: int | None
    file: GraphNode
    #: Innermost scope containing the line (a function, else its class), when there is one.
    scope: GraphNode | None = None

    @property
    def citation(self) -> str:
        return f"{self.path.rsplit('/', 1)[-1]}:{self.line}"


def parse_location(text: str) -> tuple[str, int, int | None] | None:
    """Split ``File.java:304`` into ``("File.java", 304, None)``."""
    found = LOCATION_RE.fullmatch(text.strip().strip("[]()`"))
    if not found:
        return None
    end = int(found.group(3)) if found.group(3) else None
    return found.group(1), int(found.group(2)), end


def find_files(store: GraphStore, repository_id: str, reference: str) -> list[GraphNode]:
    """File nodes matching a path, a path suffix or a bare file name."""
    reference = reference.strip().lstrip("./")
    if not reference:
        return []
    exact = store.find_nodes(repository_id, labels=[NodeLabel.FILE], path=reference, limit=2)
    if exact:
        return exact
    name = reference.rsplit("/", 1)[-1]
    candidates = store.find_nodes(repository_id, labels=[NodeLabel.FILE], name=name, limit=50)
    if "/" in reference:
        suffix = "/" + reference
        candidates = [node for node in candidates if ("/" + (node.path or "")).endswith(suffix)]
    # ``name`` matches case-insensitively; prefer the exact spelling, then the shortest path.
    candidates.sort(key=lambda node: (node.name != name, len(node.path or ""), node.path or ""))
    return candidates


def resolve_location(
    store: GraphStore, repository_id: str, reference: str, line: int, end_line: int | None = None
) -> list[Location]:
    """All positions ``reference:line`` can denote, best match first.

    A bare file name can exist in several directories; candidates in which the
    line falls inside a known scope are ranked first, and files too short to
    contain the line are dropped.
    """
    locations: list[Location] = []
    for file in find_files(store, repository_id, reference):
        path = file.path or ""
        if line < 1 or line > int(file.get("line_count") or 0) + 1:
            continue
        scopes = store.nodes_at(repository_id, path, line, labels=_SCOPE_LABELS)
        locations.append(Location(path, line, end_line, file, scopes[0] if scopes else None))
    locations.sort(key=lambda location: location.scope is None)
    return locations


def find_symbols(store: GraphStore, repository_id: str, text: str, *, limit: int = 5) -> list[GraphNode]:
    """Nodes named ``text``: exact qualified name, then exact name, then fuzzy search."""
    text = text.strip().strip("`'\"()")
    if not text:
        return []
    normalised = text.replace("::", ".").replace("#", ".")
    for candidate in dict.fromkeys((text, normalised)):
        found = store.find_nodes(repository_id, labels=_SYMBOL_LABELS, qualified_name=candidate, limit=limit)
        if found:
            return found
    tail = normalised.rsplit(".", 1)[-1]
    found = store.find_nodes(repository_id, labels=_SYMBOL_LABELS, name=tail, limit=limit * 4)
    if found and "." in normalised:
        owner = normalised.rsplit(".", 1)[0].lower()
        narrowed = [node for node in found if owner in node.qualified_name.lower()]
        found = narrowed or found
    if found:
        return found[:limit]
    return store.search_nodes(repository_id, tail, labels=_SYMBOL_LABELS, limit=limit)
