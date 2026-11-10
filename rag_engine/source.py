"""Reads source text of an ingested repository from its checkout."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_MAX_BYTES = 3_000_000


class SourceReader:
    """Line-oriented access to the files of one repository checkout.

    Paths are repository-relative. Anything resolving outside the checkout
    (``..``, absolute paths, symlinks pointing elsewhere) is refused.
    """

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root).resolve() if root else None
        self._lines = lru_cache(maxsize=256)(self._load)

    @property
    def available(self) -> bool:
        return self.root is not None and self.root.is_dir()

    def _load(self, path: str) -> tuple[str, ...] | None:
        if self.root is None or not path or path.startswith(("/", "\\")):
            return None
        try:
            target = (self.root / path).resolve()
            if self.root not in target.parents or not target.is_file() or target.stat().st_size > _MAX_BYTES:
                return None
            data = target.read_bytes()
        except OSError:
            return None
        if b"\0" in data[:8192]:
            return None
        return tuple(data.decode("utf-8", "replace").splitlines())

    def lines(self, path: str) -> tuple[str, ...] | None:
        """All lines of ``path``, or ``None`` when the file cannot be served."""
        return self._lines(path)

    def snippet(
        self, path: str, start: int, end: int, *, max_lines: int | None = None
    ) -> list[tuple[int, str]]:
        """``(line number, text)`` pairs for lines ``start..end`` (1-based, inclusive)."""
        lines = self.lines(path)
        if lines is None:
            return []
        first = max(start, 1)
        last = min(end, len(lines))
        if max_lines is not None:
            last = min(last, first + max_lines - 1)
        return [(number, lines[number - 1]) for number in range(first, last + 1)]

    def text(self, path: str, start: int, end: int, *, max_lines: int | None = None) -> str:
        return "\n".join(line for _, line in self.snippet(path, start, end, max_lines=max_lines))


def numbered(snippet: list[tuple[int, str]]) -> str:
    """Render a snippet with a line-number gutter, the form shown to the language model."""
    if not snippet:
        return ""
    width = len(str(snippet[-1][0]))
    return "\n".join(f"{number:>{width}} | {text}" for number, text in snippet)
