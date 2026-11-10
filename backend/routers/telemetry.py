from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from backend import serializers
from backend.deps import AppState, Repository, State, Writable, websocket_authorized
from backend.schemas import SimulateBody, TelemetryBody
from backend.telemetry.collector import MAX_BATCH, IngestSummary
from backend.telemetry.otlp import spans_to_events
from backend.telemetry.simulator import simulate_events
from graph_engine.algorithms import DEFAULT_MIN_CONFIDENCE
from graph_engine.schema import RUNTIME_RELS, NodeLabel, RelType, make_id

logger = logging.getLogger(__name__)
router = APIRouter(tags=["telemetry"])
#: The WebSocket endpoint authenticates itself (browsers cannot send an Authorization header).
ws_router = APIRouter()

_HEARTBEAT_SECONDS = 15.0
_MAX_WS_MESSAGE = 2_000_000


def _require_enabled(state: AppState) -> None:
    if not state.container.settings.telemetry_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "runtime telemetry is disabled (ARCHAEO_TELEMETRY_ENABLED)"
        )


def _ingest(state: AppState, repository_id: str, events: list[Any], source: str) -> IngestSummary:
    summary = state.collector.ingest(repository_id, events, source=source)
    if summary.observed:
        state.hub.publish(
            repository_id,
            {
                "type": "observed",
                "agent": source,
                "edges": [
                    {"source": origin, "type": relation, "target": target, "count": count}
                    for origin, relation, target, count in summary.observed[:500]
                ],
            },
        )
    return summary


@router.post("/repositories/{repository_id}/telemetry/events", dependencies=[Writable])
def post_events(repository: Repository, body: TelemetryBody, state: State) -> dict[str, Any]:
    """Report runtime calls and data access. See ``docs/telemetry.md`` for the event format."""
    _require_enabled(state)
    return _ingest(state, repository.repository_id, body.events, body.source).to_dict()


@router.post("/repositories/{repository_id}/telemetry/otlp", dependencies=[Writable])
def post_otlp(
    repository: Repository, state: State, payload: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """Accept an OTLP/HTTP JSON trace export (``ExportTraceServiceRequest``)."""
    _require_enabled(state)
    events = spans_to_events(payload)
    if len(events) > MAX_BATCH * 4:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "too many spans in one export")
    return _ingest(state, repository.repository_id, events, "otlp").to_dict()


@router.post("/repositories/{repository_id}/telemetry/simulate", dependencies=[Writable])
def simulate(repository: Repository, body: SimulateBody, state: State) -> dict[str, Any]:
    """Generate synthetic traffic from the static graph. Results are tagged ``simulated``."""
    _require_enabled(state)
    events = simulate_events(
        state.container.store, repository.repository_id, traces=body.traces, seed=body.seed
    )
    if not events:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "nothing to simulate: the repository has no detected entry points"
        )
    summary = _ingest(state, repository.repository_id, events, "simulated").to_dict()
    summary["traces"] = body.traces
    return summary


@router.delete("/repositories/{repository_id}/telemetry", dependencies=[Writable])
def clear(repository: Repository, state: State) -> dict[str, Any]:
    """Remove every runtime observation of the repository. The static graph is untouched."""
    removed = state.container.store.delete_edges(repository.repository_id, RUNTIME_RELS)
    state.hub.publish(repository.repository_id, {"type": "cleared"})
    return {"removed": removed}


@router.get("/repositories/{repository_id}/telemetry/summary")
def summary(
    repository: Repository,
    state: State,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Compare what was observed at runtime with what static analysis predicted."""
    store, repository_id = state.container.store, repository.repository_id
    observed = store.list_edges(repository_id, RUNTIME_RELS)
    calls = [edge for edge in observed if edge.type is RelType.OBSERVED_CALL]
    data = [edge for edge in observed if edge.type is not RelType.OBSERVED_CALL]
    static = {(edge.source, edge.target): edge for edge in store.list_edges(repository_id, [RelType.CALLS])}
    static_data = {
        (edge.source, edge.target)
        for edge in store.list_edges(repository_id, [RelType.READS_FROM_DB, RelType.MUTATES_TABLE])
    }

    def count(edge: Any) -> int:
        return int(edge.properties.get("count") or 0)

    calls.sort(key=lambda edge: (-count(edge), edge.source, edge.target))
    data.sort(key=lambda edge: (-count(edge), edge.source, edge.target))
    runtime_only = [edge for edge in calls if (edge.source, edge.target) not in static]
    uncertain = [
        edge
        for edge in calls
        if (edge.source, edge.target) in static
        and static[(edge.source, edge.target)].confidence < DEFAULT_MIN_CONFIDENCE
    ]
    shown = [*calls[:limit], *runtime_only[:limit], *uncertain[:limit], *data[:limit]]

    # Functions static analysis says are called, but no trace ever reached.
    never: list[str] = []
    if calls:
        seen = {edge.target for edge in calls} | {edge.source for edge in calls}
        never = sorted(
            {target for (_, target), edge in static.items() if target not in seen and edge.confidence >= 0.5}
        )
    wanted = {edge.source for edge in shown} | {edge.target for edge in shown} | set(never[: limit * 2])
    nodes = {node.id: node for node in store.get_nodes(sorted(wanted))}
    never_nodes = [
        nodes[node_id]
        for node_id in never[: limit * 2]
        if node_id in nodes and not nodes[node_id].get("is_test") and not nodes[node_id].get("is_boilerplate")
    ][:limit]

    def row(edge: Any) -> dict[str, Any]:
        prediction = static.get((edge.source, edge.target))
        total, hits = float(edge.properties.get("total_ms") or 0.0), count(edge)
        return {
            **serializers.edge(edge),
            "mean_ms": round(total / hits, 3) if hits else None,
            "in_static_graph": (
                prediction is not None
                if edge.type is RelType.OBSERVED_CALL
                else (edge.source, edge.target) in static_data
            ),
            "static_confidence": prediction.confidence if prediction is not None else None,
            "static_resolution": prediction.properties.get("resolution") if prediction is not None else None,
        }

    sources = sorted({str(edge.properties.get("agent") or "agent") for edge in observed})
    last_seen = max((str(edge.properties.get("last_seen") or "") for edge in observed), default="") or None
    return {
        "totals": {
            "observed_edges": len(observed),
            "observed_calls": len(calls),
            "observed_data_access": len(data),
            "invocations": sum(count(edge) for edge in calls),
            "static_calls": len(static),
            "confirmed_static_calls": sum(1 for edge in calls if (edge.source, edge.target) in static),
            "runtime_only_calls": len(runtime_only),
            "never_observed_functions": len(never),
        },
        "sources": sources,
        "simulated": "simulated" in sources,
        "last_seen": last_seen,
        "live_subscribers": state.hub.subscriber_count(repository_id),
        "top_calls": [row(edge) for edge in calls[:limit]],
        "runtime_only": [row(edge) for edge in runtime_only[:limit]],
        "confirmed_guesses": [row(edge) for edge in uncertain[:limit]],
        "data_access": [row(edge) for edge in data[:limit]],
        "never_observed": [serializers.node_brief(node) for node in never_nodes],
        "nodes": {node_id: serializers.node_brief(node) for node_id, node in nodes.items()},
    }


@router.get("/repositories/{repository_id}/telemetry/live")
async def live(repository: Repository, request: Request, state: State) -> StreamingResponse:
    """Server-sent events with each batch of newly observed edges (for dashboards)."""
    repository_id = repository.repository_id
    queue = state.hub.subscribe(repository_id)

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield b": connected\n\n"
            while not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield b": keep-alive\n\n"
                    continue
                yield b"data: " + json.dumps(message, separators=(",", ":")).encode() + b"\n\n"
        finally:
            state.hub.unsubscribe(repository_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@ws_router.websocket("/telemetry/stream")
async def telemetry_stream(websocket: WebSocket, repository_id: str = Query(max_length=80)) -> None:
    """Bidirectional telemetry channel for agents.

    Send ``{"events": [...], "source": "my-agent"}`` messages (same format as the POST
    endpoint); each is answered with ``{"type": "ack", ...}``. Every connection also
    receives ``{"type": "observed", "edges": [...]}`` broadcasts for the repository.
    """
    state: AppState = websocket.app.state.archaeo
    if not websocket_authorized(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="missing or invalid API token")
        return
    known = await run_in_threadpool(
        state.container.store.get_node, make_id(NodeLabel.REPOSITORY, repository_id)
    )
    settings = state.container.settings
    if known is None or not settings.telemetry_enabled or settings.read_only:
        reason = (
            "this instance is read-only" if known is not None and settings.read_only else "unknown repository"
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=reason)
        return
    await websocket.accept()
    queue = state.hub.subscribe(repository_id)

    async def forward() -> None:
        while True:
            await websocket.send_json(await queue.get())

    forwarder = asyncio.create_task(forward())
    try:
        while True:
            text = await websocket.receive_text()
            if len(text) > _MAX_WS_MESSAGE:
                await websocket.send_json({"type": "error", "message": "message too large"})
                continue
            try:
                message = json.loads(text)
                events = message["events"]
                if not isinstance(events, list) or len(events) > MAX_BATCH:
                    raise ValueError
                source = str(message.get("source") or "agent")[:32]
            except (ValueError, KeyError, TypeError):
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": f'expected {{"events": [...]}} with at most {MAX_BATCH} events',
                    }
                )
                continue
            result = await run_in_threadpool(_ingest, state, repository_id, events, source)
            await websocket.send_json({"type": "ack", **result.to_dict()})
    except WebSocketDisconnect:
        pass
    finally:
        forwarder.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await forwarder
        state.hub.unsubscribe(repository_id, queue)
