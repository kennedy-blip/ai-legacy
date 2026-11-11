"""The ``archaeo`` command, configured the way an operator configures it: by ``ARCHAEO_*`` variables."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.cli import main
from backend.demo import DEMO_REPOSITORY_ID
from backend.settings import get_settings

from .conftest import GRAPH_BACKENDS, graph_database

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="ingestion needs git")

QUESTION = "Which code writes to tbl_ledger, and are there hidden side effects?"


class Shell:
    """Runs ``archaeo`` with the environment of one instance."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        self._monkeypatch = monkeypatch
        self._capsys = capsys

    def use(self, data_dir: Path, **settings: object) -> None:
        """Point the environment at an instance; every setting becomes ``ARCHAEO_<NAME>``."""
        for name in [name for name in os.environ if name.startswith("ARCHAEO_")]:
            if not name.startswith("ARCHAEO_TEST_"):
                self._monkeypatch.delenv(name)
        self._monkeypatch.setenv("ARCHAEO_DATA_DIR", str(data_dir))
        for name, value in settings.items():
            self._monkeypatch.setenv(f"ARCHAEO_{name.upper()}", str(value))

    def archaeo(self, *arguments: str) -> tuple[str, str]:
        """Standard output and standard error of one successful command."""
        get_settings.cache_clear()  # a new process would read the environment afresh
        code = main(list(arguments))
        captured = self._capsys.readouterr()
        assert code == 0, f"archaeo {' '.join(arguments)}: {captured.err[-2000:]}"
        return captured.out, captured.err


@pytest.fixture()
def shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[Shell]:
    monkeypatch.chdir(tmp_path)  # away from any .env file of the developer
    yield Shell(monkeypatch, capsys)
    get_settings.cache_clear()


def _counts(text: str, pattern: str) -> tuple[int, int]:
    found = re.search(pattern + r" +(\d+) nodes, (\d+) relationships", text)
    assert found, text
    return int(found.group(1)), int(found.group(2))


@pytest.mark.parametrize("backend", GRAPH_BACKENDS)
def test_ingest_ask_export_and_import(backend: str, shell: Shell, tmp_path: Path) -> None:
    with graph_database(backend) as graph:
        shell.use(tmp_path / "first", **graph)
        assert "no repositories" in shell.archaeo("repos")[0]

        ingested, _ = shell.archaeo("demo")
        assert f"repository  {DEMO_REPOSITORY_ID}" in ingested
        written = _counts(ingested, "graph")
        assert written[0] > 100 and written[1] > written[0]

        listed, _ = shell.archaeo("repos")
        assert listed.startswith(DEMO_REPOSITORY_ID)
        assert "25 files" in listed and "72 functions" in listed
        # The graph went where the environment said: into the SQLite file, or not into one.
        assert (tmp_path / "first" / "graph.sqlite3").exists() == (backend == "sqlite")

        answer, notes = shell.archaeo("ask", DEMO_REPOSITORY_ID, QUESTION)
        assert "written by LedgerDao.insertEntry (INSERT)" in answer
        assert "fires trigger trg_ledger_audit" in answer
        checked = re.search(r"citations: (\d+) verified, (\d+) outside the evidence, (\d+) unresolved", notes)
        assert checked, notes
        assert int(checked.group(1)) > 0
        assert (checked.group(2), checked.group(3)) == ("0", "0")

        snapshot = tmp_path / "demo.jsonl.gz"
        exported, _ = shell.archaeo("export", DEMO_REPOSITORY_ID, str(snapshot))
        assert _counts(exported, f"exported {DEMO_REPOSITORY_ID}:") == written

        # A second instance, on SQLite, that never saw the git repository.
        sources = tmp_path / "first" / "demo" / "core-billing-monolith"
        shell.use(tmp_path / "second")
        assert "no repositories" in shell.archaeo("repos")[0]
        imported, _ = shell.archaeo("import", str(snapshot), "--sources", str(sources))
        assert _counts(imported, f"imported {DEMO_REPOSITORY_ID}:") == written
        # Same graph, same question: the same report, whichever store it came from.
        assert shell.archaeo("ask", DEMO_REPOSITORY_ID, QUESTION) == (answer, notes)
