"""HTTP-level fixtures: whole applications behind a test client."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.demo import DEMO_REPOSITORY_ID
from backend.main import API_PREFIX, create_app
from backend.settings import Settings

REPOSITORY = f"{API_PREFIX}/repositories/{DEMO_REPOSITORY_ID}"


def hosted_settings(data_dir: Path) -> Settings:
    """What ``backend.vercel_app`` configures: read-only, demo from its snapshot, vectors in memory."""
    return Settings(
        data_dir=data_dir,
        read_only=True,
        demo_source="snapshot",
        vector_backend="memory",
        _env_file=None,
    )


@pytest.fixture(scope="module")
def hosted(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The instance the public demo runs: no git, no ingestion, graph loaded from the snapshot."""
    with TestClient(create_app(hosted_settings(tmp_path_factory.mktemp("hosted")))) as client:
        yield client
