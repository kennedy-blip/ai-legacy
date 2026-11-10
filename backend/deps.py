"""FastAPI dependencies: application state, authentication and common lookups."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.container import Container
from backend.jobs import JobManager
from backend.telemetry.collector import TelemetryCollector
from backend.telemetry.hub import TelemetryHub
from graph_engine.models import GraphNode
from graph_engine.schema import NodeLabel, make_id
from rag_engine.source import SourceReader


@dataclass(slots=True)
class AppState:
    container: Container
    jobs: JobManager
    collector: TelemetryCollector
    hub: TelemetryHub


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.archaeo
    return state


State = Annotated[AppState, Depends(get_state)]

_bearer = HTTPBearer(auto_error=False)


def token_matches(expected: str, presented: str | None) -> bool:
    return presented is not None and secrets.compare_digest(expected.encode(), presented.encode())


def require_token(
    state: State, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> None:
    """Enforce ``Authorization: Bearer <ARCHAEO_API_TOKEN>`` when a token is configured."""
    expected = state.container.settings.api_token
    if expected is None:
        return
    presented = credentials.credentials if credentials is not None else None
    if not token_matches(expected.get_secret_value(), presented):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_writable(state: State) -> None:
    """Refuse requests that change the graph on a read-only instance."""
    if state.container.settings.read_only:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this instance is read-only: it serves existing graphs and cannot ingest, delete or record "
            "telemetry. Run Archaeo-Code yourself to analyse your own repositories.",
        )


#: Route dependency for every endpoint that writes.
Writable = Depends(require_writable)


def websocket_authorized(websocket: WebSocket) -> bool:
    """Browsers cannot set headers on WebSockets, so the token may also be a ``token`` query parameter."""
    state: AppState = websocket.app.state.archaeo
    expected = state.container.settings.api_token
    if expected is None:
        return True
    header = websocket.headers.get("authorization", "")
    presented = header[7:] if header.lower().startswith("bearer ") else websocket.query_params.get("token")
    return token_matches(expected.get_secret_value(), presented)


def get_repository(repository_id: str, state: State) -> GraphNode:
    node = state.container.store.get_node(make_id(NodeLabel.REPOSITORY, repository_id))
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    return node


Repository = Annotated[GraphNode, Depends(get_repository)]


def node_in_repository(state: AppState, repository: GraphNode, node_id: str) -> GraphNode:
    """A node by id, restricted to ``repository`` so ids cannot be used to read across repositories."""
    node = state.container.store.get_node(node_id)
    if node is None or node.repository_id != repository.repository_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")
    return node


def source_reader(repository: GraphNode) -> SourceReader:
    return SourceReader(repository.get("workspace_path"))
