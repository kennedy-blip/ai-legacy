"""Request bodies of the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from backend.telemetry.collector import MAX_BATCH


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IngestBody(_Body):
    #: ``https://`` / ``ssh://`` Git URL, or a server-side path under an allowed root.
    source: str = Field(min_length=1, max_length=2000)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    branch: str | None = Field(default=None, min_length=1, max_length=250, pattern=r"^[\w./-]+$")
    #: Access token for a private repository. Used for the clone only and never stored.
    token: SecretStr | None = None
    #: Restrict ingestion to these repository-relative path prefixes.
    include: list[str] = Field(default_factory=list, max_length=50)


class ReingestBody(_Body):
    #: Private repositories need the token again: it is never stored.
    token: SecretStr | None = None


class ChatTurnBody(_Body):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class ExplainBody(_Body):
    question: str = Field(min_length=1, max_length=4000)
    #: Node the user is looking at; retrieval starts from it.
    focus_node_id: str | None = Field(default=None, max_length=64)
    history: list[ChatTurnBody] = Field(default_factory=list, max_length=20)
    #: ``true``: newline-delimited JSON events; ``false``: one JSON document.
    stream: bool = True


class TelemetryBody(_Body):
    events: list[dict[str, Any]] = Field(max_length=MAX_BATCH)
    #: Free-form tag of the reporting agent, stored on the observed relationships.
    source: str = Field(default="agent", pattern=r"^[a-z0-9_.-]{1,32}$")


class SimulateBody(_Body):
    traces: int = Field(default=200, ge=1, le=5000)
    seed: int | None = None
