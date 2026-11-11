"""Graph snapshots: export, import, and the snapshot shipped with the demo."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backend import demo
from graph_engine.schema import RUNTIME_RELS, NodeLabel, RelType, make_id
from graph_engine.snapshot import (
    FORMAT,
    VERSION,
    SnapshotError,
    export_repository,
    import_snapshot,
    read_header,
)
from graph_engine.sqlite_store import SqliteGraphStore
from tests.conftest import Corpus


def _store() -> SqliteGraphStore:
    store = SqliteGraphStore(":memory:")
    store.ensure_schema()
    return store


def _edges(store: SqliteGraphStore, repository_id: str) -> list[tuple[str, str, str]]:
    return sorted(
        (edge.source, edge.type.value, edge.target) for edge in store.list_edges(repository_id, list(RelType))
    )


def test_round_trip_preserves_the_graph(corpus: Corpus, tmp_path: Path) -> None:
    target = tmp_path / "graph.jsonl"
    written = export_repository(corpus.store, corpus.repository_id, target)

    header = read_header(target)
    assert (header["format"], header["version"]) == (FORMAT, VERSION)
    assert header["repository_id"] == corpus.repository_id
    assert (header["nodes"], header["edges"]) == (written.nodes, written.edges)

    copy = _store()
    loaded = import_snapshot(copy, target)
    assert (loaded.repository_id, loaded.nodes, loaded.edges) == (
        corpus.repository_id,
        written.nodes,
        written.edges,
    )
    assert _edges(copy, corpus.repository_id) == _edges(corpus.store, corpus.repository_id)

    original = corpus.function("FeeCalculator.lateFee")
    restored = copy.get_node(original.id)
    assert restored is not None
    assert restored.properties == original.properties
    copy.close()


def test_import_marks_the_origin_and_drops_local_paths(corpus: Corpus, tmp_path: Path) -> None:
    target = tmp_path / "graph.jsonl"
    export_repository(corpus.store, corpus.repository_id, target)
    assert str(corpus.root.parent) not in target.read_text(), (
        "a snapshot must not carry paths of this machine"
    )

    copy = _store()
    import_snapshot(copy, target)
    root = copy.get_node(make_id(NodeLabel.REPOSITORY, corpus.repository_id))
    assert root is not None
    assert root.get("from_snapshot") is True
    assert root.get("workspace_path") is None
    assert root.get("source") == corpus.root.name

    import_snapshot(copy, target, workspace_path=tmp_path / "sources")
    root = copy.get_node(make_id(NodeLabel.REPOSITORY, corpus.repository_id))
    assert root is not None
    assert root.get("workspace_path") == str(tmp_path / "sources")
    copy.close()


def test_import_replaces_an_existing_graph(corpus: Corpus, tmp_path: Path) -> None:
    target = tmp_path / "graph.jsonl"
    written = export_repository(corpus.store, corpus.repository_id, target)
    copy = _store()
    import_snapshot(copy, target)
    import_snapshot(copy, target)
    assert len(_edges(copy, corpus.repository_id)) == written.edges
    assert len(copy.find_nodes(corpus.repository_id, labels=[NodeLabel.FUNCTION], limit=1000)) == 72
    copy.close()


def test_compressed_snapshots_are_reproducible(corpus: Corpus, tmp_path: Path) -> None:
    first, second = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    export_repository(corpus.store, corpus.repository_id, first, reproducible=True)
    export_repository(corpus.store, corpus.repository_id, second, reproducible=True)
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes()[:2] == b"\x1f\x8b"
    copy = _store()
    assert import_snapshot(copy, first).repository_id == corpus.repository_id
    copy.close()


def test_runtime_observations_can_be_left_out(tmp_path: Path) -> None:
    store = _store()
    shipped = import_snapshot(store, demo.SNAPSHOT_PATH)
    assert store.list_edges(shipped.repository_id, list(RUNTIME_RELS)), (
        "the shipped demo carries simulated traffic"
    )

    target = tmp_path / "static.jsonl"
    written = export_repository(store, shipped.repository_id, target, include_runtime=False)
    assert written.edges < shipped.edges
    kinds = {json.loads(line).get("type") for line in target.read_text().splitlines()[1:]}
    assert not kinds & {rel.value for rel in RUNTIME_RELS}
    store.close()


def test_unknown_repository_cannot_be_exported(tmp_path: Path) -> None:
    store = _store()
    with pytest.raises(LookupError):
        export_repository(store, "nope", tmp_path / "graph.jsonl")
    store.close()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json\n",
        '{"kind": "header", "format": "something-else", "version": 1, "repository_id": "x"}\n',
        f'{{"kind": "header", "format": "{FORMAT}", "version": {VERSION + 1}, "repository_id": "x"}}\n',
        f'{{"kind": "header", "format": "{FORMAT}", "version": {VERSION}, "repository_id": "x"}}\n',
        f'{{"kind": "header", "format": "{FORMAT}", "version": {VERSION}, "repository_id": "x"}}\n{{"kind": "?"}}\n',
        f'{{"kind": "header", "format": "{FORMAT}", "version": {VERSION}, "repository_id": "x"}}\n{{"kind": "node"}}\n',
    ],
    ids=[
        "empty",
        "not-json",
        "other-format",
        "newer-version",
        "no-repository-node",
        "unknown-record",
        "bad-node",
    ],
)
def test_malformed_snapshots_are_rejected_before_anything_is_written(content: str, tmp_path: Path) -> None:
    source = tmp_path / "broken.jsonl"
    source.write_text(content)
    store = _store()
    with pytest.raises(SnapshotError):
        import_snapshot(store, source)
    assert store.find_nodes(None, labels=[NodeLabel.REPOSITORY]) == []
    store.close()


# --------------------------------------------------------------------- the shipped demo


def test_shipped_snapshot_header_matches_its_content() -> None:
    header = read_header(demo.SNAPSHOT_PATH)
    lines = demo.SNAPSHOT_PATH.read_text().splitlines()[1:]
    kinds = [json.loads(line)["kind"] for line in lines]
    assert header["repository_id"] == demo.DEMO_REPOSITORY_ID
    assert (kinds.count("node"), kinds.count("edge")) == (header["nodes"], header["edges"])


@pytest.mark.skipif(shutil.which("git") is None, reason="building the demo repository needs git")
def test_shipped_snapshot_is_what_the_pipeline_produces_today(tmp_path: Path) -> None:
    """The public demo serves this file. If a parser changes, it has to be regenerated:

    python -m backend.cli demo --write-snapshot
    """
    fresh = tmp_path / "graph-snapshot.jsonl"
    demo.write_demo_snapshot(fresh)
    assert fresh.read_text() == demo.SNAPSHOT_PATH.read_text(), (
        "examples/demo-monolith/graph-snapshot.jsonl is stale; regenerate it with "
        "`python -m backend.cli demo --write-snapshot`"
    )


def test_bundled_sources_match_the_snapshot(tmp_path: Path) -> None:
    """Source text shown by the hosted demo comes from files exported next to the graph."""
    sources = demo.export_demo_sources(tmp_path / "sources")
    store = _store()
    summary = import_snapshot(store, demo.SNAPSHOT_PATH, workspace_path=sources)
    files = store.find_nodes(summary.repository_id, labels=[NodeLabel.FILE], limit=1000)
    assert len(files) == 25
    for file in files:
        path = sources / str(file.path)
        assert path.is_file(), file.path
        assert len(path.read_text().splitlines()) == file.get("line_count"), file.path
    store.close()
