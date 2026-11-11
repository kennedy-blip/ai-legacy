"""Adapter from OTLP/HTTP JSON trace exports to collector events.

Accepts the body an OpenTelemetry exporter POSTs to ``/v1/traces`` with
``Content-Type: application/json``. Spans are linked through ``parentSpanId``
*within one request*: a span whose parent arrived in another batch contributes
no call edge (the collector is stateless by design).

Function identity is taken from the code semantic-convention attributes
(``code.function.name``, or the older ``code.namespace`` + ``code.function``)
and falls back to the span name. Database spans (``db.*`` attributes) become
query events on the nearest ancestor that maps to code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from parsers.sql_analysis import analyze_sql

_MAX_SPANS = 20_000


def _attributes(raw: Any) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for item in raw or ():
        if not isinstance(item, Mapping):
            continue
        value = item.get("value")
        if isinstance(value, Mapping):
            for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
                if key in value:
                    attributes[str(item.get("key"))] = value[key]
                    break
    return attributes


def _symbol(name: str, attributes: Mapping[str, Any]) -> str:
    function = attributes.get("code.function.name") or attributes.get("code.function")
    namespace = attributes.get("code.namespace")
    if function and namespace and "." not in str(function):
        return f"{namespace}.{function}"
    return str(function or name)


def _duration_ms(span: Mapping[str, Any]) -> float:
    try:
        return max((int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1_000_000, 0.0)
    except (KeyError, TypeError, ValueError):
        return 0.0


def spans_to_events(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten an OTLP JSON export into ``call`` and ``query`` events."""
    spans: dict[str, dict[str, Any]] = {}
    for resource in payload.get("resourceSpans") or ():
        for scope in (resource or {}).get("scopeSpans") or ():
            for span in (scope or {}).get("spans") or ():
                if not isinstance(span, Mapping) or not span.get("spanId") or len(spans) >= _MAX_SPANS:
                    continue
                attributes = _attributes(span.get("attributes"))
                spans[str(span["spanId"])] = {
                    "parent": str(span.get("parentSpanId") or ""),
                    "symbol": _symbol(str(span.get("name") or ""), attributes),
                    "attributes": attributes,
                    "duration_ms": _duration_ms(span),
                    "error": (span.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR"),
                    "is_db": any(key.startswith("db.") for key in attributes),
                }

    def code_ancestor(span_id: str) -> dict[str, Any] | None:
        seen: set[str] = set()
        current = spans.get(span_id)
        while current is not None and current["is_db"]:
            parent = current["parent"]
            if not parent or parent in seen:
                return None
            seen.add(parent)
            current = spans.get(parent)
        return current

    events: list[dict[str, Any]] = []
    for span in spans.values():
        parent = code_ancestor(span["parent"]) if span["parent"] else None
        if parent is None:
            continue
        attributes = span["attributes"]
        if span["is_db"]:
            operation = attributes.get("db.operation.name") or attributes.get("db.operation")
            tables = [attributes.get("db.collection.name") or attributes.get("db.sql.table")]
            statement = attributes.get("db.query.text") or attributes.get("db.statement")
            if not tables[0] and statement:
                analysis = analyze_sql(str(statement))
                tables = [*analysis.writes, *analysis.reads]
                written = set(analysis.writes)
                operation = operation or analysis.operation
                for table in tables:
                    events.append(
                        {
                            "type": "query",
                            "function": parent["symbol"],
                            "table": table,
                            "operation": (operation if table in written else "SELECT"),
                            "duration_ms": span["duration_ms"],
                            "error": span["error"],
                        }
                    )
                continue
            if tables[0]:
                events.append(
                    {
                        "type": "query",
                        "function": parent["symbol"],
                        "table": str(tables[0]),
                        "operation": str(operation or "QUERY"),
                        "duration_ms": span["duration_ms"],
                        "error": span["error"],
                    }
                )
        else:
            events.append(
                {
                    "type": "call",
                    "caller": parent["symbol"],
                    "callee": span["symbol"],
                    "duration_ms": span["duration_ms"],
                    "error": span["error"],
                }
            )
    return events
