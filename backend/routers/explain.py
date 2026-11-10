from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend.deps import Repository, State, node_in_repository
from backend.schemas import ExplainBody
from graph_engine.schema import NodeLabel
from rag_engine.engine import ChatTurn

logger = logging.getLogger(__name__)
router = APIRouter(tags=["explain"])


@router.post("/repositories/{repository_id}/explain", response_model=None)
def explain(repository: Repository, body: ExplainBody, state: State) -> StreamingResponse | dict[str, Any]:
    """Answer a question about the repository, grounded in graph evidence.

    With ``stream: true`` (default) the response is newline-delimited JSON, one event
    per line: ``context`` (the evidence used), ``token`` (answer text), ``warning``,
    ``citations`` (each citation checked against the evidence and the repository)
    and ``done``. With ``stream: false`` the same information arrives as one document.
    """
    if body.focus_node_id is not None:
        node_in_repository(state, repository, body.focus_node_id)
    engine = state.container.rag
    repository_id = repository.repository_id
    history = [ChatTurn(turn.role, turn.content) for turn in body.history]

    if not body.stream:
        return engine.answer(repository_id, body.question, focus_node_id=body.focus_node_id, history=history)

    def events() -> Iterator[bytes]:
        try:
            for event in engine.stream(
                repository_id, body.question, focus_node_id=body.focus_node_id, history=history
            ):
                yield json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        except Exception:  # the status line is already sent; report the failure in-band
            logger.exception("explain stream failed")
            failure = {"type": "error", "message": "The answer could not be completed. See the server log."}
            yield json.dumps(failure).encode() + b"\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/repositories/{repository_id}/suggestions")
def suggestions(repository: Repository, state: State) -> dict[str, Any]:
    """Starter questions derived from what ingestion found notable in this repository."""
    raw = repository.get("insights_json")
    insights: dict[str, Any] = json.loads(raw) if raw else {}
    questions: list[dict[str, Any]] = []
    used: set[str] = set()

    def items(key: str, nested: str | None = None) -> list[dict[str, Any]]:
        value = insights.get(key)
        if nested is not None and isinstance(value, dict):
            value = value.get(nested)
        return [item for item in value or [] if isinstance(item, dict) and item.get("id") not in used]

    def add(template: str, item: dict[str, Any]) -> None:
        used.add(str(item.get("id")))
        questions.append({"question": template.format(name=item["name"]), "node_id": item.get("id")})

    entries = state.container.store.find_nodes(
        repository.repository_id, labels=[NodeLabel.FUNCTION], where={"is_entry_point": True}, limit=50
    )
    entries = [node for node in entries if not node.get("is_test")]
    entries.sort(key=lambda node: (node.get("entry_kind") != "http", -int(node.get("fan_out") or 0)))
    if entries:
        entry = entries[0]
        add(
            "What happens, step by step, when {name} runs? Include every table it touches.",
            {"id": entry.id, "name": entry.qualified_name},
        )
    for table in items("hot_tables")[:1]:
        add("Which code writes to {name}, and are there triggers or other hidden side effects?", table)
    for dead in items("dead_candidates", "largest")[:1]:
        add("Is {name} still used anywhere, and why was it written? Would it be safe to delete?", dead)
    for old in items("oldest_code")[:1]:
        add("Why does {name} work the way it does? What do the commits say about the reasons?", old)
    for complicated in items("most_complex")[:1]:
        add("What are the responsibilities of {name}, and what would break if it changed?", complicated)
    for popular in items("most_called")[:1]:
        add("Who depends on {name}, and who has been maintaining it?", popular)
    return {"suggestions": questions[:6]}
