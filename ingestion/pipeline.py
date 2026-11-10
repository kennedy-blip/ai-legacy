"""End-to-end ingestion: checkout -> parse -> resolve -> git archeology -> graph -> vector index."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from graph_engine.models import GraphNode
from graph_engine.schema import RUNTIME_RELS, NodeLabel, make_repository_id
from graph_engine.store import GraphStore
from ingestion import gitops
from ingestion.discovery import discover, parse_files
from ingestion.github import fetch_pull_requests
from ingestion.graph_builder import BuiltGraph, GraphBuilder, _clip, repository_node_id
from ingestion.history import apply_history
from ingestion.insights import compute_insights
from ingestion.resolver import SymbolTable

logger = logging.getLogger(__name__)

#: ``(stage, fraction complete in [0, 1], human-readable message)``
ProgressCallback = Callable[[str, float, str], None]

_WRITE_BATCH = 5000


class IngestionError(RuntimeError):
    """The repository could not be ingested; the message is safe to show to the user."""


class Indexer(Protocol):
    """Builds the semantic (vector) index for a repository after its graph is written."""

    def index_repository(
        self, repository_id: str, source_root: Path | None, progress: Callable[[int, int], None] | None = None
    ) -> int: ...


@dataclass(slots=True)
class IngestionRequest:
    #: ``https://`` / ``ssh://`` URL, or a path under one of the allowed local roots.
    source: str
    name: str | None = None
    branch: str | None = None
    #: Access token for a private remote. Used for the clone only; never stored.
    token: str | None = field(default=None, repr=False)
    #: Restrict ingestion to these path prefixes.
    include: tuple[str, ...] = ()
    #: Marks the bundled synthetic demonstration repository so the UI can label it.
    demo: bool = False


@dataclass(slots=True)
class IngestionOptions:
    workspace_dir: Path
    allowed_local_roots: tuple[Path, ...] = ()
    #: Hosts repositories may be cloned from; empty allows any host.
    allowed_git_hosts: tuple[str, ...] = ()
    max_files: int = 20_000
    max_file_bytes: int = 1_500_000
    max_commits: int = 20_000
    blame: bool = True
    blame_max_files: int = 4_000
    blame_budget_seconds: float = 300.0
    parse_workers: int | None = None
    github_enrichment: bool = True
    github_token: str | None = field(default=None, repr=False)
    github_max_pull_requests: int = 60


@dataclass(slots=True)
class IngestionResult:
    repository_id: str
    name: str
    stats: dict[str, Any]
    warnings: list[str]
    duration_seconds: float


def default_name(source: str) -> str:
    tail = source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail.removesuffix(".git") or "repository"


def remote_host(source: str) -> str:
    """Lower-cased host of a validated remote URL (``https://``, ``ssh://`` or ``git@host:path``)."""
    authority = source.split("://", 1)[1].split("/", 1)[0] if "://" in source else source.split(":", 1)[0]
    return authority.rsplit("@", 1)[-1].split(":", 1)[0].lower()


def is_remote(source: str) -> bool:
    return source.startswith(("https://", "http://", "ssh://", "git@", "git://", "file://"))


class IngestionPipeline:
    def __init__(
        self, store: GraphStore, options: IngestionOptions, *, indexer: Indexer | None = None
    ) -> None:
        self.store = store
        self.options = options
        self.indexer = indexer

    # ---------------------------------------------------------------- identity

    def identify(self, request: IngestionRequest) -> tuple[str, str, str]:
        """``(repository id, display name, canonical source)`` for a request, without side effects."""
        if is_remote(request.source) or "://" in request.source:
            source = gitops.validate_remote_url(request.source)
            if source.startswith("https://") and "@" in source.split("/", 3)[2]:
                raise IngestionError(
                    "credentials must not be embedded in the URL; pass an access token instead"
                )
            host = remote_host(source)
            allowed = {entry.lower() for entry in self.options.allowed_git_hosts}
            if allowed and host not in allowed:
                raise IngestionError(
                    f"cloning from {host or 'this host'} is not permitted (ARCHAEO_ALLOWED_GIT_HOSTS)"
                )
        else:
            source = str(self._local_root(request.source))
        name = (request.name or default_name(source)).strip()[:120] or "repository"
        return make_repository_id(name, source), name, source

    def _local_root(self, raw: str) -> Path:
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise IngestionError("local path does not exist") from error
        if not path.is_dir():
            raise IngestionError("local path is not a directory")
        roots = [root.expanduser().resolve() for root in self.options.allowed_local_roots]
        if not any(path == root or root in path.parents for root in roots):
            raise IngestionError(
                "local path is outside the directories this server may read (ARCHAEO_ALLOWED_LOCAL_ROOTS)"
            )
        return path

    # --------------------------------------------------------------------- run

    def run(self, request: IngestionRequest, *, progress: ProgressCallback | None = None) -> IngestionResult:
        started = time.monotonic()
        warnings: list[str] = []

        def report(stage: str, fraction: float, message: str) -> None:
            logger.info("ingest[%s] %3.0f%% %s", stage, fraction * 100, message)
            if progress is not None:
                progress(stage, min(max(fraction, 0.0), 1.0), message)

        try:
            repository_id, name, source = self.identify(request)
        except ValueError as error:
            raise IngestionError(str(error)) from error

        report("checkout", 0.02, "Preparing source")
        root = self._checkout(request, repository_id, source)
        is_git = gitops.is_git_repository(root)
        head = gitops.head_commit(root) if is_git else None
        branch = gitops.current_branch(root) if is_git else None

        report("discover", 0.10, "Discovering source files")
        discovery = discover(
            root,
            max_file_bytes=self.options.max_file_bytes,
            max_files=self.options.max_files,
            include=request.include,
        )
        if not discovery.files:
            raise IngestionError(
                "no supported source files found (Java, Python, JavaScript/TypeScript, C/C++, SQL)"
            )
        for reason, count in sorted(discovery.skipped.items()):
            if reason != "empty":  # empty package markers are not worth a warning
                warnings.append(f"{count} file(s) skipped: {reason}")

        total = len(discovery.files)
        report("parse", 0.15, f"Parsing {total} files")
        parsed = parse_files(
            root,
            discovery.files,
            workers=self.options.parse_workers,
            progress=lambda done, count: report(
                "parse", 0.15 + 0.30 * done / max(count, 1), f"Parsed {done}/{count}"
            ),
        )
        failures = [item for item in parsed if item.module is None]
        if failures:
            reasons = sorted({item.error or "unknown" for item in failures})[:3]
            warnings.append(f"{len(failures)} file(s) could not be parsed ({'; '.join(reasons)})")

        report("resolve", 0.47, "Resolving calls, inheritance and data access")
        modules = [item.module for item in parsed if item.module is not None]
        symbols = SymbolTable(repository_id, modules)
        resolution = symbols.resolve()
        graph = GraphBuilder(repository_id, symbols, resolution).build(parsed)
        stats: dict[str, Any] = dict(graph.stats)
        stats["files_skipped"] = dict(discovery.skipped)

        if is_git:
            report("history", 0.55, "Reading git history")
            stats["history"] = self._history(root, graph, repository_id, symbols, source, warnings, report)
        else:
            warnings.append("source is not a git repository: no history, authorship or blame available")

        insights = compute_insights(graph)
        repository = GraphNode(
            repository_node_id(repository_id),
            NodeLabel.REPOSITORY,
            repository_id,
            {
                "name": name,
                "source": source,
                "source_kind": "remote" if is_remote(source) else "local",
                "branch": branch,
                "requested_branch": request.branch,
                "include": list(request.include),
                "is_demo": request.demo or None,
                "head_commit": head,
                "workspace_path": str(root),
                "status": "ready",
                "ingested_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "stats_json": json.dumps(stats, sort_keys=True),
                "insights_json": json.dumps(insights, sort_keys=True),
                "warnings": warnings[:20],
                "file_count": stats["files"],
                "function_count": stats["functions"],
                "class_count": stats["classes"],
                "table_count": stats["tables"],
                "line_count": stats["lines"],
                "languages": sorted(stats["languages"], key=lambda key: -stats["languages"][key]),
            },
        )
        graph.add_node(repository)

        report("store", 0.78, f"Writing {len(graph.nodes)} nodes and {len(graph.edges)} relationships")
        self._write(repository_id, graph, stats, warnings)

        if self.indexer is not None:
            report("index", 0.86, "Building the semantic index")
            try:
                stats["vectors"] = self.indexer.index_repository(
                    repository_id,
                    root,
                    lambda done, count: report(
                        "index", 0.86 + 0.13 * done / max(count, 1), f"Embedded {done}/{count} scopes"
                    ),
                )
            except Exception as error:  # the graph is usable without vectors; say so instead of failing
                logger.exception("semantic indexing failed")
                warnings.append(f"semantic index not built: {error.__class__.__name__}: {str(error)[:200]}")
                stats["vectors"] = 0

        duration = round(time.monotonic() - started, 2)
        stats["duration_seconds"] = duration
        repository.properties.update(
            {
                "stats_json": json.dumps(stats, sort_keys=True),
                "warnings": warnings[:20],
                "duration_seconds": duration,
            }
        )
        self.store.upsert_nodes([repository])
        report("done", 1.0, f"Ingested {stats['files']} files, {stats['functions']} functions in {duration}s")
        return IngestionResult(repository_id, name, stats, warnings, duration)

    # ---------------------------------------------------------------- stages

    def _checkout(self, request: IngestionRequest, repository_id: str, source: str) -> Path:
        if not is_remote(source):
            return Path(source)
        if not gitops.git_available():
            raise IngestionError("git is not installed on the server")
        workspace = self.options.workspace_dir
        workspace.mkdir(parents=True, exist_ok=True)
        final = workspace / repository_id
        staging = workspace / f".{repository_id}.{os.getpid()}.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            gitops.clone(source, staging, branch=request.branch, token=request.token)
        except (gitops.GitError, ValueError) as error:
            shutil.rmtree(staging, ignore_errors=True)
            raise IngestionError(f"could not clone repository: {error}") from error
        # Swap the fresh clone in; a reader holding the old tree keeps a consistent view until then.
        retired = workspace / f".{repository_id}.{os.getpid()}.old"
        shutil.rmtree(retired, ignore_errors=True)
        if final.exists():
            final.rename(retired)
        staging.rename(final)
        shutil.rmtree(retired, ignore_errors=True)
        return final

    def _history(
        self,
        root: Path,
        graph: BuiltGraph,
        repository_id: str,
        symbols: SymbolTable,
        source: str,
        warnings: list[str],
        report: ProgressCallback,
    ) -> dict[str, Any]:
        options = self.options
        try:
            commits = gitops.read_history(root, max_commits=options.max_commits)
        except gitops.GitError as error:
            warnings.append(f"git history unavailable: {error}")
            return {"commits": 0}
        if len(commits) >= options.max_commits:
            warnings.append(f"history truncated to the most recent {options.max_commits} commits")

        blame: dict[str, dict[int, str]] = {}
        if options.blame and commits:
            paths = sorted(symbols.modules)[: options.blame_max_files]
            if len(symbols.modules) > len(paths):
                warnings.append(f"blame limited to {len(paths)} of {len(symbols.modules)} files")
            deadline = time.monotonic() + options.blame_budget_seconds
            report("history", 0.58, f"Attributing {len(paths)} files with git blame")
            blame = self._blame(root, paths, deadline, report)
            if len(blame) < len(paths):
                warnings.append(
                    f"blame completed for {len(blame)} of {len(paths)} files (time budget or untracked)"
                )

        summary = apply_history(graph, repository_id, symbols, commits, blame)
        if options.github_enrichment:
            summary["pull_requests"] = self._enrich(root, graph, source)
        return summary

    def _blame(
        self, root: Path, paths: Sequence[str], deadline: float, report: ProgressCallback
    ) -> dict[str, dict[int, str]]:
        results: dict[str, dict[int, str]] = {}

        def one(path: str) -> tuple[str, dict[int, str] | None]:
            if time.monotonic() > deadline:
                return path, None
            return path, gitops.blame_lines(root, path)

        with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2) * 2)) as pool:
            futures = [pool.submit(one, path) for path in paths]
            for done, future in enumerate(as_completed(futures), start=1):
                path, lines = future.result()
                if lines:
                    results[path] = lines
                if done % 50 == 0 or done == len(futures):
                    report(
                        "history", 0.58 + 0.16 * done / len(futures), f"Blamed {done}/{len(futures)} files"
                    )
        return results

    def _enrich(self, root: Path, graph: BuiltGraph, source: str) -> int:
        slug = gitops.github_slug(source if is_remote(source) else gitops.remote_url(root))
        if slug is None:
            return 0
        commits = [
            node for node in graph.nodes.values() if node.label is NodeLabel.COMMIT and node.get("pr_number")
        ]
        commits.sort(key=lambda node: str(node.get("authored_at")), reverse=True)
        numbers = [int(node.get("pr_number")) for node in commits]
        try:
            pulls = fetch_pull_requests(
                slug[0],
                slug[1],
                numbers,
                token=self.options.github_token,
                limit=self.options.github_max_pull_requests,
            )
        except Exception:  # enrichment is optional by design
            logger.info("GitHub enrichment failed", exc_info=True)
            return 0
        for node in commits:
            pull = pulls.get(int(node.get("pr_number")))
            if pull is not None:
                node.properties.update(
                    {
                        "pr_title": _clip(pull.title, 300),
                        "pr_body": _clip(pull.body, 3000),
                        "pr_url": pull.url,
                        "pr_author": pull.author,
                        "pr_labels": list(pull.labels)[:10],
                    }
                )
        return len(pulls)

    def _write(
        self, repository_id: str, graph: BuiltGraph, stats: dict[str, Any], warnings: list[str]
    ) -> None:
        """Replace the repository's static graph, carrying runtime observations across."""
        store = self.store
        store.ensure_schema()
        runtime = store.list_edges(repository_id, RUNTIME_RELS)
        store.delete_repository(repository_id, keep_labels=[NodeLabel.INGESTION_JOB])
        nodes = list(graph.nodes.values())
        for start in range(0, len(nodes), _WRITE_BATCH):
            store.upsert_nodes(nodes[start : start + _WRITE_BATCH])
        edges = list(graph.edges.values())
        written = 0
        for start in range(0, len(edges), _WRITE_BATCH):
            written += store.upsert_edges(edges[start : start + _WRITE_BATCH])
        stats["nodes_written"] = len(nodes)
        stats["edges_written"] = written
        if runtime:
            kept = store.upsert_edges(runtime)
            stats["runtime_edges_preserved"] = kept
            if kept < len(runtime):
                warnings.append(
                    f"{len(runtime) - kept} runtime observation(s) dropped: their functions no longer exist"
                )
