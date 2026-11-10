"""``archaeo`` command line: run the server, ingest repositories and ask questions without the UI."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from backend import __version__
from backend.settings import Settings, get_settings


def _container(settings: Settings) -> Any:
    from backend.container import Container  # noqa: PLC0415 - keeps `archaeo --help` instant

    return Container.build(settings)


def _progress(stage: str, fraction: float, message: str) -> None:
    print(f"\r[{fraction * 100:5.1f}%] {stage:<9} {message:<70.70}", end="", file=sys.stderr, flush=True)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn  # noqa: PLC0415

    uvicorn.run(
        "backend.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=get_settings().log_level.lower(),
        # One process: the embedded stores and the in-process job thread are not shareable.
        workers=1,
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from backend.celery_app import celery_app  # noqa: PLC0415

    celery_app.worker_main(["worker", f"--loglevel={args.loglevel}", f"--concurrency={args.concurrency}"])
    return 0


def _ingest(container: Any, request: Any) -> int:
    from ingestion.pipeline import IngestionError  # noqa: PLC0415

    try:
        result = container.pipeline.run(request, progress=_progress)
    except IngestionError as error:
        print(f"\ningestion failed: {error}", file=sys.stderr)
        return 1
    finally:
        container.close()
    print(file=sys.stderr)
    stats = result.stats
    print(f"repository  {result.repository_id}")
    print(
        f"parsed      {stats.get('files', 0)} files, {stats.get('functions', 0)} functions, "
        f"{stats.get('classes', 0)} classes, {stats.get('queries', 0)} SQL statements, "
        f"{stats.get('tables', 0)} tables"
    )
    print(f"graph       {stats.get('nodes_written', 0)} nodes, {stats.get('edges_written', 0)} relationships")
    print(f"duration    {result.duration_seconds}s")
    for warning in result.warnings:
        print(f"warning     {warning}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from ingestion.pipeline import IngestionRequest  # noqa: PLC0415

    settings = get_settings()
    if args.allow_local:
        settings = settings.model_copy(
            update={"allowed_local_roots": [*settings.allowed_local_roots, args.source]}
        )
    request = IngestionRequest(
        source=args.source,
        name=args.name,
        branch=args.branch,
        token=settings.github_token.get_secret_value() if settings.github_token else None,
        include=tuple(args.include or ()),
    )
    return _ingest(_container(settings), request)


def cmd_demo(args: argparse.Namespace) -> int:
    from backend.demo import (  # noqa: PLC0415
        DemoUnavailableError,
        build_demo_repository,
        demo_request,
        write_demo_snapshot,
    )

    if args.write_snapshot:
        try:
            written = write_demo_snapshot()
        except DemoUnavailableError as error:
            print(error, file=sys.stderr)
            return 1
        print(f"snapshot of {written.repository_id}: {written.nodes} nodes, {written.edges} relationships")
        return 0
    settings = get_settings()
    try:
        path = build_demo_repository(settings.demo_path, force=args.force)
    except DemoUnavailableError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"demo repository at {path}", file=sys.stderr)
    return _ingest(_container(settings), demo_request(settings))


def cmd_export(args: argparse.Namespace) -> int:
    from pathlib import Path  # noqa: PLC0415

    from graph_engine.snapshot import export_repository  # noqa: PLC0415

    container = _container(get_settings())
    try:
        written = export_repository(
            container.store, args.repository, Path(args.file), include_runtime=not args.no_runtime
        )
    except LookupError:
        print(f"unknown repository {args.repository!r}; list them with `archaeo repos`", file=sys.stderr)
        return 1
    finally:
        container.close()
    print(f"exported {written.repository_id}: {written.nodes} nodes, {written.edges} relationships")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from pathlib import Path  # noqa: PLC0415

    from graph_engine.snapshot import SnapshotError, import_snapshot  # noqa: PLC0415

    sources = Path(args.sources).resolve() if args.sources else None
    if sources is not None and not sources.is_dir():
        print(f"{sources} is not a directory", file=sys.stderr)
        return 1
    container = _container(get_settings())
    try:
        loaded = import_snapshot(container.store, Path(args.file), workspace_path=sources)
        if container.index is not None:
            from rag_engine.indexer import RepositoryIndexer  # noqa: PLC0415

            RepositoryIndexer(container.store, container.index).index_repository(
                loaded.repository_id, sources
            )
    except (SnapshotError, OSError) as error:
        print(f"import failed: {error}", file=sys.stderr)
        return 1
    finally:
        container.close()
    print(f"imported {loaded.repository_id}: {loaded.nodes} nodes, {loaded.edges} relationships")
    if sources is None:
        print("no --sources given: the graph is complete but source text cannot be shown", file=sys.stderr)
    return 0


def cmd_repos(_: argparse.Namespace) -> int:
    from graph_engine.schema import NodeLabel  # noqa: PLC0415

    container = _container(get_settings())
    try:
        nodes = container.store.find_nodes(None, labels=[NodeLabel.REPOSITORY], limit=500)
    finally:
        container.close()
    if not nodes:
        print("no repositories ingested yet - try `archaeo demo`")
        return 0
    for node in nodes:
        print(
            f"{node.repository_id:<44} {node.get('file_count', 0):>6} files  "
            f"{node.get('function_count', 0):>7} functions  {node.get('source')}"
        )
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from rag_engine.engine import UnknownRepositoryError  # noqa: PLC0415

    container = _container(get_settings())
    try:
        if args.json:
            print(json.dumps(container.rag.answer(args.repository, args.question), indent=2))
            return 0
        for event in container.rag.stream(args.repository, args.question):
            kind = event["type"]
            if kind == "context":
                print(f"[{event['mode']}] {len(event['evidence'])} pieces of evidence\n", file=sys.stderr)
            elif kind == "token":
                print(event["text"], end="", flush=True)
            elif kind == "citations":
                checked = event["summary"]
                print(
                    f"\n\ncitations: {checked['verified']} verified, {checked['outside_context']} outside "
                    f"the evidence, {checked['unresolved']} unresolved",
                    file=sys.stderr,
                )
            elif kind == "done":
                for warning in event["warnings"]:
                    print(f"warning: {warning}", file=sys.stderr)
    except UnknownRepositoryError:
        print(f"unknown repository {args.repository!r}; list them with `archaeo repos`", file=sys.stderr)
        return 1
    finally:
        container.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archaeo", description="Archaeo-Code: code archeology for legacy systems."
    )
    parser.add_argument("--version", action="version", version=f"archaeo-code {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    serve.set_defaults(handler=cmd_serve)

    worker = commands.add_parser("worker", help="run a Celery ingestion worker (ARCHAEO_JOB_BACKEND=celery)")
    worker.add_argument("--concurrency", type=int, default=2)
    worker.add_argument("--loglevel", default="info")
    worker.set_defaults(handler=cmd_worker)

    ingest = commands.add_parser("ingest", help="ingest a repository (git URL or local path)")
    ingest.add_argument("source")
    ingest.add_argument("--name")
    ingest.add_argument("--branch")
    ingest.add_argument(
        "--include", action="append", metavar="PREFIX", help="only this path prefix (repeatable)"
    )
    ingest.add_argument(
        "--allow-local",
        action="store_true",
        help="permit this local path without ARCHAEO_ALLOWED_LOCAL_ROOTS",
    )
    ingest.set_defaults(handler=cmd_ingest)

    demo = commands.add_parser("demo", help="build and ingest the bundled synthetic demo repository")
    demo.add_argument("--force", action="store_true", help="rebuild the demo repository from scratch")
    demo.add_argument(
        "--write-snapshot",
        action="store_true",
        help="regenerate examples/demo-monolith/graph-snapshot.jsonl instead of ingesting",
    )
    demo.set_defaults(handler=cmd_demo)

    export = commands.add_parser("export", help="write the graph of a repository to a snapshot file")
    export.add_argument("repository", help="repository id (see `archaeo repos`)")
    export.add_argument("file", help="target path; a .gz suffix compresses")
    export.add_argument("--no-runtime", action="store_true", help="leave out runtime-telemetry observations")
    export.set_defaults(handler=cmd_export)

    importer = commands.add_parser("import", help="load a snapshot file into the graph")
    importer.add_argument("file")
    importer.add_argument("--sources", metavar="DIR", help="directory holding the repository's files")
    importer.set_defaults(handler=cmd_import)

    repos = commands.add_parser("repos", help="list ingested repositories")
    repos.set_defaults(handler=cmd_repos)

    ask = commands.add_parser("ask", help="ask a question about an ingested repository")
    ask.add_argument("repository", help="repository id (see `archaeo repos`)")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true", help="print the full result, evidence included, as JSON")
    ask.set_defaults(handler=cmd_ask)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = args.handler
    result: int = handler(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
