"""Behavioural contract every GraphStore backend must satisfy.

The same assertions run against the embedded SQLite store (always) and against a
live Neo4j when ``ARCHAEO_TEST_NEO4J_URI`` is set (CI does this with a service
container). Retrieval algorithms are written once against the GraphStore
interface, so this suite is what guarantees they behave identically on both.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from graph_engine import Direction, GraphEdge, GraphNode, GraphStore, NodeLabel, RelType
from graph_engine.sqlite_store import SqliteGraphStore

NEO4J_URI = os.environ.get("ARCHAEO_TEST_NEO4J_URI")


def _backends() -> list[object]:
    return [
        "sqlite-memory",
        "sqlite-file",
        pytest.param(
            "neo4j",
            marks=[
                pytest.mark.neo4j,
                pytest.mark.skipif(not NEO4J_URI, reason="ARCHAEO_TEST_NEO4J_URI not set"),
            ],
        ),
    ]


@pytest.fixture(params=_backends())
def store(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[GraphStore]:
    backend: str = request.param
    instance: GraphStore
    if backend == "sqlite-memory":
        instance = SqliteGraphStore(":memory:")
    elif backend == "sqlite-file":
        instance = SqliteGraphStore(tmp_path_factory.mktemp("graph") / "graph.sqlite3")
    else:
        from graph_engine.neo4j_store import Neo4jGraphStore

        assert NEO4J_URI
        instance = Neo4jGraphStore(
            NEO4J_URI,
            os.environ.get("ARCHAEO_TEST_NEO4J_USER", "neo4j"),
            os.environ.get("ARCHAEO_TEST_NEO4J_PASSWORD", "neo4j"),
        )
    instance.ensure_schema()
    instance.ensure_schema()  # must be idempotent
    yield instance
    instance.close()


@pytest.fixture()
def repo(store: GraphStore) -> Iterator[str]:
    """A unique repository id per test so a shared Neo4j database stays isolated."""
    repository_id = f"test-{uuid.uuid4().hex[:12]}"
    yield repository_id
    store.delete_repository(repository_id)


def fn(repo: str, name: str, **props: object) -> GraphNode:
    return GraphNode(
        id=f"{repo}:{name}",
        label=NodeLabel.FUNCTION,
        repository_id=repo,
        properties={"name": name, "qualified_name": f"Svc.{name}", "path": "src/Svc.java", **props},
    )


def calls(repo: str, source: str, target: str, **props: object) -> GraphEdge:
    return GraphEdge(
        source=f"{repo}:{source}",
        target=f"{repo}:{target}",
        type=RelType.CALLS,
        repository_id=repo,
        properties=props,
    )


def seed_chain(store: GraphStore, repo: str) -> None:
    """a -> b -> c -> d -> e, a -> c (shortcut), c -> a (cycle), b -> x (low confidence)."""
    store.upsert_nodes([fn(repo, name) for name in ("a", "b", "c", "d", "e", "x")])
    store.upsert_edges(
        [
            calls(repo, "a", "b", confidence=1.0),
            calls(repo, "b", "c", confidence=0.9),
            calls(repo, "c", "d"),
            calls(repo, "d", "e"),
            calls(repo, "a", "c"),
            calls(repo, "c", "a"),
            calls(repo, "b", "x", confidence=0.3),
        ]
    )


def names(nodes: list[GraphNode]) -> list[str]:
    return [node.name for node in nodes]


# ------------------------------------------------------------------------- nodes


def test_ping_reports_backend(store: GraphStore) -> None:
    assert store.ping()["backend"] in {"sqlite", "neo4j"}


def test_node_round_trip_preserves_property_types(store: GraphStore, repo: str) -> None:
    node = GraphNode(
        id=f"{repo}:n",
        label=NodeLabel.FUNCTION,
        repository_id=repo,
        properties={
            "name": "processNightlyReconciliation",
            "qualified_name": "ReconciliationWorker.processNightlyReconciliation",
            "path": "src/ReconciliationWorker.java",
            "start_line": 45,
            "end_line": 90,
            "is_entry_point": True,
            "is_test": False,
            "complexity": 7,
            "score": 0.25,
            "decorators": ["Scheduled", "Transactional"],
            "lines": [3, 5, 8],
            "empty": [],
            "unicode": "naïve — ✓",
            "dropped": None,
        },
    )
    assert store.upsert_nodes([node]) == 1
    loaded = store.get_node(node.id)
    assert loaded is not None
    assert loaded.label is NodeLabel.FUNCTION
    assert loaded.repository_id == repo
    expected = dict(node.properties)
    assert "dropped" not in expected
    assert loaded.properties == expected
    assert loaded.properties["is_entry_point"] is True
    assert loaded.properties["is_test"] is False
    assert isinstance(loaded.properties["complexity"], int)
    assert isinstance(loaded.properties["score"], float)
    assert loaded.location == "ReconciliationWorker.java:45"


def test_upsert_replaces_all_properties(store: GraphStore, repo: str) -> None:
    store.upsert_nodes([fn(repo, "a", stale="yes", complexity=1)])
    store.upsert_nodes([fn(repo, "a", complexity=2)])
    loaded = store.get_node(f"{repo}:a")
    assert loaded is not None
    assert "stale" not in loaded.properties
    assert loaded.properties["complexity"] == 2
    assert store.counts(repo).nodes == {"Function": 1}


def test_get_nodes_ignores_unknown_and_duplicate_ids(store: GraphStore, repo: str) -> None:
    store.upsert_nodes([fn(repo, "a"), fn(repo, "b")])
    found = store.get_nodes([f"{repo}:a", f"{repo}:a", f"{repo}:missing", f"{repo}:b"])
    assert sorted(names(found)) == ["a", "b"]
    assert store.get_node(f"{repo}:missing") is None
    assert store.get_nodes([]) == []


def test_rejects_nested_property_values() -> None:
    with pytest.raises(TypeError):
        GraphNode(id="x", label=NodeLabel.FILE, repository_id="r", properties={"nested": {"a": 1}})
    with pytest.raises(TypeError):
        GraphNode(id="x", label=NodeLabel.FILE, repository_id="r", properties={"mixed": ["a", 1]})
    with pytest.raises(ValueError):
        GraphNode(id="x", label=NodeLabel.FILE, repository_id="r", properties={"id": "shadow"})


# ----------------------------------------------------------------------- lookups


def test_find_nodes_filters_and_orders(store: GraphStore, repo: str) -> None:
    store.upsert_nodes(
        [
            fn(repo, "Zeta"),
            fn(repo, "alpha", is_entry_point=True, kind="method"),
            fn(repo, "Beta", is_entry_point=True, kind="function"),
            GraphNode(
                id=f"{repo}:cls", label=NodeLabel.CLASS, repository_id=repo, properties={"name": "Alpha"}
            ),
        ]
    )
    # ordered by (name, id); uppercase sorts before lowercase in code-point order
    assert names(store.find_nodes(repo, labels=[NodeLabel.FUNCTION])) == ["Beta", "Zeta", "alpha"]
    # name comparison is case-insensitive and spans labels unless restricted
    assert sorted(n.label.value for n in store.find_nodes(repo, name="ALPHA")) == ["Class", "Function"]
    assert names(store.find_nodes(repo, name="alpha", labels=[NodeLabel.CLASS])) == ["Alpha"]
    assert names(store.find_nodes(repo, qualified_name="svc.zeta")) == ["Zeta"]
    assert len(store.find_nodes(repo, path="src/Svc.java")) == 3
    assert store.find_nodes(repo, path="src/svc.java") == []  # path is case-sensitive
    assert names(store.find_nodes(repo, where={"is_entry_point": True})) == ["Beta", "alpha"]
    assert names(store.find_nodes(repo, where={"is_entry_point": True, "kind": "method"})) == ["alpha"]
    assert names(store.find_nodes(repo, labels=[NodeLabel.FUNCTION], limit=1, offset=1)) == ["Zeta"]
    assert store.find_nodes(repo, labels=[]) == []
    with pytest.raises(ValueError):
        store.find_nodes(repo, where={"bad key; DROP": 1})


def test_find_nodes_across_repositories(store: GraphStore, repo: str) -> None:
    store.upsert_nodes(
        [
            GraphNode(
                id=repo, label=NodeLabel.REPOSITORY, repository_id=repo, properties={"name": f"zz-{repo}"}
            )
        ]
    )
    found = store.find_nodes(None, labels=[NodeLabel.REPOSITORY], name=f"zz-{repo}")
    assert [node.id for node in found] == [repo]


def test_search_nodes_ranks_exact_then_prefix_then_substring(store: GraphStore, repo: str) -> None:
    store.upsert_nodes(
        [
            fn(repo, "reconcileLedgerEntries"),
            fn(repo, "Reconcile"),
            fn(repo, "reconcileAll"),
            fn(repo, "processNightlyReconciliation"),
            fn(repo, "unrelated"),
            GraphNode(
                id=f"{repo}:tbl",
                label=NodeLabel.DATABASE_TABLE,
                repository_id=repo,
                properties={"name": "reconcile_log"},
            ),
        ]
    )
    # exact match, then prefix matches (shorter name first), then other substrings
    assert names(store.search_nodes(repo, "RECONCILE", labels=[NodeLabel.FUNCTION])) == [
        "Reconcile",
        "reconcileAll",
        "reconcileLedgerEntries",
    ]
    assert names(store.search_nodes(repo, "reconcil", labels=[NodeLabel.FUNCTION])) == [
        "Reconcile",
        "reconcileAll",
        "reconcileLedgerEntries",
        "processNightlyReconciliation",
    ]
    assert "reconcile_log" in names(store.search_nodes(repo, "reconcile"))
    # qualified names are searched too ("Svc." prefix is shared by every function)
    assert len(store.search_nodes(repo, "svc.", labels=[NodeLabel.FUNCTION])) == 5
    assert names(store.search_nodes(repo, "reconcile", limit=2)) == ["Reconcile", "reconcileAll"]
    assert store.search_nodes(repo, "   ") == []
    assert store.search_nodes(repo, "100%_no_wildcards") == []


def test_nodes_at_returns_innermost_scope_first(store: GraphStore, repo: str) -> None:
    store.upsert_nodes(
        [
            GraphNode(
                id=f"{repo}:cls",
                label=NodeLabel.CLASS,
                repository_id=repo,
                properties={
                    "name": "LegacyAccountDAO",
                    "path": "dao/LegacyAccountDAO.java",
                    "start_line": 10,
                    "end_line": 400,
                },
            ),
            fn(repo, "findFlagged", path="dao/LegacyAccountDAO.java", start_line=300, end_line=320),
            fn(repo, "other", path="dao/LegacyAccountDAO.java", start_line=330, end_line=340),
            fn(repo, "elsewhere", path="dao/Other.java", start_line=300, end_line=320),
        ]
    )
    assert names(store.nodes_at(repo, "dao/LegacyAccountDAO.java", 304)) == [
        "findFlagged",
        "LegacyAccountDAO",
    ]
    assert names(store.nodes_at(repo, "dao/LegacyAccountDAO.java", 304, labels=[NodeLabel.CLASS])) == [
        "LegacyAccountDAO"
    ]
    assert names(store.nodes_at(repo, "dao/LegacyAccountDAO.java", 320)) == [
        "findFlagged",
        "LegacyAccountDAO",
    ]
    assert names(store.nodes_at(repo, "dao/LegacyAccountDAO.java", 325)) == ["LegacyAccountDAO"]
    assert store.nodes_at(repo, "dao/LegacyAccountDAO.java", 999) == []


# ------------------------------------------------------------------------- edges


def test_edges_require_both_endpoints(store: GraphStore, repo: str) -> None:
    store.upsert_nodes([fn(repo, "a"), fn(repo, "b")])
    written = store.upsert_edges(
        [calls(repo, "a", "b"), calls(repo, "a", "ghost"), calls(repo, "ghost", "b")]
    )
    assert written == 1
    assert store.counts(repo).edges == {"CALLS": 1}


def test_edge_upsert_is_keyed_by_source_type_target(store: GraphStore, repo: str) -> None:
    store.upsert_nodes([fn(repo, "a"), fn(repo, "b")])
    store.upsert_edges([calls(repo, "a", "b", lines=[10], stale=True)])
    store.upsert_edges([calls(repo, "a", "b", lines=[10, 22], confidence=0.6, resolution="unique_name")])
    store.upsert_edges(
        [
            GraphEdge(
                source=f"{repo}:a",
                target=f"{repo}:b",
                type=RelType.OBSERVED_CALL,
                repository_id=repo,
                properties={"count": 1},
            )
        ]
    )
    edges = {edge.type: edge for edge in store.list_edges(repo, [RelType.CALLS, RelType.OBSERVED_CALL])}
    assert set(edges) == {RelType.CALLS, RelType.OBSERVED_CALL}
    assert edges[RelType.CALLS].properties == {
        "lines": [10, 22],
        "confidence": 0.6,
        "resolution": "unique_name",
    }
    assert edges[RelType.CALLS].repository_id == repo
    assert store.list_edges(repo, [RelType.CONTAINS]) == []
    assert store.list_edges(repo, []) == []


def test_accumulate_edges_has_counter_semantics(store: GraphStore, repo: str) -> None:
    store.upsert_nodes([fn(repo, "a"), fn(repo, "b")])

    def observed(**props: object) -> GraphEdge:
        return GraphEdge(
            source=f"{repo}:a",
            target=f"{repo}:b",
            type=RelType.OBSERVED_CALL,
            repository_id=repo,
            properties=props,
        )

    options = {
        "additive": ("count", "total_ms", "error_count"),
        "maximum": ("max_ms",),
        "preserve": ("first_seen",),
    }
    assert (
        store.accumulate_edges(
            [
                observed(
                    count=2,
                    total_ms=30.0,
                    max_ms=20.0,
                    first_seen="t1",
                    last_seen="t1",
                    environment="staging",
                )
            ],
            **options,
        )
        == 1
    )
    store.accumulate_edges(
        [observed(count=3, total_ms=12.5, max_ms=9.0, first_seen="t2", last_seen="t2", environment="prod")],
        **options,
    )
    store.accumulate_edges(
        [observed(count=1, total_ms=50.0, max_ms=50.0, error_count=1, last_seen="t3")], **options
    )
    (edge,) = store.list_edges(repo, [RelType.OBSERVED_CALL])
    assert edge.properties == {
        "count": 6,
        "total_ms": 92.5,
        "max_ms": 50.0,
        "error_count": 1,
        "first_seen": "t1",
        "last_seen": "t3",
        "environment": "prod",
    }
    # a missing endpoint is skipped, exactly like upsert_edges
    ghost = GraphEdge(
        source=f"{repo}:a",
        target=f"{repo}:ghost",
        type=RelType.OBSERVED_CALL,
        repository_id=repo,
        properties={"count": 1},
    )
    assert store.accumulate_edges([ghost], **options) == 0


# --------------------------------------------------------------------- traversal


def test_expand_directions(store: GraphStore, repo: str) -> None:
    seed_chain(store, repo)
    out = store.expand([f"{repo}:a"], [RelType.CALLS], Direction.OUT)
    assert sorted(hop.node.name for hop in out) == ["b", "c"]
    assert all(hop.origin == f"{repo}:a" and hop.edge.source == f"{repo}:a" for hop in out)

    incoming = store.expand([f"{repo}:c"], [RelType.CALLS], Direction.IN)
    assert sorted(hop.node.name for hop in incoming) == ["a", "b"]
    assert all(hop.origin == f"{repo}:c" and hop.edge.target == f"{repo}:c" for hop in incoming)

    both = store.expand([f"{repo}:c"], [RelType.CALLS], Direction.BOTH)
    assert sorted(hop.node.name for hop in both) == ["a", "a", "b", "d"]  # c->a and a->c are distinct edges

    assert store.expand([f"{repo}:a"], [RelType.CONTAINS], Direction.OUT) == []
    assert store.expand([], [RelType.CALLS], Direction.OUT) == []
    assert store.expand([f"{repo}:a"], [], Direction.OUT) == []


def test_expand_filters_by_confidence_with_missing_treated_as_certain(store: GraphStore, repo: str) -> None:
    seed_chain(store, repo)
    everything = store.expand([f"{repo}:b"], [RelType.CALLS], Direction.OUT)
    assert sorted(hop.node.name for hop in everything) == ["c", "x"]
    confident = store.expand([f"{repo}:b"], [RelType.CALLS], Direction.OUT, min_confidence=0.5)
    assert [hop.node.name for hop in confident] == ["c"]
    # c -> d has no confidence property and must survive any threshold
    certain = store.expand([f"{repo}:c"], [RelType.CALLS], Direction.OUT, min_confidence=1.0)
    assert sorted(hop.node.name for hop in certain) == ["a", "d"]


def test_expand_returns_self_loop_once_per_origin(store: GraphStore, repo: str) -> None:
    store.upsert_nodes([fn(repo, "rec")])
    store.upsert_edges([calls(repo, "rec", "rec")])
    for direction in (Direction.OUT, Direction.IN, Direction.BOTH):
        hops = store.expand([f"{repo}:rec"], [RelType.CALLS], direction)
        assert [(hop.origin, hop.node.name) for hop in hops] == [(f"{repo}:rec", "rec")]


def test_traverse_reports_minimum_depth_and_induced_edges(store: GraphStore, repo: str) -> None:
    seed_chain(store, repo)
    graph = store.traverse([f"{repo}:a"], [RelType.CALLS], max_hops=2, min_confidence=0.5)
    assert {node.name: graph.depths[node.id] for node in graph.nodes.values()} == {
        "a": 0,
        "b": 1,
        "c": 1,
        "d": 2,
    }
    edge_names = {(e.source.split(":")[-1], e.target.split(":")[-1]) for e in graph.edges.values()}
    # includes the cross edge b->c and the back edge c->a, excludes d->e (e is 3 hops away)
    assert edge_names == {("a", "b"), ("a", "c"), ("b", "c"), ("c", "d"), ("c", "a")}
    assert graph.truncated is False


def test_traverse_terminates_on_cycles_and_reaches_everything(store: GraphStore, repo: str) -> None:
    seed_chain(store, repo)
    graph = store.traverse([f"{repo}:a"], [RelType.CALLS], max_hops=50)
    assert sorted(node.name for node in graph.nodes.values()) == ["a", "b", "c", "d", "e", "x"]
    assert graph.depths[f"{repo}:e"] == 3
    assert len(graph.edges) == 7


def test_traverse_upstream(store: GraphStore, repo: str) -> None:
    seed_chain(store, repo)
    graph = store.traverse([f"{repo}:e"], [RelType.CALLS], direction=Direction.IN, max_hops=2)
    assert {node.name: graph.depths[node.id] for node in graph.nodes.values()} == {"e": 0, "d": 1, "c": 2}


def test_traverse_zero_hops_and_unknown_start(store: GraphStore, repo: str) -> None:
    seed_chain(store, repo)
    graph = store.traverse([f"{repo}:a"], [RelType.CALLS], max_hops=0)
    assert list(graph.depths.values()) == [0] and not graph.edges
    assert not store.traverse([f"{repo}:missing"], [RelType.CALLS]).nodes
    with pytest.raises(ValueError):
        store.traverse([f"{repo}:a"], [RelType.CALLS], max_hops=-1)


def test_traverse_truncation_is_flagged_and_deterministic(store: GraphStore, repo: str) -> None:
    seed_chain(store, repo)
    first = store.traverse([f"{repo}:a"], [RelType.CALLS], max_hops=5, max_nodes=3)
    second = store.traverse([f"{repo}:a"], [RelType.CALLS], max_hops=5, max_nodes=3)
    assert first.truncated is True
    assert sorted(first.nodes) == sorted(second.nodes)
    assert sorted(node.name for node in first.nodes.values()) == ["a", "b", "c"]


def test_traverse_node_and_edge_filters(store: GraphStore, repo: str) -> None:
    seed_chain(store, repo)
    without_c = store.traverse(
        [f"{repo}:a"], [RelType.CALLS], max_hops=5, node_filter=lambda n: n.name != "c"
    )
    assert sorted(node.name for node in without_c.nodes.values()) == ["a", "b", "x"]
    confident_only = store.traverse(
        [f"{repo}:a"], [RelType.CALLS], max_hops=5, edge_filter=lambda e: e.confidence >= 0.95
    )
    # b->c (0.9) and b->x (0.3) are not followed; c is still reached through a->c
    edge_names = {(e.source.split(":")[-1], e.target.split(":")[-1]) for e in confident_only.edges.values()}
    assert ("b", "c") not in edge_names and ("b", "x") not in edge_names
    assert sorted(node.name for node in confident_only.nodes.values()) == ["a", "b", "c", "d", "e"]


# ---------------------------------------------------------------------- deletion


def test_delete_repository_is_scoped_and_can_keep_labels(store: GraphStore, repo: str) -> None:
    other = f"{repo}-other"
    try:
        seed_chain(store, repo)
        job = GraphNode(
            id=f"{repo}:job", label=NodeLabel.INGESTION_JOB, repository_id=repo, properties={"name": "job"}
        )
        store.upsert_nodes([job, fn(other, "keepme"), fn(other, "keepme2")])
        store.upsert_edges([calls(other, "keepme", "keepme2")])

        store.delete_repository(repo, keep_labels=[NodeLabel.INGESTION_JOB])
        assert store.counts(repo).nodes == {"IngestionJob": 1}
        assert store.counts(repo).edges == {}
        assert store.get_node(f"{repo}:a") is None
        assert store.counts(other).nodes == {"Function": 2}
        assert store.counts(other).edges == {"CALLS": 1}

        store.delete_repository(repo)
        assert store.counts(repo).total_nodes == 0
    finally:
        store.delete_repository(other)


def test_delete_edges_removes_only_the_given_types_of_one_repository(store: GraphStore, repo: str) -> None:
    other = f"{repo}-other"

    def observed(repository: str, source: str, target: str) -> GraphEdge:
        return GraphEdge(
            source=f"{repository}:{source}",
            target=f"{repository}:{target}",
            type=RelType.OBSERVED_CALL,
            repository_id=repository,
            properties={"count": 3},
        )

    try:
        seed_chain(store, repo)
        store.upsert_nodes([fn(other, "p"), fn(other, "q")])
        store.upsert_edges([observed(repo, "a", "b"), observed(repo, "b", "c"), observed(other, "p", "q")])
        static_edges = store.counts(repo).edges["CALLS"]

        assert store.delete_edges(repo, []) == 0
        assert store.delete_edges(repo, [RelType.OBSERVED_CALL, RelType.OBSERVED_READ]) == 2
        assert store.counts(repo).edges == {"CALLS": static_edges}
        assert store.counts(repo).nodes == {"Function": 6}
        assert store.counts(other).edges == {"OBSERVED_CALL": 1}
        assert store.delete_edges(repo, [RelType.OBSERVED_CALL]) == 0
    finally:
        store.delete_repository(other)


def test_counts_group_by_label_and_type(store: GraphStore, repo: str) -> None:
    seed_chain(store, repo)
    store.upsert_nodes(
        [
            GraphNode(
                id=f"{repo}:t",
                label=NodeLabel.DATABASE_TABLE,
                repository_id=repo,
                properties={"name": "tbl_acct_v1"},
            )
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                source=f"{repo}:e",
                target=f"{repo}:t",
                type=RelType.READS_FROM_DB,
                repository_id=repo,
                properties={},
            )
        ]
    )
    counts = store.counts(repo)
    assert counts.nodes == {"Function": 6, "DatabaseTable": 1}
    assert counts.edges == {"CALLS": 7, "READS_FROM_DB": 1}
    assert (counts.total_nodes, counts.total_edges) == (7, 8)
