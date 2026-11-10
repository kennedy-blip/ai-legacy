"""Source discovery and (parallel) parsing of a checked-out repository."""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ingestion.gitops import tracked_files
from parsers.ir import ModuleDef
from parsers.registry import language_for_path, parse_file

#: Directory names that never contain first-party source worth mapping.
IGNORED_DIRECTORIES: Final = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor", "third_party", "third-party",
        "3rdparty", "external", "dist", "build", "out", "target", "bin", "obj", ".next", ".nuxt", ".venv",
        "venv", "env", "__pycache__", "site-packages", ".tox", ".mypy_cache", ".pytest_cache", "coverage",
        ".idea", ".vscode", ".gradle", "Pods", "DerivedData", ".terraform", "generated", "gen",
    }
)  # fmt: skip

_GENERATED_NAME_RE: Final = re.compile(
    r"(?:\.min\.|[.-]bundle\.|\.generated\.|_pb2(?:_grpc)?\.py$|\.pb\.(?:cc|h)$|\.designer\.|"
    r"^jquery[.-]|^bootstrap[.-]|^angular[.-]|\.chunk\.js$|\.map$)",
    re.IGNORECASE,
)
_TEST_PATH_RE: Final = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs|testing|test-utils|fixtures|mocks?|__mocks__|e2e|it)(?:/|$)"
    r"|(?:^|/)src/test/"
    r"|(?:^|/)(?:test_[^/]+|[^/]+_test|conftest)\.py$"
    r"|(?:^|/)[^/]+(?:Test|Tests|TestCase|IT|Spec)\.java$"
    r"|(?:^|/)[^/]+[._-](?:test|spec)\.[cm]?[jt]sx?$"
    r"|(?:^|/)[^/]+_(?:test|unittest)\.(?:c|cc|cpp|cxx)$",
)
#: Lines longer than this on average indicate minified or generated code.
_MAX_AVERAGE_LINE: Final = 400

ProgressFn = Callable[[int, int], None]


def is_test_path(path: str) -> bool:
    """Heuristic: does ``path`` hold test code rather than product code?"""
    return bool(_TEST_PATH_RE.search(path))


@dataclass(slots=True)
class SourceFile:
    #: Repository-relative POSIX path.
    path: str
    size: int


@dataclass(slots=True)
class Discovery:
    files: list[SourceFile] = field(default_factory=list)
    #: Reason -> number of files skipped for it.
    skipped: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class ParsedFile:
    path: str
    size: int
    sha1: str = ""
    module: ModuleDef | None = None
    error: str | None = None


def _walk(root: Path) -> Iterable[str]:
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(
            name for name in names if name not in IGNORED_DIRECTORIES and not name.startswith(".")
        )
        relative = Path(directory).relative_to(root).as_posix()
        for name in sorted(files):
            yield name if relative == "." else f"{relative}/{name}"


def discover(
    root: str | Path,
    *,
    max_file_bytes: int = 1_500_000,
    max_files: int = 20_000,
    include: Sequence[str] = (),
) -> Discovery:
    """List the parseable first-party source files under ``root``.

    Git-tracked files are used when ``root`` is a work tree (so ignored build
    output is skipped for free); otherwise the directory is walked. ``include``
    optionally restricts discovery to paths under the given prefixes.
    """
    base = Path(root).resolve()
    result = Discovery()
    candidates = tracked_files(base)
    paths: Iterable[str] = sorted(candidates) if candidates is not None else _walk(base)
    prefixes = tuple(prefix.strip("/") + "/" for prefix in include if prefix.strip("/"))
    for path in paths:
        if language_for_path(path) is None:
            continue
        parts = path.split("/")
        if any(part in IGNORED_DIRECTORIES for part in parts[:-1]):
            result.skipped["vendored or build output"] += 1
            continue
        if prefixes and not path.startswith(prefixes):
            result.skipped["outside include filter"] += 1
            continue
        if _GENERATED_NAME_RE.search(parts[-1]):
            result.skipped["generated or minified"] += 1
            continue
        absolute = base / path
        try:
            if absolute.is_symlink() or not absolute.is_file():
                result.skipped["not a regular file"] += 1
                continue
            if base not in absolute.resolve().parents:
                result.skipped["outside repository"] += 1
                continue
            size = absolute.stat().st_size
        except OSError:
            result.skipped["unreadable"] += 1
            continue
        if size == 0:
            result.skipped["empty"] += 1
            continue
        if size > max_file_bytes:
            result.skipped["too large"] += 1
            continue
        if len(result.files) >= max_files:
            result.skipped["over file limit"] += 1
            continue
        result.files.append(SourceFile(path, size))
    return result


def _parse_one(root: str, path: str, size: int) -> ParsedFile:
    parsed = ParsedFile(path, size)
    try:
        data = (Path(root) / path).read_bytes()
    except OSError as error:
        parsed.error = f"unreadable: {error.__class__.__name__}"
        return parsed
    parsed.sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
    if b"\0" in data[:8192]:
        parsed.error = "binary content"
        return parsed
    newlines = data.count(b"\n") + 1
    if len(data) / newlines > _MAX_AVERAGE_LINE:
        parsed.error = "minified or generated"
        return parsed
    try:
        parsed.module = parse_file(path, data)
    except RecursionError:
        parsed.error = "syntax tree too deep"
    except Exception as error:  # one unparseable file must not abort the ingestion
        parsed.error = f"parser error: {error.__class__.__name__}: {str(error)[:120]}"
    return parsed


def _parse_batch(root: str, batch: list[tuple[str, int]]) -> list[ParsedFile]:
    return [_parse_one(root, path, size) for path, size in batch]


def parse_files(
    root: str | Path,
    files: Sequence[SourceFile],
    *,
    workers: int | None = None,
    progress: ProgressFn | None = None,
) -> list[ParsedFile]:
    """Parse ``files`` into the language-neutral IR, in parallel when it pays off.

    Parsing is CPU-bound, so worker *processes* are used. Inside a daemonic
    process (a Celery prefork worker cannot have children) or for small inputs
    the work is done inline.
    """
    base = str(Path(root).resolve())
    total = len(files)
    results: list[ParsedFile] = []
    if workers is None:
        workers = min(os.cpu_count() or 1, 8)
    batches = [[(f.path, f.size) for f in files[start : start + 40]] for start in range(0, total, 40)]

    def report() -> None:
        if progress is not None:
            progress(len(results), total)

    use_pool = workers > 1 and total >= 80 and not _is_daemonic()
    if use_pool:
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for parsed in pool.map(_parse_batch, [base] * len(batches), batches):
                    results.extend(parsed)
                    report()
            return results
        except (OSError, RuntimeError):  # no fork allowed / pool broke: fall back to inline parsing
            results.clear()
    for batch in batches:
        results.extend(_parse_batch(base, batch))
        report()
    return results


def _is_daemonic() -> bool:
    import multiprocessing  # noqa: PLC0415

    return bool(multiprocessing.current_process().daemon)
