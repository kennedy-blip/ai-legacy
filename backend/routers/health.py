from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status

from backend import __version__
from backend.deps import State, require_token
from parsers.registry import SUPPORTED_EXTENSIONS

router = APIRouter(tags=["system"])


@router.get("/health")
def health(state: State, response: Response) -> dict[str, Any]:
    """Liveness and dependency status. Unauthenticated; reveals no configuration values."""
    components = state.container.health()
    healthy = components["graph"]["status"] == "ok" and components["vectors"]["status"] != "error"
    if components["graph"]["status"] != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "components": {name: {"status": info["status"]} for name, info in components.items()},
    }


@router.get("/system", dependencies=[Depends(require_token)])
def system(state: State) -> dict[str, Any]:
    """Configuration the UI adapts to: which stores and model are active, and current limits."""
    settings = state.container.settings
    return {
        "version": __version__,
        "components": state.container.health(),
        "warnings": state.container.warnings,
        "graph_backend": settings.graph_backend,
        "job_backend": settings.job_backend,
        "answer_mode": state.container.rag.mode,
        "llm_model": state.container.rag.llm_label,
        "embedding": state.container.index.embedder.name if state.container.index else None,
        "vector_backend": state.container.index.backend if state.container.index else None,
        # True on instances that only serve existing graphs: no ingestion, deletion or telemetry intake.
        "read_only": settings.read_only,
        "telemetry_enabled": settings.telemetry_enabled,
        "local_sources_enabled": bool(settings.allowed_local_roots),
        "allowed_git_hosts": settings.allowed_git_hosts,
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
    }
