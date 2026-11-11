"""Whole installations, started and used from outside, the way an operator runs them.

``archaeo serve`` runs as a process of its own, configured only by ``ARCHAEO_*`` variables, and
the tests talk to it over HTTP. Two installations are covered:

``single-process``
    What ``pip install -e .`` gives: the graph in SQLite, ingestion on a thread of the server.

``servers``
    The graph in Neo4j, the search index in a Qdrant server, and ingestion in a Celery worker
    (``archaeo worker``, a second process) that takes its jobs from Redis. It runs when
    ``ARCHAEO_TEST_NEO4J_URI``, ``ARCHAEO_TEST_QDRANT_URL`` and ``ARCHAEO_TEST_REDIS_URL`` are all
    set, and those servers must hold nothing else.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.demo import DEMO_REPOSITORY_ID
from backend.main import API_PREFIX
from rag_engine.embeddings import HashingEmbedder
from rag_engine.vector_index import create_vector_index
from tests.api.conftest import NEO4J_URI, graph_database
from tests.conftest import REPO_ROOT

QDRANT_URL = os.environ.get("ARCHAEO_TEST_QDRANT_URL")
REDIS_URL = os.environ.get("ARCHAEO_TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="ingestion needs git")

INSTALLATIONS: list[object] = [
    "single-process",
    pytest.param(
        "servers",
        marks=[
            pytest.mark.servers,
            pytest.mark.skipif(
                not (NEO4J_URI and QDRANT_URL and REDIS_URL),
                reason="ARCHAEO_TEST_NEO4J_URI, ARCHAEO_TEST_QDRANT_URL and ARCHAEO_TEST_REDIS_URL not all set",
            ),
        ],
    ),
]

DEMO = f"/repositories/{DEMO_REPOSITORY_ID}"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@dataclass
class Installation:
    kind: str
    client: httpx.Client
    directory: Path
    processes: dict[str, subprocess.Popen[bytes]] = field(default_factory=dict)

    def logs(self) -> str:
        """The end of what each process printed, for the message of a failed assertion."""
        tails = []
        for name in self.processes:
            lines = (self.directory / f"{name}.log").read_text(errors="replace").splitlines()[-25:]
            tails.append(f"--- {name} ---\n" + "\n".join(lines))
        return "\n".join(tails)

    def request(self, method: str, path: str, *, expect: int = 200, **options: Any) -> httpx.Response:
        response = self.client.request(method, path, **options)
        assert response.status_code == expect, (
            f"{method} {path}: {response.status_code} {response.text[:300]}\n{self.logs()}"
        )
        return response

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params).json()

    def wait_for_jobs(self, timeout: float = 180.0) -> list[dict[str, Any]]:
        """Every job once none is queued or running."""
        deadline = time.monotonic() + timeout
        while True:
            jobs: list[dict[str, Any]] = self.get("/jobs")
            if jobs and not any(job["active"] for job in jobs):
                return jobs
            assert time.monotonic() < deadline, f"jobs did not finish: {jobs}\n{self.logs()}"
            time.sleep(0.2)

    def vectors(self, repository_id: str) -> int | None:
        """Vectors kept for a repository, counted on the Qdrant server itself.

        ``None`` for the single-process installation, whose index lives inside the server process.
        """
        if self.kind != "servers":
            return None
        index = create_vector_index(HashingEmbedder(), backend="qdrant", location=QDRANT_URL)
        try:
            return index.count(repository_id)
        finally:
            index.close()


def _start(installation: Installation, name: str, environment: dict[str, str], *arguments: str) -> None:
    log = (installation.directory / f"{name}.log").open("wb")
    installation.processes[name] = subprocess.Popen(
        [sys.executable, "-m", "backend.cli", *arguments],
        cwd=installation.directory,  # away from any .env file of the developer
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _wait_until_healthy(installation: Installation, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        for name, process in installation.processes.items():
            assert process.poll() is None, f"`archaeo {name}` exited\n{installation.logs()}"
        try:
            if installation.client.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        assert time.monotonic() < deadline, f"the API did not come up\n{installation.logs()}"
        time.sleep(0.2)


@pytest.fixture(scope="module", params=INSTALLATIONS)
def installation(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, demo_repo: Path
) -> Iterator[Installation]:
    kind: str = request.param
    directory = tmp_path_factory.mktemp(kind)
    port = _free_port()
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}{API_PREFIX}", timeout=60)
    running = Installation(kind, client, directory)

    with graph_database("neo4j" if kind == "servers" else "sqlite") as graph:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("ARCHAEO_") or name.startswith("ARCHAEO_TEST_")
        }
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(REPO_ROOT), environment.get("PYTHONPATH")])
        )
        environment["ARCHAEO_DATA_DIR"] = str(directory / "data")
        environment["ARCHAEO_ALLOWED_LOCAL_ROOTS"] = str(demo_repo.parent)
        environment["ARCHAEO_GITHUB_ENRICHMENT"] = "false"
        for name, value in graph.items():
            environment[f"ARCHAEO_{name.upper()}"] = str(value)
        if kind == "servers":
            assert QDRANT_URL and REDIS_URL
            environment["ARCHAEO_QDRANT_URL"] = QDRANT_URL
            environment["ARCHAEO_JOB_BACKEND"] = "celery"
            environment["ARCHAEO_REDIS_URL"] = REDIS_URL
        try:
            if kind == "servers":
                _start(running, "worker", environment, "worker", "--concurrency", "1")
            _start(running, "serve", environment, "serve", "--port", str(port))
            _wait_until_healthy(running)
            running.wait_for_jobs()  # the demo is ingested at the first start
            yield running
        finally:
            try:  # removes the vectors as well, which a server keeps
                for repository in client.get("/repositories").json():
                    client.delete(f"/repositories/{repository['id']}")
            except (httpx.HTTPError, ValueError):
                pass
            client.close()
            for process in running.processes.values():
                process.terminate()
            for process in running.processes.values():
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def test_the_parts_are_the_configured_ones(installation: Installation) -> None:
    system = installation.get("/system")
    servers = installation.kind == "servers"
    assert system["components"]["graph"]["status"] == "ok"
    assert system["components"]["graph"]["backend"] == ("neo4j" if servers else "sqlite")
    assert system["job_backend"] == ("celery" if servers else "inline")
    assert system["components"]["vectors"]["status"] == "ok"
    if servers:
        assert system["vector_backend"] == "qdrant"
    assert system["warnings"] == []


def test_the_demo_was_ingested_at_start_up(installation: Installation) -> None:
    jobs = installation.wait_for_jobs()
    assert [(job["repository_id"], job["status"]) for job in jobs] == [(DEMO_REPOSITORY_ID, "succeeded")], (
        f"{jobs}\n{installation.logs()}"
    )
    demo = installation.get(DEMO)
    assert demo["from_snapshot"] is False
    assert (demo["files"], demo["functions"], demo["tables"]) == (25, 72, 8)
    assert installation.vectors(DEMO_REPOSITORY_ID) != 0


def test_search_by_vocabulary_uses_the_index(installation: Installation) -> None:
    # Two words are not a name, so only the search index can find them.
    assert installation.get(f"{DEMO}/search", q="late fee")["results"] == []
    found = installation.get(f"{DEMO}/search", q="late fee", semantic=True)["results"]
    assert (found[0]["qualified_name"], found[0]["match"]) == ("FeeCalculator.lateFee", "semantic"), found[:3]


def test_an_answer_streams_over_http_and_its_citations_hold(installation: Installation) -> None:
    question = {"question": "Which code writes to tbl_ledger, and are there hidden side effects?"}
    events = []
    with installation.client.stream("POST", f"{DEMO}/explain", json=question) as response:
        assert response.status_code == 200, installation.logs()
        assert response.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in response.iter_lines() if line.strip()]
    kinds = [event["type"] for event in events]
    assert kinds[0] == "context" and kinds[-2:] == ["citations", "done"], kinds
    assert "error" not in kinds, events
    summary = events[-2]["summary"]
    assert summary["total"] > 0 and summary["verified"] == summary["total"], summary
    answer = events[-1]["answer"]
    assert "written by LedgerDao.insertEntry (INSERT)" in answer
    assert "fires trigger trg_ledger_audit" in answer


def test_a_second_repository_is_ingested_and_deleted(installation: Installation, demo_repo: Path) -> None:
    queued = installation.request(
        "POST", "/repositories", expect=202, json={"source": str(demo_repo), "name": "second-copy"}
    ).json()
    repository_id = queued["repository_id"]
    assert repository_id != DEMO_REPOSITORY_ID

    finished = next(job for job in installation.wait_for_jobs() if job["id"] == queued["id"])
    assert finished["status"] == "succeeded", f"{finished}\n{installation.logs()}"
    assert installation.get(f"/repositories/{repository_id}")["functions"] == 72
    assert installation.vectors(repository_id) == installation.vectors(DEMO_REPOSITORY_ID)
    found = installation.get(f"/repositories/{repository_id}/search", q="late fee", semantic=True)
    assert found["results"][0]["qualified_name"] == "FeeCalculator.lateFee"

    installation.request("DELETE", f"/repositories/{repository_id}", expect=204)
    installation.request("GET", f"/repositories/{repository_id}", expect=404)
    assert installation.vectors(repository_id) in (None, 0), "deleting a repository removes its vectors"
    assert installation.get(DEMO)["functions"] == 72
