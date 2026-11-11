"""Git archeology: commits, authorship and blame mapped onto the code graph."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from graph_engine.models import GraphEdge, GraphNode
from graph_engine.schema import NodeLabel, RelType, make_id
from ingestion.gitops import CommitInfo, iter_current_paths
from ingestion.graph_builder import BuiltGraph, _clip
from ingestion.resolver import SymbolTable

_MAX_BODY: Final = 3000
_MAX_BLAME_EDGES: Final = 25
_MAX_FILE_EDGES: Final = 400


def to_utc(iso: str) -> str:
    """Normalise an ISO-8601 timestamp to UTC (``...Z``) so that strings sort chronologically."""
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def developer_key(name: str, email: str) -> str:
    return (email or name).strip().lower()


@dataclass(slots=True)
class _Developer:
    name: str
    email: str
    commits: int = 0
    first: str = ""
    last: str = ""
    lines_owned: int = 0


@dataclass(slots=True)
class _Touch:
    commits: dict[str, int] = field(default_factory=dict)  # hash -> surviving lines

    def add(self, commit: str) -> None:
        self.commits[commit] = self.commits.get(commit, 0) + 1


def apply_history(
    graph: BuiltGraph,
    repository_id: str,
    symbols: SymbolTable,
    commits: Sequence[CommitInfo],
    blame: Mapping[str, Mapping[int, str]],
) -> dict[str, Any]:
    """Add Commit / Developer nodes and ``TOUCHED_BY`` / ``AUTHORED_BY`` edges to ``graph``.

    * File level - every commit that changed a file (renames followed) from ``git log``.
    * Scope level - for each class and function, the commits that last changed its
      *surviving* lines, from ``git blame``. This answers "who wrote what is there
      now, and when"; lines that were since rewritten are attributed to the rewrite.

    Returns summary statistics.
    """
    by_hash = {commit.hash: commit for commit in commits}
    when = {commit.hash: to_utc(commit.authored_at) for commit in commits}
    developers: dict[str, _Developer] = {}
    for commit in commits:  # newest first: the most recent spelling of a name wins
        key = developer_key(commit.author_name, commit.author_email)
        developer = developers.get(key)
        if developer is None:
            developer = developers[key] = _Developer(commit.author_name, commit.author_email)
        developer.commits += 1
        moment = when[commit.hash]
        developer.first = min(developer.first or moment, moment)
        developer.last = max(developer.last, moment)

    used: dict[str, None] = {}
    edges: list[GraphEdge] = []

    def author_of(commit_hash: str) -> str:
        return by_hash[commit_hash].author_name

    def commit_id(commit_hash: str) -> str:
        return make_id(NodeLabel.COMMIT, repository_id, commit_hash)

    # ------------------------------------------------------------ file level
    file_ids = {
        node.path: node.id for node in graph.nodes.values() if node.label is NodeLabel.FILE and node.path
    }
    file_changes: dict[str, list[tuple[CommitInfo, int, int, str | None]]] = defaultdict(list)
    for commit, change, today in iter_current_paths(commits):
        if today in file_ids:
            renamed_from = change.old_path if change.old_path and change.old_path != today else None
            file_changes[today].append((commit, change.insertions, change.deletions, renamed_from))
    for path, changes in file_changes.items():
        node = graph.nodes[file_ids[path]]
        authors = Counter(change[0].author_name for change in changes)
        moments = [when[change[0].hash] for change in changes]
        newest = changes[0][0]
        node.properties.update(
            {
                "commit_count": len(changes),
                "author_count": len(authors),
                "authors": [f"{name} ({count})" for name, count in authors.most_common(5)],
                "first_commit_at": min(moments),
                "last_commit_at": max(moments),
                "last_author": newest.author_name,
                "churn": sum(change[1] + change[2] for change in changes),
            }
        )
        for commit, insertions, deletions, renamed_from in changes[:_MAX_FILE_EDGES]:
            used[commit.hash] = None
            edges.append(
                GraphEdge(
                    node.id,
                    commit_id(commit.hash),
                    RelType.TOUCHED_BY,
                    repository_id,
                    {
                        "insertions": insertions,
                        "deletions": deletions,
                        "renamed_from": renamed_from,
                        "scope": "file",
                    },
                )
            )

    # ----------------------------------------------------------- scope level
    scopes_blamed = 0
    spans: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for function in symbols.functions:
        definition = function.definition
        spans[function.module.path].append((function.id, definition.start_line, definition.end_line))
    for cls in symbols.classes:
        spans[cls.module.path].append((cls.id, cls.definition.start_line, cls.definition.end_line))
    for path, lines in blame.items():
        known = {line: sha for line, sha in lines.items() if sha in by_hash}
        if not known:
            continue
        for sha in known.values():
            developers[developer_key(author_of(sha), by_hash[sha].author_email)].lines_owned += 1
        for node_id, start, end in spans.get(path, ()):
            touch = _Touch()
            for line in range(start, end + 1):
                line_sha = known.get(line)
                if line_sha is not None:
                    touch.add(line_sha)
            if not touch.commits:
                continue
            scopes_blamed += 1
            ranked = sorted(touch.commits.items(), key=lambda item: (-item[1], item[0]))
            newest_hash = max(touch.commits, key=lambda h: (when[h], h))
            oldest_hash = min(touch.commits, key=lambda h: (when[h], h))
            owners: Counter[str] = Counter()
            for sha, count in touch.commits.items():
                owners[author_of(sha)] += count
            total = sum(touch.commits.values())
            graph.nodes[node_id].properties.update(
                {
                    "last_modified_at": when[newest_hash],
                    "last_author": author_of(newest_hash),
                    "last_commit": newest_hash[:8],
                    "oldest_line_at": when[oldest_hash],
                    "oldest_commit": oldest_hash[:8],
                    "author_count": len(owners),
                    "authors": [
                        f"{name} ({round(100 * count / total)}%)" for name, count in owners.most_common(5)
                    ],
                    "primary_author": owners.most_common(1)[0][0],
                    "blamed_lines": total,
                    "blame_commit_count": len(touch.commits),
                }
            )
            for sha, count in ranked[:_MAX_BLAME_EDGES]:
                used[sha] = None
                edges.append(
                    GraphEdge(
                        node_id,
                        commit_id(sha),
                        RelType.TOUCHED_BY,
                        repository_id,
                        {"lines": count, "share": round(count / total, 3), "scope": "blame"},
                    )
                )

    # -------------------------------------------------------- commit / developer nodes
    for commit_hash in used:
        commit = by_hash[commit_hash]
        node_id = commit_id(commit_hash)
        graph.add_node(
            GraphNode(
                node_id,
                NodeLabel.COMMIT,
                repository_id,
                {
                    "name": commit_hash[:8],
                    "hash": commit_hash,
                    "message": _clip(commit.subject, 300) or "(no message)",
                    "body": _clip(commit.body, _MAX_BODY),
                    "authored_at": when[commit_hash],
                    "author_name": commit.author_name,
                    "author_email": commit.author_email,
                    "is_merge": commit.is_merge,
                    "pr_number": commit.pr_number,
                    "tickets": commit.tickets[:10],
                    "files_changed": len(commit.files),
                    "insertions": sum(change.insertions for change in commit.files),
                    "deletions": sum(change.deletions for change in commit.files),
                },
            )
        )
        developer_id = make_id(
            NodeLabel.DEVELOPER, repository_id, developer_key(commit.author_name, commit.author_email)
        )
        edges.append(GraphEdge(node_id, developer_id, RelType.AUTHORED_BY, repository_id, {}))
    for key, developer in developers.items():
        graph.add_node(
            GraphNode(
                make_id(NodeLabel.DEVELOPER, repository_id, key),
                NodeLabel.DEVELOPER,
                repository_id,
                {
                    "name": developer.name,
                    "email": developer.email,
                    "commit_count": developer.commits,
                    "first_commit_at": developer.first,
                    "last_commit_at": developer.last,
                    "lines_owned": developer.lines_owned,
                },
            )
        )
    for edge in edges:
        graph.add_edge(edge)

    moments = sorted(when.values())
    return {
        "commits": len(commits),
        "commits_linked": len(used),
        "developers": len(developers),
        "first_commit_at": moments[0] if moments else None,
        "last_commit_at": moments[-1] if moments else None,
        "files_with_history": len(file_changes),
        "files_blamed": len(blame),
        "scopes_blamed": scopes_blamed,
    }
