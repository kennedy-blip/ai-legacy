"""Turns runtime events into ``OBSERVED_*`` relationships on the code graph.

Static analysis cannot see dynamic dispatch, reflection or dependency injection.
A lightweight agent - an OpenTelemetry exporter, an APM hook, a logging aspect -
can report what actually ran. The collector resolves the reported symbols to graph
nodes and accumulates counters on ``OBSERVED_CALL`` / ``OBSERVED_READ`` /
``OBSERVED_WRITE`` edges, which the API overlays on the static call graph.

Event format (one JSON object per event)::

    {"type": "call",  "caller": "PaymentService.applyPayment", "callee": "LedgerDao.insertEntry",
     "count": 1, "duration_ms": 3.2, "error": false}
    {"type": "query", "function": "LedgerDao.insertEntry", "table": "tbl_ledger",
     "operation": "INSERT", "duration_ms": 1.1}

Symbols may be ``Class.method``, a fully qualified ``com.acme.Class.method``,
``ns::Class::method``, or a ``File.java:123`` location.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from graph_engine import locator
from graph_engine.models import GraphEdge, GraphNode
from graph_engine.schema import NodeLabel, RelType, make_id
from graph_engine.store import GraphStore

_READ_OPERATIONS: Final = frozenset({"SELECT", "READ", "QUERY", "GET", "FIND"})
_ADDITIVE: Final = ("count", "total_ms", "errors")
_MAXIMUM: Final = ("max_ms",)
_PRESERVE: Final = ("first_seen",)
_MAX_CACHE: Final = 20_000
MAX_BATCH: Final = 5000


@dataclass(slots=True)
class _Counter:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    errors: int = 0
    operations: dict[str, None] = field(default_factory=dict)


@dataclass(slots=True)
class IngestSummary:
    received: int = 0
    accepted: int = 0
    edges: int = 0
    #: Symbols that matched no node - usually library code or a naming mismatch.
    unmatched: dict[str, int] = field(default_factory=dict)
    invalid: int = 0
    #: ``(source id, relationship, target id, count)`` for live subscribers.
    observed: list[tuple[str, str, str, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        top = sorted(self.unmatched.items(), key=lambda item: (-item[1], item[0]))[:20]
        return {
            "received": self.received,
            "accepted": self.accepted,
            "edges_updated": self.edges,
            "invalid": self.invalid,
            "unmatched_symbols": dict(top),
        }


class TelemetryCollector:
    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self._cache: dict[tuple[str, str], str | None] = {}
        self._lock = threading.Lock()

    # --------------------------------------------------------------- resolution

    def resolve_function(self, repository_id: str, symbol: str) -> str | None:
        """Node id of the function ``symbol`` denotes, or ``None``."""
        key = (repository_id, symbol)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        resolved = self._resolve(repository_id, symbol.strip())
        with self._lock:
            if len(self._cache) >= _MAX_CACHE:
                self._cache.clear()
            self._cache[key] = resolved
        return resolved

    def _resolve(self, repository_id: str, symbol: str) -> str | None:
        if not symbol or len(symbol) > 500:
            return None
        location = locator.parse_location(symbol)
        if location is not None:
            for place in locator.resolve_location(self.store, repository_id, location[0], location[1]):
                if place.scope is not None and place.scope.label is NodeLabel.FUNCTION:
                    return place.scope.id
            return None
        normalised = symbol.split("(", 1)[0].replace("::", ".").replace("#", ".").replace("$", ".")
        parts = [part for part in normalised.split(".") if part]
        if not parts:
            return None
        labels = [NodeLabel.FUNCTION]
        # Longest qualified suffix first: com.acme.Billing.pay -> "Billing.pay" before "pay".
        for size in range(min(len(parts), 3), 1, -1):
            candidate = ".".join(parts[-size:])
            for name in (candidate, candidate.replace(".", "::")):
                matches = self.store.find_nodes(repository_id, labels=labels, qualified_name=name, limit=3)
                if matches:
                    return self._pick(matches, parts)
        # A bare name is only trusted when it is unambiguous.
        if len(parts) == 1:
            matches = self.store.find_nodes(repository_id, labels=labels, name=parts[0], limit=2)
            if len(matches) == 1:
                return matches[0].id
        return None

    @staticmethod
    def _pick(candidates: list[GraphNode], parts: list[str]) -> str:
        """Among overloads / same-named scopes prefer the one whose path matches the package."""
        if len(candidates) > 1 and len(parts) > 2:
            package = "/".join(parts[:-2])
            for candidate in candidates:
                if package and package in (candidate.path or ""):
                    return candidate.id
        return candidates[0].id

    def resolve_table(self, repository_id: str, name: str) -> str | None:
        table = name.strip().strip('"`[]').rsplit(".", 1)[-1].lower()
        if not table:
            return None
        node_id = make_id(NodeLabel.DATABASE_TABLE, repository_id, table)
        key = (repository_id, f"table:{table}")
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        resolved = node_id if self.store.get_node(node_id) is not None else None
        with self._lock:
            self._cache[key] = resolved
        return resolved

    # ---------------------------------------------------------------- ingestion

    def ingest(self, repository_id: str, events: Iterable[Any], *, source: str = "agent") -> IngestSummary:
        """Aggregate ``events`` and merge them into the graph in one write."""
        summary = IngestSummary()
        counters: dict[tuple[str, RelType, str], _Counter] = {}

        def miss(symbol: Any) -> None:
            text = str(symbol)[:120]
            summary.unmatched[text] = summary.unmatched.get(text, 0) + 1

        for event in events:
            summary.received += 1
            if not isinstance(event, Mapping):
                summary.invalid += 1
                continue
            kind = event.get("type")
            try:
                count = max(int(event.get("count") or 1), 1)
                duration = max(float(event.get("duration_ms") or 0.0), 0.0)
            except (TypeError, ValueError):
                summary.invalid += 1
                continue
            if kind == "call":
                caller, callee = event.get("caller"), event.get("callee")
                if not isinstance(caller, str) or not isinstance(callee, str):
                    summary.invalid += 1
                    continue
                source_id = self.resolve_function(repository_id, caller)
                target_id = self.resolve_function(repository_id, callee)
                if source_id is None:
                    miss(caller)
                if target_id is None:
                    miss(callee)
                if source_id is None or target_id is None:
                    continue
                key = (source_id, RelType.OBSERVED_CALL, target_id)
                operation = None
            elif kind == "query":
                function, table = event.get("function"), event.get("table")
                if not isinstance(function, str) or not isinstance(table, str):
                    summary.invalid += 1
                    continue
                source_id = self.resolve_function(repository_id, function)
                target_id = self.resolve_table(repository_id, table)
                if source_id is None:
                    miss(function)
                if target_id is None:
                    miss(f"table {table}")
                if source_id is None or target_id is None:
                    continue
                operation = str(event.get("operation") or "QUERY").upper()[:16]
                relation = RelType.OBSERVED_READ if operation in _READ_OPERATIONS else RelType.OBSERVED_WRITE
                key = (source_id, relation, target_id)
            else:
                summary.invalid += 1
                continue
            counter = counters.setdefault(key, _Counter())
            counter.count += count
            counter.total_ms += duration * count
            counter.max_ms = max(counter.max_ms, duration)
            counter.errors += count if event.get("error") else 0
            if operation:
                counter.operations[operation] = None
            summary.accepted += 1

        if counters:
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            edges = [
                GraphEdge(
                    source_id,
                    target_id,
                    relation,
                    repository_id,
                    {
                        "count": counter.count,
                        "total_ms": round(counter.total_ms, 3),
                        "max_ms": round(counter.max_ms, 3),
                        "errors": counter.errors,
                        "first_seen": now,
                        "last_seen": now,
                        "agent": source,
                        "operations": list(counter.operations) or None,
                    },
                )
                for (source_id, relation, target_id), counter in counters.items()
            ]
            summary.edges = self.store.accumulate_edges(
                edges, additive=_ADDITIVE, maximum=_MAXIMUM, preserve=_PRESERVE
            )
            summary.observed = [
                (source_id, relation.value, target_id, counter.count)
                for (source_id, relation, target_id), counter in counters.items()
            ]
        return summary
