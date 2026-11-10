from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from backend import demo, serializers
from backend.deps import Repository, State, Writable
from backend.jobs import JobConflictError
from backend.schemas import IngestBody, ReingestBody
from graph_engine.schema import NodeLabel
from ingestion.pipeline import IngestionError, IngestionRequest

logger = logging.getLogger(__name__)
router = APIRouter(tags=["repositories"])


def _submit(state: State, request: IngestionRequest) -> dict[str, Any]:
    try:
        job = state.jobs.submit(request)
    except JobConflictError as conflict:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": str(conflict), "job_id": conflict.job.id},
        ) from conflict
    except (IngestionError, ValueError) as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except Exception as error:
        logger.exception("could not queue ingestion")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the job queue is unavailable; try again shortly"
        ) from error
    return serializers.job(job)


@router.get("/repositories")
def list_repositories(state: State) -> list[dict[str, Any]]:
    nodes = state.container.store.find_nodes(None, labels=[NodeLabel.REPOSITORY], limit=500)
    nodes.sort(key=lambda node: (not node.get("is_demo"), node.name.lower()))
    return [serializers.repository(node) for node in nodes]


@router.post("/repositories", status_code=status.HTTP_202_ACCEPTED, dependencies=[Writable])
def ingest_repository(body: IngestBody, state: State) -> dict[str, Any]:
    """Queue ingestion of a repository. Poll the returned job for progress."""
    return _submit(
        state,
        IngestionRequest(
            source=body.source,
            name=body.name,
            branch=body.branch,
            token=body.token.get_secret_value() if body.token else None,
            include=tuple(body.include),
        ),
    )


@router.get("/repositories/{repository_id}")
def get_repository(repository: Repository, state: State) -> dict[str, Any]:
    counts = state.container.store.counts(repository.repository_id)
    return serializers.repository(repository, counts, detail=True)


@router.post(
    "/repositories/{repository_id}/reingest", status_code=status.HTTP_202_ACCEPTED, dependencies=[Writable]
)
def reingest_repository(
    repository: Repository, state: State, body: ReingestBody | None = None
) -> dict[str, Any]:
    """Re-read the repository at its source. Runtime telemetry already collected is kept."""
    if repository.get("is_demo"):
        # The demo may have come from its snapshot, whose recorded source is not a checkout.
        settings = state.container.settings
        try:
            demo.build_demo_repository(settings.demo_path)
        except demo.DemoUnavailableError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return _submit(state, demo.demo_request(settings))
    return _submit(
        state,
        IngestionRequest(
            source=str(repository.get("source")),
            name=repository.name,
            branch=repository.get("requested_branch"),
            token=body.token.get_secret_value() if body and body.token else None,
            include=tuple(repository.get("include") or ()),
            demo=bool(repository.get("is_demo")),
        ),
    )


@router.delete(
    "/repositories/{repository_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Writable]
)
def delete_repository(repository: Repository, state: State) -> None:
    repository_id = repository.repository_id
    if any(job.active for job in state.jobs.list(repository_id, limit=20)):
        raise HTTPException(status.HTTP_409_CONFLICT, "an ingestion of this repository is in progress")
    state.container.store.delete_repository(repository_id)
    if state.container.index is not None:
        try:
            state.container.index.delete_repository(repository_id)
        except Exception:
            logger.warning("could not delete vectors of %s", repository_id, exc_info=True)
    # Remove the clone, but never a local directory the user pointed the server at.
    workspace = repository.get("workspace_path")
    clones = state.container.settings.workspace_path.resolve()
    if workspace and clones in Path(workspace).resolve().parents:
        shutil.rmtree(workspace, ignore_errors=True)


@router.get("/jobs")
def list_jobs(
    state: State,
    repository_id: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    return [serializers.job(job) for job in state.jobs.list(repository_id, limit=limit)]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, state: State) -> dict[str, Any]:
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return serializers.job(job)
