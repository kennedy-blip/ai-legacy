"""FastAPI application factory.

Run with ``uvicorn backend.main:create_app --factory`` or ``archaeo serve``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import __version__
from backend.container import Container
from backend.demo import seed_demo
from backend.deps import AppState, require_token
from backend.jobs import JobManager
from backend.routers import explain, graph, health, repositories, telemetry
from backend.settings import Settings, get_settings
from backend.telemetry.collector import TelemetryCollector
from backend.telemetry.hub import TelemetryHub
from graph_engine.schema import NodeLabel
from graph_engine.store import GraphStoreError
from rag_engine.engine import UnknownRepositoryError
from rag_engine.vector_index import VectorIndexError

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
#: Largest accepted request body. Telemetry batches are the biggest legitimate payload.
MAX_BODY_BYTES = 8 * 1024 * 1024


def build_state(settings: Settings) -> AppState:
    container = Container.build(settings)
    dispatcher = None
    if settings.job_backend == "celery":
        from backend.celery_app import dispatch  # noqa: PLC0415

        dispatcher = dispatch
    return AppState(
        container=container,
        jobs=JobManager(container.store, container.pipeline, dispatcher=dispatcher),
        collector=TelemetryCollector(container.store),
        hub=TelemetryHub(),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = await asyncio.to_thread(build_state, settings)
        state.hub.bind(asyncio.get_running_loop())
        app.state.archaeo = state
        for warning in state.container.warnings:
            logger.warning(warning)
        if settings.seed_demo:
            # In a thread: building and ingesting the demo must not delay readiness.
            threading.Thread(target=_seed_if_empty, args=(state,), name="seed-demo", daemon=True).start()
        logger.info(
            "Archaeo-Code %s ready: graph=%s jobs=%s answers=%s",
            __version__,
            settings.graph_backend,
            settings.job_backend,
            state.container.rag.mode,
        )
        try:
            yield
        finally:
            state.jobs.shutdown()
            await asyncio.to_thread(state.container.close)

    app = FastAPI(
        title="Archaeo-Code API",
        version=__version__,
        summary="Code knowledge graph, git archeology, runtime telemetry and grounded explanations.",
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def limit_body(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "request body too large"}, status.HTTP_413_CONTENT_TOO_LARGE)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    @app.exception_handler(GraphStoreError)
    async def graph_unavailable(_: Request, error: GraphStoreError) -> JSONResponse:
        logger.error("graph store error: %s", error)
        return JSONResponse(
            {"detail": "the graph database is unavailable"}, status.HTTP_503_SERVICE_UNAVAILABLE
        )

    @app.exception_handler(VectorIndexError)
    async def vectors_unavailable(_: Request, error: VectorIndexError) -> JSONResponse:
        logger.error("vector index error: %s", error)
        return JSONResponse(
            {"detail": "the semantic index is unavailable"}, status.HTTP_503_SERVICE_UNAVAILABLE
        )

    @app.exception_handler(UnknownRepositoryError)
    async def unknown_repository(_: Request, __: UnknownRepositoryError) -> JSONResponse:
        return JSONResponse({"detail": "repository not found"}, status.HTTP_404_NOT_FOUND)

    public = APIRouter(prefix=API_PREFIX)
    public.include_router(health.router)
    public.include_router(telemetry.ws_router)  # authenticates inside the handler
    protected = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_token)])
    for module in (repositories, graph, explain, telemetry):
        protected.include_router(module.router)
    app.include_router(public)
    app.include_router(protected)
    return app


def _seed_if_empty(state: AppState) -> None:
    try:
        existing = state.container.store.find_nodes(None, labels=[NodeLabel.REPOSITORY], limit=1)
        pending = state.jobs.list(limit=1)
    except Exception:
        logger.exception("could not check whether the demo repository is needed")
        return
    if not existing and not pending:
        job = seed_demo(state.container.settings, state.jobs)
        if job is not None:
            logger.info("seeding the demo repository (job %s)", job.id)
