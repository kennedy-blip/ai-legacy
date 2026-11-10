"""The bundled demonstration repository.

``examples/demo-monolith`` holds a small, **synthetic** legacy billing system and a
script that replays it into a git repository with twelve years of fictional
history. It exists so the product can be explored without pointing it at real code;
the UI labels it as a demo everywhere it appears.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from backend.jobs import JobConflictError, JobManager, JobRecord
from backend.settings import REPO_ROOT, Settings
from ingestion.pipeline import IngestionRequest

logger = logging.getLogger(__name__)

DEMO_NAME = "core-billing-monolith"
BUILD_SCRIPT = REPO_ROOT / "examples" / "demo-monolith" / "build_history.py"


class DemoUnavailableError(RuntimeError):
    """The demo sources are not shipped with this installation."""


def build_demo_repository(target: Path, *, force: bool = False) -> Path:
    """Materialise the demo git repository at ``target`` (idempotent unless ``force``)."""
    if (target / ".git").is_dir() and not force:
        return target
    if not BUILD_SCRIPT.is_file():
        raise DemoUnavailableError(f"demo sources not found at {BUILD_SCRIPT.parent}")
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), str(target), "--force"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise DemoUnavailableError(f"could not build the demo repository: {completed.stderr.strip()[-400:]}")
    return target


def demo_request(settings: Settings) -> IngestionRequest:
    return IngestionRequest(source=str(settings.demo_path), name=DEMO_NAME, demo=True)


def seed_demo(settings: Settings, jobs: JobManager) -> JobRecord | None:
    """Build the demo repository and queue its ingestion. Never raises."""
    try:
        build_demo_repository(settings.demo_path)
        return jobs.submit(demo_request(settings))
    except JobConflictError:
        return None
    except DemoUnavailableError as error:
        logger.info("demo repository not seeded: %s", error)
    except Exception:
        logger.exception("demo repository could not be seeded")
    return None
