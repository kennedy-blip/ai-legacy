"""A self-hosted instance: in-process jobs and real ingestion, on SQLite and (when configured) Neo4j."""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.demo import DEMO_REPOSITORY_ID
from backend.main import API_PREFIX, create_app
from backend.settings import Settings

from .conftest import (
    GRAPH_BACKENDS,
    REPOSITORY,
    graph_settings,
    hosted_settings,
    require_empty_database,
    running,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="ingestion needs git")


def _wait_for_jobs(client: TestClient, timeout: float = 60.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        jobs: list[dict[str, Any]] = client.get(f"{API_PREFIX}/jobs").json()
        if jobs and not any(job["active"] for job in jobs):
            return jobs
        assert time.monotonic() < deadline, f"jobs still running: {jobs}"
        time.sleep(0.05)


@pytest.fixture(scope="module", params=GRAPH_BACKENDS)
def local(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, demo_repo: Path
) -> Iterator[TestClient]:
    """Read-write instance; the demo is ingested from a git repository at start-up."""
    backend: str = request.param
    require_empty_database(backend)
    settings = Settings(
        data_dir=tmp_path_factory.mktemp("local"),
        demo_source="ingest",
        vector_backend="memory",
        allowed_local_roots=[demo_repo.parent],
        github_enrichment=False,
        _env_file=None,
        **graph_settings(backend),
    )
    with running(settings) as client:
        _wait_for_jobs(client)
        yield client


def test_demo_was_ingested_not_loaded(local: TestClient) -> None:
    jobs = _wait_for_jobs(local)
    assert [job["status"] for job in jobs] == ["succeeded"], jobs
    demo = local.get(REPOSITORY).json()
    assert demo["from_snapshot"] is False
    assert demo["ingested_at"]
    assert local.get(f"{API_PREFIX}/system").json()["read_only"] is False


def test_ingested_graph_matches_the_shipped_snapshot(local: TestClient, tmp_path: Path) -> None:
    """The public demo serves a snapshot; it must describe the same graph the pipeline builds."""

    def static(graph: dict[str, dict[str, int]]) -> tuple[dict[str, int], dict[str, int]]:
        # The snapshot leaves out the ingestion job and carries simulated runtime traffic.
        nodes = {label: count for label, count in graph["nodes"].items() if label != "IngestionJob"}
        edges = {kind: count for kind, count in graph["edges"].items() if not kind.startswith("OBSERVED_")}
        return nodes, edges

    def static_links(topology: dict[str, Any]) -> list[tuple[str, str, str, int]]:
        links = [(link["source"], link["target"], link["kind"], link["weight"]) for link in topology["links"]]
        return sorted(link for link in links if link[3] > 0)

    local.delete(f"{REPOSITORY}/telemetry")
    with TestClient(create_app(hosted_settings(tmp_path))) as hosted:
        for path in ("", "/entry-points", "/files"):
            fresh, shipped = local.get(REPOSITORY + path).json(), hosted.get(REPOSITORY + path).json()
            for volatile in ("ingested_at", "from_snapshot", "stats"):
                fresh.pop(volatile, None)
                shipped.pop(volatile, None)
            if not path:
                assert static(fresh.pop("graph")) == static(shipped.pop("graph"))
            assert fresh == shipped, path

        fresh, shipped = (
            local.get(f"{REPOSITORY}/topology").json(),
            hosted.get(f"{REPOSITORY}/topology").json(),
        )
        assert fresh["groups"] == shipped["groups"]
        assert static_links(fresh) == static_links(shipped)


def test_telemetry_round_trip(local: TestClient) -> None:
    telemetry = f"{REPOSITORY}/telemetry"
    assert local.delete(telemetry).status_code == 200
    assert local.get(f"{telemetry}/summary").json()["totals"]["observed_edges"] == 0

    events = [
        {
            "type": "call",
            "caller": "PaymentService.applyPayment",
            "callee": "LedgerDao.insertEntry",
            "count": 3,
        },
        {"type": "query", "function": "LedgerDao.insertEntry", "table": "tbl_ledger", "operation": "INSERT"},
        # Not in the source: only a running system can report it.
        {"type": "call", "caller": "PaymentService.applyPayment", "callee": "Tax2013Adjuster.adjust"},
        {"type": "call", "caller": "PaymentService.applyPayment", "callee": "NoSuchClass.method"},
    ]
    accepted = local.post(f"{telemetry}/events", json={"events": events, "source": "test-agent"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["received"] == 4
    assert accepted.json()["accepted"] == 3
    assert accepted.json()["unmatched_symbols"] == {"NoSuchClass.method": 1}

    summary = local.get(f"{telemetry}/summary").json()
    assert summary["simulated"] is False
    assert summary["sources"] == ["test-agent"]
    assert summary["totals"]["invocations"] == 4
    assert summary["totals"]["confirmed_static_calls"] == 1
    runtime_only = [(row["source"], row["target"]) for row in summary["runtime_only"]]
    names = {
        node_id: node.get("qualified_name") or node["name"] for node_id, node in summary["nodes"].items()
    }
    assert [(names[a], names[b]) for a, b in runtime_only] == [
        ("PaymentService.applyPayment", "Tax2013Adjuster.adjust")
    ]
    # The count in the header and the list underneath describe the same set.
    assert summary["totals"]["never_observed_functions"] == len(summary["never_observed"])

    assert local.delete(telemetry).json()["removed"] == 3
    assert local.get(f"{telemetry}/summary").json()["totals"]["observed_edges"] == 0


def test_simulated_traffic_is_tagged(local: TestClient) -> None:
    telemetry = f"{REPOSITORY}/telemetry"
    local.delete(telemetry)
    simulated = local.post(f"{telemetry}/simulate", json={"traces": 40, "seed": 7})
    assert simulated.status_code == 200, simulated.text
    summary = local.get(f"{telemetry}/summary").json()
    assert summary["simulated"] is True
    assert summary["totals"]["invocations"] > 0
    assert summary["totals"]["runtime_only_calls"] == 0, "the simulator only walks calls found in source"
    local.delete(telemetry)


def test_ingest_and_delete_a_repository(local: TestClient, demo_repo: Path) -> None:
    queued = local.post(f"{API_PREFIX}/repositories", json={"source": str(demo_repo), "name": "second-copy"})
    assert queued.status_code == 202, queued.text
    job = queued.json()
    repository_id = job["repository_id"]
    assert repository_id != DEMO_REPOSITORY_ID

    finished = next(item for item in _wait_for_jobs(local) if item["id"] == job["id"])
    assert finished["status"] == "succeeded", finished
    listed = {repository["id"]: repository for repository in local.get(f"{API_PREFIX}/repositories").json()}
    assert listed[repository_id]["functions"] == listed[DEMO_REPOSITORY_ID]["functions"]
    assert listed[repository_id]["is_demo"] is False

    found = local.get(
        f"{API_PREFIX}/repositories/{repository_id}/search", params={"q": "late fee", "semantic": True}
    )
    assert found.json()["results"][0]["qualified_name"] == "FeeCalculator.lateFee"

    assert local.delete(f"{API_PREFIX}/repositories/{repository_id}").status_code == 204
    assert local.get(f"{API_PREFIX}/repositories/{repository_id}").status_code == 404
    assert demo_repo.is_dir(), "deleting a repository must never remove a local directory it was read from"
    assert local.get(REPOSITORY).status_code == 200


def test_local_paths_outside_the_allowed_roots_are_refused(local: TestClient, tmp_path: Path) -> None:
    (tmp_path / "Example.java").write_text("class Example {}\n")
    response = local.post(f"{API_PREFIX}/repositories", json={"source": str(tmp_path)})
    if response.status_code == 202:  # refused by the job rather than by the request
        job = next(item for item in _wait_for_jobs(local) if item["id"] == response.json()["id"])
        assert job["status"] == "failed", job
        assert "allowed" in job["error"].lower()
    else:
        assert response.status_code in (400, 403, 422), response.text
    names = [repository["name"] for repository in local.get(f"{API_PREFIX}/repositories").json()]
    assert tmp_path.name not in names


@pytest.mark.parametrize(
    "source", ["file:///etc", "ext::sh -c id", "--upload-pack=id", "ftp://example.com/x.git"]
)
def test_dangerous_sources_are_refused(local: TestClient, source: str) -> None:
    response = local.post(f"{API_PREFIX}/repositories", json={"source": source})
    if response.status_code == 202:
        job = next(item for item in _wait_for_jobs(local) if item["id"] == response.json()["id"])
        assert job["status"] == "failed", job
    else:
        assert response.status_code in (400, 403, 422), response.text


def test_api_token_protects_everything_but_health_and_the_api_reference(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        api_token="s3cret",
        seed_demo=False,
        vector_backend="memory",
        _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        for public in ("health", "docs", "openapi.json"):
            assert client.get(f"{API_PREFIX}/{public}").status_code == 200, public
        for protected in ("repositories", "system", "jobs"):
            assert client.get(f"{API_PREFIX}/{protected}").status_code == 401, protected
        assert (
            client.get(f"{API_PREFIX}/repositories", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        granted = client.get(f"{API_PREFIX}/repositories", headers={"Authorization": "Bearer s3cret"})
        assert granted.status_code == 200
        assert granted.json() == []
