"""Graph retrieval algorithms shared by the HTTP API and the Graph-RAG context builder.

Everything here is written against :class:`graph_engine.store.GraphStore`, so the
embedded SQLite store and Neo4j answer every question identically.

* :func:`call_chain` - bounded upstream (callers) / downstream (callees) neighbourhood.
* :func:`sequence` - the ordered execution trace of one entry point.
* :func:`lineage` - entry point -> business logic -> query -> table -> trigger side effects.
* :func:`topology` - the repository folded into directories with weighted dependencies.
* :func:`node_context` - one node with its immediate structural and historical context.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from graph_engine.models import GraphEdge, GraphNode, SubGraph
from graph_engine.schema import Direction, NodeLabel, RelType
from graph_engine.store import GraphStore

ChainDirection = Literal["upstream", "downstream", "both"]

#: Default confidence floor: weaker edges are "possible" links and are opt-in.
DEFAULT_MIN_CONFIDENCE: Final = 0.5

_CALLS: Final = frozenset({RelType.CALLS})
_RUNTIME_CALLS: Final = frozenset({RelType.OBSERVED_CALL})
_READS: Final = frozenset({RelType.READS_FROM_DB, RelType.OBSERVED_READ})
_WRITES: Final = frozenset({RelType.MUTATES_TABLE, RelType.OBSERVED_WRITE})
_STATIC_DATA: Final = frozenset({RelType.READS_FROM_DB, RelType.MUTATES_TABLE, RelType.FIRES_TRIGGER})
_RUNTIME_DATA: Final = frozenset({RelType.OBSERVED_READ, RelType.OBSERVED_WRITE})
_CHAIN_LABELS: Final = frozenset({NodeLabel.FUNCTION, NodeLabel.DATABASE_TABLE})
_LINEAGE_LABELS: Final = frozenset({NodeLabel.FUNCTION, NodeLabel.DATABASE_TABLE, NodeLabel.DATABASE_QUERY})
_PAGE: Final = 5000
#: How far below a fired trigger a sequence is still expanded, regardless of the depth limit.
_TRIGGER_EXTRA_DEPTH: Final = 2


def _first_line(edge: GraphEdge) -> int:
    lines = edge.properties.get("lines")
    return int(lines[0]) if lines else 0


# --------------------------------------------------------------------------- call chain


@dataclass(slots=True)
class CallChain:
    root: GraphNode
    graph: SubGraph
    #: Signed distance from the root: negative = upstream (callers), positive = downstream.
    levels: dict[str, int] = field(default_factory=dict)
    #: Functions the chain was actually started from (the methods of a class root, ...).
    start_ids: list[str] = field(default_factory=list)


def start_functions(store: GraphStore, node: GraphNode) -> list[str]:
    """The nodes a behavioural question about ``node`` starts from.

    A function or table stands for itself; a class or file stands for the functions
    it contains; a query stands for the function that runs it.
    """
    if node.label in (NodeLabel.FUNCTION, NodeLabel.DATABASE_TABLE):
        return [node.id]
    contains = [RelType.CONTAINS]
    if node.label is NodeLabel.DATABASE_QUERY:
        return sorted(
            hop.node.id
            for hop in store.expand([node.id], contains, Direction.IN)
            if hop.node.label is NodeLabel.FUNCTION
        )
    if node.label in (NodeLabel.CLASS, NodeLabel.FILE):
        found: dict[str, None] = {}
        frontier = [node.id]
        for _ in range(2):  # File -> Class -> Function
            hops = store.expand(frontier, contains, Direction.OUT)
            frontier = []
            for hop in sorted(hops, key=lambda h: (h.node.start_line or 0, h.node.id)):
                if hop.node.label is NodeLabel.FUNCTION:
                    found[hop.node.id] = None
                elif hop.node.label is NodeLabel.CLASS:
                    frontier.append(hop.node.id)
            if not frontier:
                break
        return list(found)
    return []


def call_chain(
    store: GraphStore,
    node_id: str,
    *,
    direction: ChainDirection = "both",
    max_hops: int = 3,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    include_db: bool = True,
    include_runtime: bool = False,
    max_nodes: int = 400,
) -> CallChain | None:
    """Callers and/or callees of a node up to ``max_hops`` away.

    Downstream follows calls and - with ``include_db`` - continues into the tables
    a function touches and the triggers those tables fire, so hidden side effects
    appear in the same picture. Upstream follows callers; from a table it starts at
    every function that reads or mutates it, and from a trigger function it
    continues to the writers of the table that fires it.

    ``include_runtime`` overlays edges observed by the telemetry collector, which
    exposes dynamic dispatch that static analysis could not resolve.
    """
    root = store.get_node(node_id)
    if root is None:
        return None
    starts = start_functions(store, root)
    chain = CallChain(root=root, graph=SubGraph(), start_ids=starts)
    if not starts:
        return chain

    def admit(node: GraphNode) -> bool:
        return node.label in _CHAIN_LABELS

    if direction in ("downstream", "both"):
        rels: set[RelType] = set(_CALLS)
        if include_db:
            rels |= _STATIC_DATA
        if include_runtime:
            rels |= _RUNTIME_CALLS
            if include_db:
                rels |= _RUNTIME_DATA
        down = store.traverse(
            starts,
            rels,
            direction=Direction.OUT,
            max_hops=max_hops,
            min_confidence=min_confidence,
            max_nodes=max_nodes,
            node_filter=admit,
        )
        chain.graph.merge(down)
        for found, depth in down.depths.items():
            chain.levels.setdefault(found, depth)

    if direction in ("upstream", "both"):
        rels = set(_CALLS)
        if include_runtime:
            rels |= _RUNTIME_CALLS
        if include_db:
            # Writers cause a trigger to fire; readers do not, so READS is only
            # followed from a table the question is about (handled by the filter).
            rels |= {RelType.MUTATES_TABLE, RelType.FIRES_TRIGGER, RelType.READS_FROM_DB}
            if include_runtime:
                rels |= _RUNTIME_DATA
        root_tables = {start for start in starts if root.label is NodeLabel.DATABASE_TABLE}

        def follow(edge: GraphEdge) -> bool:
            if edge.type in (RelType.READS_FROM_DB, RelType.OBSERVED_READ):
                return edge.target in root_tables
            return True

        up = store.traverse(
            starts,
            rels,
            direction=Direction.IN,
            max_hops=max_hops,
            min_confidence=min_confidence,
            max_nodes=max_nodes,
            node_filter=admit,
            edge_filter=follow,
        )
        chain.graph.merge(up)
        for found, depth in up.depths.items():
            chain.levels.setdefault(found, -depth)

    # A class or file root is represented by its member functions, all at level 0.
    for start in starts:
        chain.levels[start] = 0
    return chain


# ----------------------------------------------------------------------------- sequence


@dataclass(slots=True)
class Participant:
    id: str
    name: str
    #: ``class`` | ``module`` | ``table`` | ``routine``
    kind: str
    path: str | None = None


@dataclass(slots=True)
class SequenceStep:
    index: int
    depth: int
    source: str
    target: str
    #: ``call`` | ``read`` | ``write`` | ``trigger``
    kind: str
    label: str
    #: Function node performing the step and the node it reaches.
    from_node: str
    to_node: str
    line: int | None = None
    path: str | None = None
    confidence: float = 1.0
    #: ``recursive`` when the callee is already on the stack, ``depth-limit`` when not expanded.
    note: str | None = None


@dataclass(slots=True)
class SequenceDiagram:
    root: GraphNode
    participants: list[Participant] = field(default_factory=list)
    steps: list[SequenceStep] = field(default_factory=list)
    truncated: bool = False


def _participant_of(node: GraphNode) -> Participant:
    if node.label is NodeLabel.DATABASE_TABLE:
        return Participant(node.id, node.name, "table", node.path)
    class_id = node.get("class_id")
    if class_id:
        return Participant(str(class_id), str(node.get("class_name") or "?"), "class", node.path)
    path = node.path or ""
    kind = "routine" if node.get("language") == "sql" else "module"
    return Participant(
        str(node.get("file_id") or f"file:{path}"), posixpath.basename(path) or "?", kind, path
    )


def sequence(
    store: GraphStore,
    node_id: str,
    *,
    max_depth: int = 5,
    max_steps: int = 80,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> SequenceDiagram | None:
    """The execution trace of one function as an ordered list of messages.

    Calls and database accesses are emitted in source order (by call-site line)
    and expanded depth-first, which is the order a reader - or a debugger - would
    see them. The trace is static: branches are flattened and every call that
    *may* happen is shown.
    """
    root = store.get_node(node_id)
    if root is None:
        return None
    diagram = SequenceDiagram(root=root)
    starts = start_functions(store, root)
    if root.label is not NodeLabel.FUNCTION or not starts:
        return diagram

    graph = store.traverse(
        starts,
        _CALLS | _STATIC_DATA,
        direction=Direction.OUT,
        max_hops=max_depth + _TRIGGER_EXTRA_DEPTH + 2,
        min_confidence=min_confidence,
        max_nodes=600,
        node_filter=lambda node: node.label in _CHAIN_LABELS,
    )
    outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in graph.edges.values():
        outgoing[edge.source].append(edge)
    for edges in outgoing.values():
        edges.sort(key=lambda e: (_first_line(e), e.type.value, graph.nodes[e.target].name, e.target))

    participants: dict[str, Participant] = {}

    def lifeline(node: GraphNode) -> str:
        participant = _participant_of(node)
        participants.setdefault(participant.id, participant)
        return participant.id

    def emit(
        depth: int, kind: str, origin: GraphNode, target: GraphNode, edge: GraphEdge, label: str
    ) -> SequenceStep:
        step = SequenceStep(
            index=len(diagram.steps) + 1,
            depth=depth,
            source=lifeline(origin),
            target=lifeline(target),
            kind=kind,
            label=label,
            from_node=origin.id,
            to_node=target.id,
            line=_first_line(edge) or None,
            path=origin.path,
            confidence=edge.confidence,
        )
        diagram.steps.append(step)
        return step

    lifeline(root)
    stack: list[str] = []

    def visit(function: GraphNode, depth: int, limit: int) -> None:
        stack.append(function.id)
        for edge in outgoing.get(function.id, ()):
            if len(diagram.steps) >= max_steps:
                diagram.truncated = True
                break
            target = graph.nodes[edge.target]
            if edge.type is RelType.CALLS:
                step = emit(depth, "call", function, target, edge, f"{target.name}()")
                if target.id in stack:
                    step.note = "recursive"
                elif depth >= limit:
                    if outgoing.get(target.id):
                        step.note = "depth-limit"
                        diagram.truncated = True
                else:
                    visit(target, depth + 1, limit)
            elif edge.type in (RelType.READS_FROM_DB, RelType.MUTATES_TABLE):
                write = edge.type is RelType.MUTATES_TABLE
                operations = edge.properties.get("operations") or (["WRITE"] if write else ["SELECT"])
                emit(depth, "write" if write else "read", function, target, edge, " / ".join(operations))
                if write:
                    fire(target, operations, depth)
        stack.pop()

    def fire(table: GraphNode, operations: Sequence[str], depth: int) -> None:
        for edge in outgoing.get(table.id, ()):
            if edge.type is not RelType.FIRES_TRIGGER or len(diagram.steps) >= max_steps:
                continue
            events = {str(event).upper() for event in edge.properties.get("events") or ()}
            if (
                events
                and not events & {operation.upper() for operation in operations}
                and "WRITE" not in operations
            ):
                continue
            trigger = graph.nodes[edge.target]
            step = emit(depth, "trigger", table, trigger, edge, f"fires {trigger.name}")
            step.path, step.line = trigger.path, trigger.line
            if trigger.id not in stack:
                # A trigger is a hidden side effect: always show what it does, even at the depth limit.
                visit(trigger, depth + 1, max(depth + _TRIGGER_EXTRA_DEPTH, max_depth))

    visit(root, 1, max_depth)
    diagram.participants = list(participants.values())
    diagram.truncated = diagram.truncated or graph.truncated
    return diagram


# ------------------------------------------------------------------------------ lineage

LineageLayer = Literal["entry", "logic", "query", "table", "side_effect"]


@dataclass(slots=True)
class Lineage:
    root: GraphNode
    graph: SubGraph
    layers: dict[str, LineageLayer] = field(default_factory=dict)


def lineage(
    store: GraphStore,
    node_id: str,
    *,
    max_hops: int = 6,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_nodes: int = 400,
) -> Lineage | None:
    """Data lineage through ``node``: who triggers it, what it touches, what happens next.

    From a function the path runs entry point -> business logic -> query -> table ->
    trigger side effects. From a table it runs in reverse as well: every function
    (and, further upstream, every entry point) that reads or mutates the table.
    """
    root = store.get_node(node_id)
    if root is None:
        return None
    result = Lineage(root=root, graph=SubGraph())
    starts = start_functions(store, root)
    if not starts:
        return result

    def admit(node: GraphNode) -> bool:
        return node.label in _LINEAGE_LABELS

    down_rels = _CALLS | _STATIC_DATA | {RelType.CONTAINS}
    down = store.traverse(
        starts,
        down_rels,
        direction=Direction.OUT,
        max_hops=max_hops,
        min_confidence=min_confidence,
        max_nodes=max_nodes,
        node_filter=admit,
    )
    result.graph.merge(down)

    is_table = root.label is NodeLabel.DATABASE_TABLE
    up_rels = set(_CALLS) | {RelType.MUTATES_TABLE, RelType.READS_FROM_DB, RelType.FIRES_TRIGGER}
    root_tables = set(starts) if is_table else set()

    def follow(edge: GraphEdge) -> bool:
        if edge.type is RelType.READS_FROM_DB:
            return edge.target in root_tables
        return True

    up = store.traverse(
        starts,
        up_rels,
        direction=Direction.IN,
        max_hops=max_hops,
        min_confidence=min_confidence,
        max_nodes=max_nodes,
        node_filter=admit,
        edge_filter=follow,
    )
    result.graph.merge(up)

    # Queries that sit between an upstream function and the table it touches.
    upstream_functions = [n for n in up.nodes.values() if n.label is NodeLabel.FUNCTION]
    known_tables = {n.id for n in result.graph.nodes.values() if n.label is NodeLabel.DATABASE_TABLE}
    if upstream_functions and known_tables:
        hops = store.expand([n.id for n in upstream_functions], [RelType.CONTAINS], Direction.OUT)
        queries = {hop.node.id: hop for hop in hops if hop.node.label is NodeLabel.DATABASE_QUERY}
        if queries:
            for hop in store.expand(
                list(queries), _STATIC_DATA, Direction.OUT, min_confidence=min_confidence
            ):
                if hop.node.id in known_tables:
                    owner = queries[hop.origin]
                    result.graph.nodes.setdefault(owner.node.id, owner.node)
                    result.graph.edges[owner.edge.key] = owner.edge
                    result.graph.edges[hop.edge.key] = hop.edge

    _drop_redundant_table_edges(result.graph)
    _assign_layers(result, set(down.nodes), set(up.nodes))
    return result


def _drop_redundant_table_edges(graph: SubGraph) -> None:
    """Hide ``function -> table`` when a query node of that function shows the same access."""
    via_query: set[tuple[str, str, str]] = set()
    owner_of: dict[str, str] = {}
    for edge in graph.edges.values():
        if edge.type is RelType.CONTAINS:
            owner_of[edge.target] = edge.source
    for edge in graph.edges.values():
        if edge.type in (RelType.READS_FROM_DB, RelType.MUTATES_TABLE) and edge.source in owner_of:
            via_query.add((owner_of[edge.source], edge.type.value, edge.target))
    for key in via_query:
        graph.edges.pop(key, None)


def _assign_layers(result: Lineage, downstream: set[str], upstream: set[str]) -> None:
    graph = result.graph
    # Everything only reachable through a trigger is a side effect of the write, not its purpose.
    reachable: set[str] = set()
    adjacency: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in graph.edges.values():
        adjacency[edge.source].append(edge)
    frontier = [node_id for node_id in graph.nodes if node_id in upstream]
    reachable.update(frontier)
    while frontier:
        current = frontier.pop()
        for edge in adjacency.get(current, ()):
            if edge.type is RelType.FIRES_TRIGGER or edge.target in reachable:
                continue
            reachable.add(edge.target)
            frontier.append(edge.target)

    has_caller = {edge.target for edge in graph.edges.values() if edge.type is RelType.CALLS}
    for node_id, node in graph.nodes.items():
        side_effect = node_id in downstream and node_id not in reachable
        layer: LineageLayer
        if side_effect:
            layer = "side_effect"
        elif node.label is NodeLabel.DATABASE_QUERY:
            layer = "query"
        elif node.label is NodeLabel.DATABASE_TABLE:
            layer = "table"
        elif node.get("is_entry_point") or (node_id not in has_caller and node.label is NodeLabel.FUNCTION):
            layer = "entry"
        else:
            layer = "logic"
        result.layers[node_id] = layer


# ----------------------------------------------------------------------------- topology


@dataclass(slots=True)
class TopologyGroup:
    id: str
    name: str
    #: ``directory`` | ``table``
    kind: str
    files: int = 0
    functions: int = 0
    classes: int = 0
    lines: int = 0
    entry_points: int = 0
    languages: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class TopologyLink:
    source: str
    target: str
    #: ``calls`` | ``reads`` | ``writes`` | ``triggers``
    kind: str
    weight: int = 0
    observed: int = 0


@dataclass(slots=True)
class Topology:
    depth: int
    groups: list[TopologyGroup] = field(default_factory=list)
    links: list[TopologyLink] = field(default_factory=list)


def _all_nodes(store: GraphStore, repository_id: str, labels: Collection[NodeLabel]) -> Iterable[GraphNode]:
    offset = 0
    while True:
        page = store.find_nodes(repository_id, labels=labels, limit=_PAGE, offset=offset)
        yield from page
        if len(page) < _PAGE:
            return
        offset += _PAGE


#: Name of the group that collects directories beyond ``max_groups``.
OTHER_GROUP: Final = "(other directories)"


def _group_key(path: str, depth: int) -> str:
    directory = posixpath.dirname(path)
    if not directory:
        return "."
    return "/".join(directory.split("/")[:depth])


def topology(
    store: GraphStore,
    repository_id: str,
    *,
    depth: int | None = None,
    max_groups: int = 40,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    include_tables: bool = True,
) -> Topology:
    """Fold the repository into directories and count the dependencies between them.

    ``depth`` is the number of leading path segments that identify a group. When
    omitted, the deepest level that still yields at most ``max_groups`` groups is
    used, which gives a readable architecture map for both flat and deeply
    nested (``src/main/java/com/...``) layouts.
    """
    files = list(_all_nodes(store, repository_id, [NodeLabel.FILE]))
    paths = [node.path or "" for node in files]
    if depth is None:
        depth = 1
        deepest = max((path.count("/") for path in paths), default=0)
        for candidate in range(1, deepest + 1):
            if len({_group_key(path, candidate) for path in paths}) > max_groups:
                break
            depth = candidate
    depth = max(depth, 1)

    # An explicit depth can still produce more groups than a map can show: keep the
    # largest and fold the long tail into one group instead of dropping it.
    sizes: dict[str, int] = defaultdict(int)
    for node in files:
        sizes[_group_key(node.path or "", depth)] += int(node.get("line_count") or 0) + 1
    folded: set[str] = set()
    if len(sizes) > max_groups:
        ranked = sorted(sizes, key=lambda key: (-sizes[key], key))
        folded = set(ranked[max(max_groups - 1, 1) :])

    groups: dict[str, TopologyGroup] = {}

    def group_for(path: str) -> TopologyGroup:
        key = _group_key(path, depth)
        if key in folded:
            key = OTHER_GROUP
        found = groups.get(key)
        if found is None:
            found = groups[key] = TopologyGroup(id=f"dir:{key}", name=key, kind="directory")
        return found

    for node in files:
        group = group_for(node.path or "")
        group.files += 1
        group.lines += int(node.get("line_count") or 0)
        language = str(node.get("language") or "other")
        group.languages[language] = group.languages.get(language, 0) + 1

    owner: dict[str, str] = {}
    for node in _all_nodes(store, repository_id, [NodeLabel.FUNCTION, NodeLabel.CLASS]):
        group = group_for(node.path or "")
        owner[node.id] = group.id
        if node.label is NodeLabel.CLASS:
            group.classes += 1
        else:
            group.functions += 1
            if node.get("is_entry_point"):
                group.entry_points += 1

    links: dict[tuple[str, str, str], TopologyLink] = {}

    def link(source: str, target: str, kind: str, *, observed: int = 0) -> None:
        found = links.get((source, target, kind))
        if found is None:
            found = links[source, target, kind] = TopologyLink(source, target, kind)
        if observed:
            found.observed += observed
        else:
            found.weight += 1

    rel_types = [RelType.CALLS, RelType.OBSERVED_CALL]
    if include_tables:
        rel_types += [RelType.READS_FROM_DB, RelType.MUTATES_TABLE, RelType.FIRES_TRIGGER]
        for node in _all_nodes(store, repository_id, [NodeLabel.DATABASE_TABLE]):
            groups[node.id] = TopologyGroup(id=node.id, name=node.name, kind="table")
            owner[node.id] = node.id
    for edge in store.list_edges(repository_id, rel_types):
        if edge.confidence < min_confidence:
            continue
        source, target = owner.get(edge.source), owner.get(edge.target)
        if source is None or target is None:
            continue  # e.g. DatabaseQuery -> table: the function-level edge already counts
        if edge.type is RelType.CALLS:
            if source != target:
                link(source, target, "calls")
        elif edge.type is RelType.OBSERVED_CALL:
            if source != target:
                link(source, target, "calls", observed=int(edge.properties.get("count") or 1))
        elif edge.type is RelType.READS_FROM_DB:
            link(source, target, "reads")
        elif edge.type is RelType.MUTATES_TABLE:
            link(source, target, "writes")
        elif edge.type is RelType.FIRES_TRIGGER:
            link(source, target, "triggers")

    connected = {link_.source for link_ in links.values()} | {link_.target for link_ in links.values()}
    ordered = sorted(
        (group for group in groups.values() if group.kind == "directory" or group.id in connected),
        key=lambda g: (g.kind != "directory", g.name),
    )
    return Topology(
        depth=depth,
        groups=ordered,
        links=sorted(
            links.values(),
            key=lambda item: (-(item.weight + item.observed), item.source, item.target, item.kind),
        ),
    )


# ------------------------------------------------------------------------- node context


@dataclass(slots=True)
class Neighbour:
    node: GraphNode
    edge: GraphEdge


@dataclass(slots=True)
class NodeContext:
    """One node with everything a reader needs to interpret it."""

    node: GraphNode
    containers: list[GraphNode] = field(default_factory=list)
    members: list[GraphNode] = field(default_factory=list)
    callers: list[Neighbour] = field(default_factory=list)
    callees: list[Neighbour] = field(default_factory=list)
    reads: list[Neighbour] = field(default_factory=list)
    writes: list[Neighbour] = field(default_factory=list)
    #: For a table: the functions and queries reading / mutating it.
    readers: list[Neighbour] = field(default_factory=list)
    writers: list[Neighbour] = field(default_factory=list)
    triggers: list[Neighbour] = field(default_factory=list)
    queries: list[GraphNode] = field(default_factory=list)
    supertypes: list[Neighbour] = field(default_factory=list)
    subtypes: list[Neighbour] = field(default_factory=list)
    #: Commits that touched the node, newest first.
    commits: list[Neighbour] = field(default_factory=list)
    observed_callers: list[Neighbour] = field(default_factory=list)
    observed_callees: list[Neighbour] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "callers": len(self.callers),
            "callees": len(self.callees),
            "reads": len(self.reads),
            "writes": len(self.writes),
            "queries": len(self.queries),
            "commits": len(self.commits),
        }


def _by_name(items: list[Neighbour]) -> list[Neighbour]:
    return sorted(items, key=lambda n: (-n.edge.confidence, n.node.qualified_name, n.node.id))


def node_context(store: GraphStore, node_id: str, *, min_confidence: float = 0.0) -> NodeContext | None:
    """Fetch a node together with its direct neighbours, grouped by meaning.

    ``min_confidence`` defaults to 0 so that low-confidence ("possible") links are
    returned too; each carries its confidence so callers can present it honestly.
    """
    node = store.get_node(node_id)
    if node is None:
        return None
    context = NodeContext(node=node)
    all_types = list(RelType)
    for hop in store.expand([node_id], all_types, Direction.OUT, min_confidence=min_confidence):
        neighbour = Neighbour(hop.node, hop.edge)
        kind = hop.edge.type
        if kind is RelType.CONTAINS:
            if hop.node.label is NodeLabel.DATABASE_QUERY:
                context.queries.append(hop.node)
            else:
                context.members.append(hop.node)
        elif kind is RelType.CALLS:
            context.callees.append(neighbour)
        elif kind is RelType.READS_FROM_DB:
            context.reads.append(neighbour)
        elif kind is RelType.MUTATES_TABLE:
            context.writes.append(neighbour)
        elif kind is RelType.FIRES_TRIGGER:
            context.triggers.append(neighbour)
        elif kind is RelType.INHERITS_FROM:
            context.supertypes.append(neighbour)
        elif kind is RelType.TOUCHED_BY:
            context.commits.append(neighbour)
        elif kind is RelType.OBSERVED_CALL:
            context.observed_callees.append(neighbour)
    for hop in store.expand([node_id], all_types, Direction.IN, min_confidence=min_confidence):
        neighbour = Neighbour(hop.node, hop.edge)
        kind = hop.edge.type
        if kind is RelType.CONTAINS:
            context.containers.append(hop.node)
        elif kind is RelType.CALLS:
            context.callers.append(neighbour)
        elif kind in _READS:
            context.readers.append(neighbour)
        elif kind in _WRITES:
            context.writers.append(neighbour)
        elif kind is RelType.FIRES_TRIGGER:
            context.triggers.append(neighbour)
        elif kind is RelType.INHERITS_FROM:
            context.subtypes.append(neighbour)
        elif kind is RelType.OBSERVED_CALL:
            context.observed_callers.append(neighbour)

    context.members.sort(key=lambda n: (n.start_line or 0, n.name))
    context.queries.sort(key=lambda n: (n.start_line or 0, n.id))
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
    ):
        setattr(context, name, _by_name(getattr(context, name)))
    context.commits.sort(key=lambda n: (str(n.node.get("authored_at") or ""), n.node.id), reverse=True)
    return context
