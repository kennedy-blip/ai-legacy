"""Knowledge-graph vocabulary.

This module is the single source of truth for node labels, relationship types and
identifier construction. Every store implementation, the ingestion pipeline, the
retrieval engine and the HTTP API import their vocabulary from here, so a label or
relationship can never be spelled two different ways in two places.

Graph model (``*`` marks an addition to the minimal schema in the product brief)::

    (Repository)-[:CONTAINS]->(File)-[:CONTAINS]->(Class)-[:CONTAINS]->(Function)
    (File)-[:CONTAINS]->(Function)                       module-level functions
    (Function)-[:CONTAINS]->(DatabaseQuery)              SQL found inside the function
    (Function)-[:CALLS {lines, confidence, resolution}]->(Function)
    (Class)-[:INHERITS_FROM {kind}]->(Class)
    (Function|DatabaseQuery)-[:READS_FROM_DB]->(DatabaseTable)
    (Function|DatabaseQuery)-[:MUTATES_TABLE]->(DatabaseTable)
    (File|Class|Function)-[:TOUCHED_BY {lines}]->(Commit)
    (Commit)-[:AUTHORED_BY]->(Developer)                 *
    (DatabaseTable)-[:FIRES_TRIGGER {events}]->(Function) *  SQL triggers
    (Function)-[:OBSERVED_CALL {count, total_ms}]->(Function)       *  runtime telemetry
    (Function)-[:OBSERVED_READ|OBSERVED_WRITE]->(DatabaseTable)     *  runtime telemetry
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Final


class NodeLabel(StrEnum):
    """Node labels. Every node additionally carries :data:`BASE_LABEL` in Neo4j."""

    REPOSITORY = "Repository"
    FILE = "File"
    CLASS = "Class"
    FUNCTION = "Function"
    DATABASE_QUERY = "DatabaseQuery"
    DATABASE_TABLE = "DatabaseTable"
    COMMIT = "Commit"
    DEVELOPER = "Developer"
    INGESTION_JOB = "IngestionJob"


class RelType(StrEnum):
    """Relationship types."""

    CONTAINS = "CONTAINS"
    CALLS = "CALLS"
    INHERITS_FROM = "INHERITS_FROM"
    READS_FROM_DB = "READS_FROM_DB"
    MUTATES_TABLE = "MUTATES_TABLE"
    TOUCHED_BY = "TOUCHED_BY"
    AUTHORED_BY = "AUTHORED_BY"
    FIRES_TRIGGER = "FIRES_TRIGGER"
    OBSERVED_CALL = "OBSERVED_CALL"
    OBSERVED_READ = "OBSERVED_READ"
    OBSERVED_WRITE = "OBSERVED_WRITE"


class Direction(StrEnum):
    """Traversal direction relative to the node being expanded."""

    OUT = "out"
    IN = "in"
    BOTH = "both"


#: Shared secondary label carried by every node in Neo4j so that ``id`` lookups hit a
#: single uniqueness constraint regardless of the domain label.
BASE_LABEL: Final = "Entity"

#: Relationship types that describe code structure derived from static analysis.
STATIC_CODE_RELS: Final = frozenset(
    {
        RelType.CONTAINS,
        RelType.CALLS,
        RelType.INHERITS_FROM,
        RelType.READS_FROM_DB,
        RelType.MUTATES_TABLE,
        RelType.FIRES_TRIGGER,
    }
)

#: Relationship types written by the runtime-telemetry collector. They survive a
#: re-ingestion of the repository as long as both endpoints still exist.
RUNTIME_RELS: Final = frozenset({RelType.OBSERVED_CALL, RelType.OBSERVED_READ, RelType.OBSERVED_WRITE})

#: Relationship types connecting code to the database layer.
DATABASE_RELS: Final = frozenset({RelType.READS_FROM_DB, RelType.MUTATES_TABLE})

_ID_PREFIX: Final[dict[NodeLabel, str]] = {
    NodeLabel.REPOSITORY: "repo",
    NodeLabel.FILE: "file",
    NodeLabel.CLASS: "cls",
    NodeLabel.FUNCTION: "fn",
    NodeLabel.DATABASE_QUERY: "qry",
    NodeLabel.DATABASE_TABLE: "tbl",
    NodeLabel.COMMIT: "cmt",
    NodeLabel.DEVELOPER: "dev",
    NodeLabel.INGESTION_JOB: "job",
}

_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")
_IDENTIFIER_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def make_id(label: NodeLabel, repository_id: str, *parts: str | int) -> str:
    """Build a deterministic, URL-safe node id.

    The id is a pure function of the repository and the entity's natural key, so
    re-ingesting unchanged code produces the same ids. That stability is what lets
    runtime-telemetry edges and deep links survive a re-ingestion.
    """
    digest = hashlib.blake2b(digest_size=10)
    digest.update(repository_id.encode("utf-8"))
    for part in parts:
        digest.update(b"\x1f")
        digest.update(str(part).encode("utf-8", "surrogatepass"))
    return f"{_ID_PREFIX[label]}_{digest.hexdigest()}"


def make_repository_id(name: str, source: str) -> str:
    """Derive a readable, stable repository id from its display name and source."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")[:40] or "repo"
    suffix = hashlib.blake2b(source.encode("utf-8"), digest_size=4).hexdigest()
    return f"{slug}-{suffix}"


def is_safe_identifier(value: str) -> bool:
    """True when ``value`` is safe to interpolate into a query as a property key."""
    return bool(_IDENTIFIER_RE.match(value))
