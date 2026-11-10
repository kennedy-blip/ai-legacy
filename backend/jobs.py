"""Ingestion jobs: submission, execution (in-process thread or Celery) and progress tracking.

Job state is stored as ``IngestionJob`` nodes in the graph database rather than in
process memory, so the API server and Celery workers - separate processes, possibly
on separate hosts - see the same state, and it survives a restart.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from graph_engine.models import GraphNode
from graph_engine.schema import NodeLabel, make_id
from graph_engine.store import GraphStore
from ingestion.pipeline import IngestionError, IngestionPipeline, IngestionRequest

logger = logging.getLogger(__name__)

ACTIVE: Final = frozenset({"queued", "running"})
#: A running job that has not reported for this long is assumed to have died with its worker.
STALE_AFTER: Final = timedelta(minutes=20)
_PROGRESS_INTERVAL: Final = 0.4

Dispatcher = Callable[[str, IngestionRequest], None]


class JobConflictError(RuntimeError):
    """The repository already has an ingestion in progress."""

    def __init__(self, job: JobRecord) -> None:
        super().__init__(f"repository {job.repository_id} is already being ingested (job {job.id})")
        self.job = job


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class JobRecord:
    id: str
    repository_id: str
    name: str
    source: str
    status: str
    stage: str
    progress: float
    message: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    branch: str | None = None
    warnings: tuple[str, ...] = ()
    stats: dict[str, Any] | None = None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @classmethod
    def from_node(cls, node: GraphNode) -> JobRecord:
        stats = node.get("stats_json")
        return cls(
            id=node.id,
            repository_id=node.repository_id,
            name=node.name,
            source=str(node.get("source") or ""),
            status=str(node.get("status") or "queued"),
            stage=str(node.get("stage") or "queued"),
            progress=float(node.get("progress") or 0.0),
            message=str(node.get("message") or ""),
            created_at=str(node.get("created_at") or ""),
            updated_at=str(node.get("updated_at") or ""),
            started_at=node.get("started_at"),
            finished_at=node.get("finished_at"),
            error=node.get("error"),
            branch=node.get("branch"),
            warnings=tuple(node.get("warnings") or ()),
            stats=json.loads(stats) if stats else None,
        )

    def to_node(self) -> GraphNode:
        return GraphNode(
            self.id,
            NodeLabel.INGESTION_JOB,
            self.repository_id,
            {
                "name": self.name,
                "source": self.source,
                "status": self.status,
                "stage": self.stage,
                "progress": round(self.progress, 4),
                "message": self.message,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "branch": self.branch,
                "warnings": list(self.warnings),
                "stats_json": json.dumps(self.stats, sort_keys=True) if self.stats is not None else None,
            },
        )


class JobManager:
    def __init__(
        self,
        store: GraphStore,
        pipeline: IngestionPipeline,
        *,
        dispatcher: Dispatcher | None = None,
    ) -> None:
        """``dispatcher`` hands a job to an external worker; without one, jobs run on a local thread."""
        self.store = store
        self.pipeline = pipeline
        self._dispatcher = dispatcher
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")
        self._submit_lock = threading.Lock()

    # ------------------------------------------------------------------ queries

    def get(self, job_id: str) -> JobRecord | None:
        node = self.store.get_node(job_id)
        if node is None or node.label is not NodeLabel.INGESTION_JOB:
            return None
        return self._expire(JobRecord.from_node(node))

    def list(self, repository_id: str | None = None, *, limit: int = 50) -> list[JobRecord]:
        nodes = self.store.find_nodes(repository_id, labels=[NodeLabel.INGESTION_JOB], limit=500)
        jobs = [self._expire(JobRecord.from_node(node)) for node in nodes]
        jobs.sort(key=lambda job: (job.created_at, job.id), reverse=True)
        return jobs[:limit]

    def _expire(self, job: JobRecord) -> JobRecord:
        """Fail a job whose worker stopped reporting, so it does not block the repository forever."""
        if not job.active or not job.updated_at:
            return job
        try:
            updated = datetime.strptime(job.updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            return job
        if datetime.now(UTC) - updated > STALE_AFTER:
            job.status, job.stage = "failed", "failed"
            job.error = "the worker stopped reporting progress (it was probably restarted)"
            job.finished_at = job.updated_at = _now()
            self.store.upsert_nodes([job.to_node()])
        return job

    # ---------------------------------------------------------------- lifecycle

    def submit(self, request: IngestionRequest) -> JobRecord:
        """Validate ``request``, record a queued job and hand it to the executor."""
        repository_id, name, source = self.pipeline.identify(request)
        with self._submit_lock:
            for existing in self.list(repository_id, limit=20):
                if existing.active:
                    raise JobConflictError(existing)
            now = _now()
            job = JobRecord(
                id=make_id(NodeLabel.INGESTION_JOB, repository_id, uuid.uuid4().hex),
                repository_id=repository_id,
                name=name,
                source=source,
                status="queued",
                stage="queued",
                progress=0.0,
                message="Waiting for a worker",
                created_at=now,
                updated_at=now,
                branch=request.branch,
            )
            self.store.upsert_nodes([job.to_node()])
        try:
            if self._dispatcher is not None:
                self._dispatcher(job.id, request)
            else:
                self._executor.submit(self.run, job.id, request)
        except Exception as error:
            job.status, job.stage, job.finished_at = "failed", "failed", _now()
            job.error = f"could not queue the job: {error.__class__.__name__}"
            self.store.upsert_nodes([job.to_node()])
            raise
        return job

    def run(self, job_id: str, request: IngestionRequest) -> JobRecord | None:
        """Execute a queued job to completion. Called on the worker (thread or Celery task)."""
        job = self.get(job_id)
        if job is None:
            logger.error("ingestion job %s not found", job_id)
            return None
        job.status, job.stage, job.started_at = "running", "checkout", _now()
        job.message = "Starting"
        self._save(job)
        last_write = 0.0

        def progress(stage: str, fraction: float, message: str) -> None:
            nonlocal last_write
            changed_stage = stage != job.stage
            job.stage, job.progress, job.message = stage, fraction, message
            now = time.monotonic()
            if changed_stage or now - last_write >= _PROGRESS_INTERVAL:
                last_write = now
                self._save(job)

        try:
            result = self.pipeline.run(request, progress=progress)
        except IngestionError as error:
            job.status, job.stage, job.error = "failed", "failed", str(error)
            logger.warning("ingestion %s failed: %s", job_id, error)
        except Exception as error:
            job.status, job.stage = "failed", "failed"
            job.error = f"unexpected error: {error.__class__.__name__}: {str(error)[:300]}"
            logger.exception("ingestion %s crashed", job_id)
        else:
            job.status, job.stage, job.progress = "succeeded", "done", 1.0
            job.stats, job.warnings = result.stats, tuple(result.warnings[:20])
            job.message = f"Ingested {result.stats.get('files', 0)} files in {result.duration_seconds}s"
        job.finished_at = _now()
        self._save(job)
        return job

    def _save(self, job: JobRecord) -> None:
        job.updated_at = _now()
        try:
            self.store.upsert_nodes([job.to_node()])
        except Exception:  # losing a progress update must not abort the ingestion itself
            logger.warning("could not persist progress of job %s", job.id, exc_info=True)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
