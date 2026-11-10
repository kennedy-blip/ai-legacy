"""Cypher statements executed by :class:`graph_engine.neo4j_store.Neo4jGraphStore`.

Every statement the Neo4j store can send is produced by a pure function in this
module. That keeps the Cypher reviewable in one place and makes the complete set
enumerable (:func:`all_statements`), which ``tools/cypher-lint/lint.mjs`` feeds to
Neo4j's official parser and semantic analyser as a static check.

Labels, relationship types and property keys are the only values ever interpolated
into query text. They come from the :mod:`graph_engine.schema` enums or are
validated by :func:`graph_engine.schema.is_safe_identifier`; all data values are
bound as parameters.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from importlib import resources

from graph_engine.schema import BASE_LABEL, RUNTIME_RELS, Direction, NodeLabel, RelType, is_safe_identifier

#: Properties the Neo4j store maintains itself for indexed, case-insensitive lookups.
#: They are stripped again when nodes are read back.
INTERNAL_NODE_KEYS = frozenset({"name_lower", "qualified_name_lower"})


def schema_statements() -> list[str]:
    """Constraint and index DDL from ``cypher/schema.cypher``, one statement each."""
    text = resources.files("graph_engine").joinpath("cypher/schema.cypher").read_text(encoding="utf-8")
    without_comments = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    return [statement.strip() for statement in without_comments.split(";") if statement.strip()]


def _rel_pattern(rel_types: Collection[RelType]) -> str:
    if not rel_types:
        raise ValueError("at least one relationship type is required")
    return "|".join(sorted(rel_type.value for rel_type in rel_types))


def _checked(keys: Sequence[str]) -> Sequence[str]:
    for key in keys:
        if not is_safe_identifier(key):
            raise ValueError(f"unsafe property name: {key!r}")
    return keys


# ---------------------------------------------------------------------------- writes


def upsert_nodes(label: NodeLabel) -> str:
    """Parameters: ``rows = [{id, props}]`` where ``props`` is the full property map."""
    return f"""
UNWIND $rows AS row
MERGE (n:{BASE_LABEL} {{id: row.id}})
SET n = row.props, n:{label.value}
RETURN count(n) AS written
"""


def upsert_edges(rel_type: RelType) -> str:
    """Parameters: ``rows = [{source, target, props}]``. Missing endpoints skip the row."""
    return f"""
UNWIND $rows AS row
MATCH (a:{BASE_LABEL} {{id: row.source}})
MATCH (b:{BASE_LABEL} {{id: row.target}})
MERGE (a)-[r:{rel_type.value}]->(b)
SET r = row.props
RETURN count(r) AS written
"""


def accumulate_edges(rel_type: RelType, additive: Sequence[str], maximum: Sequence[str]) -> str:
    """Counter-style merge used for runtime telemetry.

    Parameters: ``rows = [{source, target, props, overwrite}]`` where ``overwrite``
    is ``props`` without the additive / maximum / preserved keys.
    """
    on_match = ["r += row.overwrite"]
    on_match.extend(
        f"r.{key} = coalesce(r.{key}, 0) + coalesce(row.props.{key}, 0)" for key in _checked(additive)
    )
    on_match.extend(
        f"r.{key} = CASE WHEN coalesce(row.props.{key}, 0) > coalesce(r.{key}, 0) "
        f"THEN row.props.{key} ELSE r.{key} END"
        for key in _checked(maximum)
    )
    assignments = ",\n    ".join(on_match)
    return f"""
UNWIND $rows AS row
MATCH (a:{BASE_LABEL} {{id: row.source}})
MATCH (b:{BASE_LABEL} {{id: row.target}})
MERGE (a)-[r:{rel_type.value}]->(b)
ON CREATE SET r = row.props
ON MATCH SET
    {assignments}
RETURN count(r) AS written
"""


def delete_repository_batch() -> str:
    """Parameters: ``repository_id``, ``keep`` (labels to retain), ``batch``.

    Run repeatedly until it reports ``deleted = 0`` so that no single transaction
    has to hold the whole repository in memory.
    """
    return f"""
MATCH (n:{BASE_LABEL} {{repository_id: $repository_id}})
WHERE NOT n.label IN $keep
WITH n LIMIT $batch
DETACH DELETE n
RETURN count(*) AS deleted
"""


def delete_edges_batch(rel_types: Collection[RelType]) -> str:
    """Parameters: ``repository_id``, ``batch``. Run until it reports ``deleted = 0``."""
    return f"""
MATCH (a:{BASE_LABEL} {{repository_id: $repository_id}})-[r:{_rel_pattern(rel_types)}]->(:{BASE_LABEL})
WITH r LIMIT $batch
DELETE r
RETURN count(*) AS deleted
"""


# ----------------------------------------------------------------------------- reads


def get_nodes() -> str:
    """Parameters: ``ids``."""
    return f"""
UNWIND $ids AS id
MATCH (n:{BASE_LABEL} {{id: id}})
RETURN properties(n) AS props
"""


def find_nodes(
    *,
    by_repository: bool,
    by_labels: bool,
    by_name: bool,
    by_qualified_name: bool,
    by_path: bool,
    where_keys: Sequence[str] = (),
) -> str:
    """Exact-match lookup.

    The WHERE clause is assembled from only the filters in use (rather than
    ``$param IS NULL OR ...`` guards) so the planner can pick the matching
    composite index. Parameters: ``repository_id``, ``labels``, ``name``,
    ``qualified_name``, ``path``, ``where_<key>``, ``limit``, ``offset``.
    """
    clauses: list[str] = []
    if by_repository:
        clauses.append("n.repository_id = $repository_id")
    if by_labels:
        clauses.append("n.label IN $labels")
    if by_name:
        clauses.append("n.name_lower = $name")
    if by_qualified_name:
        clauses.append("n.qualified_name_lower = $qualified_name")
    if by_path:
        clauses.append("n.path = $path")
    clauses.extend(f"n.{key} = $where_{key}" for key in _checked(where_keys))
    condition = f"WHERE {' AND '.join(clauses)}\n" if clauses else ""
    return f"""
MATCH (n:{BASE_LABEL})
{condition}RETURN properties(n) AS props
ORDER BY coalesce(n.name, ''), n.id
SKIP $offset LIMIT $limit
"""


def search_nodes(*, by_labels: bool) -> str:
    """Parameters: ``repository_id``, ``needle`` (lower-cased), ``labels``, ``limit``."""
    label_clause = "\n  AND n.label IN $labels" if by_labels else ""
    return f"""
MATCH (n:{BASE_LABEL})
WHERE n.repository_id = $repository_id
  AND (n.name_lower CONTAINS $needle OR n.qualified_name_lower CONTAINS $needle){label_clause}
RETURN properties(n) AS props
ORDER BY CASE
           WHEN n.name_lower = $needle THEN 0
           WHEN n.name_lower STARTS WITH $needle THEN 1
           ELSE 2
         END,
         size(n.name), n.name, n.id
LIMIT $limit
"""


def nodes_at(*, by_labels: bool) -> str:
    """Parameters: ``repository_id``, ``path``, ``line``, ``labels``."""
    label_clause = "\n  AND n.label IN $labels" if by_labels else ""
    return f"""
MATCH (n:{BASE_LABEL})
WHERE n.repository_id = $repository_id
  AND n.path = $path
  AND n.start_line <= $line
  AND n.end_line >= $line{label_clause}
RETURN properties(n) AS props
ORDER BY n.end_line - n.start_line, n.start_line DESC, n.id
"""


def list_edges(rel_types: Collection[RelType]) -> str:
    """Parameters: ``repository_id``."""
    return f"""
MATCH (a:{BASE_LABEL} {{repository_id: $repository_id}})-[r:{_rel_pattern(rel_types)}]->(b:{BASE_LABEL})
RETURN a.id AS source, b.id AS target, type(r) AS type, properties(r) AS props
"""


def expand(rel_types: Collection[RelType], direction: Direction) -> str:
    """Single-hop frontier expansion; one of these runs per hop of an N-hop traversal.

    Parameters: ``ids`` (the frontier), ``min_confidence``. ``direction`` must be
    ``OUT`` or ``IN``; the store issues both for ``BOTH``.
    """
    pattern = _rel_pattern(rel_types)
    if direction is Direction.OUT:
        match = f"(a:{BASE_LABEL} {{id: origin}})-[r:{pattern}]->(b:{BASE_LABEL})"
        endpoints = "a.id AS source, b.id AS target"
    elif direction is Direction.IN:
        match = f"(a:{BASE_LABEL} {{id: origin}})<-[r:{pattern}]-(b:{BASE_LABEL})"
        endpoints = "b.id AS source, a.id AS target"
    else:
        raise ValueError("expand() takes Direction.OUT or Direction.IN")
    return f"""
UNWIND $ids AS origin
MATCH {match}
WHERE coalesce(r.confidence, 1.0) >= $min_confidence
RETURN origin, {endpoints}, type(r) AS type, properties(r) AS props, properties(b) AS node
"""


def count_nodes() -> str:
    """Parameters: ``repository_id``."""
    return f"""
MATCH (n:{BASE_LABEL} {{repository_id: $repository_id}})
RETURN n.label AS key, count(*) AS total
"""


def count_edges() -> str:
    """Parameters: ``repository_id``."""
    return f"""
MATCH (:{BASE_LABEL} {{repository_id: $repository_id}})-[r]->()
RETURN type(r) AS key, count(*) AS total
"""


def all_statements() -> dict[str, str]:
    """Every statement shape the store can emit, keyed by a descriptive name.

    Used by the Cypher lint check; it must stay exhaustive, which
    ``tests/unit/test_cypher_queries.py`` enforces for the functions above.
    """
    statements: dict[str, str] = {
        f"schema[{index}]": statement for index, statement in enumerate(schema_statements())
    }
    for label in NodeLabel:
        statements[f"upsert_nodes[{label.value}]"] = upsert_nodes(label)
    for rel_type in RelType:
        statements[f"upsert_edges[{rel_type.value}]"] = upsert_edges(rel_type)
        statements[f"accumulate_edges[{rel_type.value}]"] = accumulate_edges(
            rel_type, additive=("count", "total_ms", "error_count"), maximum=("max_ms",)
        )
    statements["accumulate_edges[no counters]"] = accumulate_edges(
        RelType.OBSERVED_CALL, additive=(), maximum=()
    )
    statements["delete_repository_batch"] = delete_repository_batch()
    statements["delete_edges_batch"] = delete_edges_batch(RUNTIME_RELS)
    statements["get_nodes"] = get_nodes()
    flags = (False, True)
    for by_repository in flags:
        for by_labels in flags:
            for by_name in flags:
                for by_qualified_name in flags:
                    for by_path in flags:
                        combination = (by_repository, by_labels, by_name, by_qualified_name, by_path)
                        key = "find_nodes[" + "".join(str(int(flag)) for flag in combination) + "]"
                        statements[key] = find_nodes(
                            by_repository=by_repository,
                            by_labels=by_labels,
                            by_name=by_name,
                            by_qualified_name=by_qualified_name,
                            by_path=by_path,
                        )
    statements["find_nodes[where]"] = find_nodes(
        by_repository=True,
        by_labels=True,
        by_name=False,
        by_qualified_name=False,
        by_path=False,
        where_keys=("is_entry_point", "kind"),
    )
    for by_labels in flags:
        statements[f"search_nodes[{by_labels:d}]"] = search_nodes(by_labels=by_labels)
        statements[f"nodes_at[{by_labels:d}]"] = nodes_at(by_labels=by_labels)
    statements["list_edges[one]"] = list_edges([RelType.OBSERVED_CALL])
    statements["list_edges[all]"] = list_edges(list(RelType))
    for direction in (Direction.OUT, Direction.IN):
        statements[f"expand[{direction.value},one]"] = expand([RelType.CALLS], direction)
        statements[f"expand[{direction.value},all]"] = expand(list(RelType), direction)
    statements["count_nodes"] = count_nodes()
    statements["count_edges"] = count_edges()
    return statements


if __name__ == "__main__":  # pragma: no cover - `python -m graph_engine.cypher_queries | node lint.mjs`
    import json
    import sys

    json.dump(all_statements(), sys.stdout, indent=1)
