"""Celery application and the ingestion task.

Start a worker with::

    celery -A backend.celery_app worker --loglevel=info --concurrency=2

Each worker process builds its own service container on first use. The task only
receives the job id and the request; all state lives in the graph database.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Celery
from celery.signals import worker_process_shutdown

from backend.settings import get_settings
from ingestion.pipeline import IngestionRequest

logger = logging.getLogger(__name__)

_settings = get_settings()

celery_app = Celery("archaeo", broker=_settings.redis_url)
celery_app.conf.update(
    task_default_queue="archaeo.ingest",
    task_serializer="json",
    accept_content=["json"],
    # State is tracked in the graph database; results would only duplicate it (and would
    # keep task arguments - which may include an access token - in Redis for longer).
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=4 * 60 * 60,
    task_soft_time_limit=4 * 60 * 60 - 120,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 5 * 60 * 60},
)

_manager: Any = None


def _job_manager() -> Any:
    """Per-process job manager, created lazily so importing this module stays cheap."""
    global _manager  # noqa: PLW0603
    if _manager is None:
        from backend.container import Container  # noqa: PLC0415
        from backend.jobs import JobManager  # noqa: PLC0415

        container = Container.build(_settings)
        _manager = JobManager(container.store, container.pipeline)
    return _manager


@worker_process_shutdown.connect  # type: ignore[untyped-decorator]
def _close(**_: Any) -> None:
    if _manager is not None:
        _manager.shutdown()


@celery_app.task(name="archaeo.ingest")  # type: ignore[untyped-decorator]
def ingest_repository(job_id: str, request: dict[str, Any]) -> None:
    payload = IngestionRequest(
        source=request["source"],
        name=request.get("name"),
        branch=request.get("branch"),
        token=request.get("token"),
        include=tuple(request.get("include") or ()),
        demo=bool(request.get("demo")),
    )
    _job_manager().run(job_id, payload)


def dispatch(job_id: str, request: IngestionRequest) -> None:
    """Queue ``request`` for a worker. Used as the :class:`backend.jobs.JobManager` dispatcher."""
    ingest_repository.apply_async(
        kwargs={
            "job_id": job_id,
            "request": {
                "source": request.source,
                "name": request.name,
                "branch": request.branch,
                "token": request.token,
                "include": list(request.include),
                "demo": request.demo,
            },
        },
        # Never echo task arguments (a token may be among them) into logs or events.
        argsrepr="(…)",
        kwargsrepr="{…}",
    )
