"""The bundled demonstration repository.

``examples/demo-monolith`` holds a small, **synthetic** legacy billing system and a
script that replays it into a git repository with twelve years of fictional
history. It exists so the product can be explored without pointing it at real code;
the UI labels it as a demo everywhere it appears.

The demo reaches the graph in one of two ways:

* **ingest** - the git repository is built and the real pipeline runs on it. This is
  the default wherever git is available.
* **snapshot** - the pre-computed graph in ``examples/demo-monolith/graph-snapshot.jsonl``
  is loaded, and the files as of the last commit are written out without git so the
  source viewer has something to show. Used on hosts that have no git binary.
  ``archaeo demo --write-snapshot`` regenerates the file, and a test fails when it
  no longer matches what ingestion produces.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from backend.jobs import JobConflictError, JobManager, JobRecord
from backend.serializers import DEMO_SOURCE_LABEL
from backend.settings import REPO_ROOT, Settings
from graph_engine.schema import make_repository_id
from graph_engine.snapshot import SnapshotSummary, export_repository, import_snapshot
from graph_engine.sqlite_store import SqliteGraphStore
from ingestion.pipeline import IngestionOptions, IngestionPipeline, IngestionRequest

if TYPE_CHECKING:
    from backend.container import Container

logger = logging.getLogger(__name__)

DEMO_NAME = "core-billing-monolith"
#: The demo has the same id on every installation (see ``IngestionPipeline.identify``).
DEMO_REPOSITORY_ID = make_repository_id(DEMO_NAME, f"demo:{DEMO_NAME}")

DEMO_DIR = REPO_ROOT / "examples" / "demo-monolith"
BUILD_SCRIPT = DEMO_DIR / "build_history.py"
SNAPSHOT_PATH = DEMO_DIR / "graph-snapshot.jsonl"

#: Synthetic traffic baked into the snapshot so a read-only instance can show the runtime overlay.
SNAPSHOT_TRACES = 120
SNAPSHOT_SEED = 2012


class DemoUnavailableError(RuntimeError):
    """The demo sources are not shipped with this installation."""


def build_demo_repository(target: Path, *, force: bool = False) -> Path:
    """Materialise the demo git repository at ``target`` (idempotent unless ``force``)."""
    if (target / ".git").is_dir() and not force:
        return target
    if not BUILD_SCRIPT.is_file():
        raise DemoUnavailableError(f"demo sources not found at {BUILD_SCRIPT.parent}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), str(target), "--force"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DemoUnavailableError(f"could not build the demo repository: {error}") from error
    if completed.returncode != 0:
        raise DemoUnavailableError(f"could not build the demo repository: {completed.stderr.strip()[-400:]}")
    return target


def demo_request(settings: Settings) -> IngestionRequest:
    return IngestionRequest(source=str(settings.demo_path), name=DEMO_NAME, demo=True)


# ---------------------------------------------------------------------------- snapshot


def snapshot_available() -> bool:
    return SNAPSHOT_PATH.is_file() and BUILD_SCRIPT.is_file()


def export_demo_sources(target: Path) -> Path:
    """Write the demo's files as of its last commit into ``target``, without git."""
    spec = importlib.util.spec_from_file_location("archaeo_demo_build_history", BUILD_SCRIPT)
    if spec is None or spec.loader is None:
        raise DemoUnavailableError(f"demo sources not found at {BUILD_SCRIPT.parent}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    try:
        spec.loader.exec_module(module)
        module.export_head(target)
    except OSError as error:
        raise DemoUnavailableError(f"could not write the demo sources: {error}") from error
    finally:
        sys.modules.pop(spec.name, None)
    return target


def load_demo_snapshot(container: Container) -> SnapshotSummary:
    """Load the pre-computed demo graph, write out its sources and build its semantic index."""
    if not snapshot_available():
        raise DemoUnavailableError(f"demo snapshot not found at {SNAPSHOT_PATH}")
    sources = export_demo_sources(container.settings.data_dir / "demo" / f"{DEMO_NAME}-sources")
    summary = import_snapshot(container.store, SNAPSHOT_PATH, workspace_path=sources)
    if container.index is not None:
        from rag_engine.indexer import RepositoryIndexer  # noqa: PLC0415

        RepositoryIndexer(container.store, container.index).index_repository(summary.repository_id, sources)
    return summary


def write_demo_snapshot(target: Path = SNAPSHOT_PATH) -> SnapshotSummary:
    """Regenerate the snapshot: build the demo repository, ingest it, add simulated traffic, export."""
    from backend.telemetry.collector import TelemetryCollector  # noqa: PLC0415
    from backend.telemetry.simulator import simulate_events  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="archaeo-demo-") as scratch:
        checkout = build_demo_repository(Path(scratch) / DEMO_NAME, force=True)
        store = SqliteGraphStore(":memory:")
        try:
            store.ensure_schema()
            options = IngestionOptions(
                workspace_dir=Path(scratch) / "workspace",
                allowed_local_roots=(Path(scratch),),
                github_enrichment=False,
                parse_workers=0,
            )
            result = IngestionPipeline(store, options).run(
                IngestionRequest(source=str(checkout), name=DEMO_NAME, demo=True)
            )
            events = simulate_events(store, result.repository_id, traces=SNAPSHOT_TRACES, seed=SNAPSHOT_SEED)
            TelemetryCollector(store).ingest(result.repository_id, events, source="simulated")
            return export_repository(
                store,
                result.repository_id,
                target,
                overrides={"source": DEMO_SOURCE_LABEL, "source_kind": "bundled"},
                reproducible=True,
            )
        finally:
            store.close()


# ----------------------------------------------------------------------------- seeding


def seed_demo(container: Container, jobs: JobManager) -> JobRecord | SnapshotSummary | None:
    """Put the demo into an empty graph, by the route the settings ask for. Never raises."""
    settings = container.settings
    prefer_snapshot = settings.demo_source == "snapshot" or settings.read_only
    try:
        if not prefer_snapshot:
            try:
                build_demo_repository(settings.demo_path)
                return jobs.submit(demo_request(settings))
            except DemoUnavailableError as error:
                if settings.demo_source == "ingest" or not snapshot_available():
                    logger.info("demo repository not seeded: %s", error)
                    return None
                logger.info("demo repository cannot be built here (%s); loading its snapshot", error)
        summary = load_demo_snapshot(container)
        logger.info("demo loaded from its snapshot: %d nodes, %d relationships", summary.nodes, summary.edges)
        return summary
    except JobConflictError:
        return None
    except DemoUnavailableError as error:
        logger.info("demo repository not seeded: %s", error)
    except Exception:
        logger.exception("demo repository could not be seeded")
    return None
