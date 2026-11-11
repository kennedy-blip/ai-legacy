"""Maps source files to the extractor for their language."""

from __future__ import annotations

from functools import cache
from pathlib import PurePosixPath
from typing import Final

from parsers.base import Extractor
from parsers.ir import Language, ModuleDef

_EXTENSIONS: Final[dict[str, Language]] = {
    ".java": Language.JAVA,
    ".py": Language.PYTHON,
    ".pyw": Language.PYTHON,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT,
    ".cts": Language.TYPESCRIPT,
    ".c": Language.CPP,
    ".cc": Language.CPP,
    ".cpp": Language.CPP,
    ".cxx": Language.CPP,
    ".h": Language.CPP,
    ".hh": Language.CPP,
    ".hpp": Language.CPP,
    ".hxx": Language.CPP,
    ".sql": Language.SQL,
    ".ddl": Language.SQL,
    ".pgsql": Language.SQL,
    ".plsql": Language.SQL,
    ".pls": Language.SQL,
    ".pks": Language.SQL,
    ".pkb": Language.SQL,
    ".prc": Language.SQL,
    ".fnc": Language.SQL,
    ".trg": Language.SQL,
}

#: File extensions the platform can parse.
SUPPORTED_EXTENSIONS: Final = frozenset(_EXTENSIONS)


def language_for_path(path: str) -> Language | None:
    """Language of ``path`` by extension, or ``None`` when it is not supported."""
    name = PurePosixPath(path).name.lower()
    if name.endswith(".d.ts"):
        return None  # type declarations only: no behaviour to map
    suffix = PurePosixPath(name).suffix
    return _EXTENSIONS.get(suffix)


@cache
def extractor_for(language: Language) -> Extractor:
    """The (stateless, per-process) extractor for ``language``. Grammars load lazily."""
    if language is Language.JAVA:
        from parsers.java import JavaExtractor  # noqa: PLC0415

        return JavaExtractor()
    if language is Language.PYTHON:
        from parsers.python import PythonExtractor  # noqa: PLC0415

        return PythonExtractor()
    if language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
        from parsers.javascript import JavaScriptExtractor, TypeScriptExtractor  # noqa: PLC0415

        return TypeScriptExtractor() if language is Language.TYPESCRIPT else JavaScriptExtractor()
    if language is Language.CPP:
        from parsers.cpp import CppExtractor  # noqa: PLC0415

        return CppExtractor()
    if language is Language.SQL:
        from parsers.sql import SqlExtractor  # noqa: PLC0415

        return SqlExtractor()
    raise ValueError(f"no extractor for {language}")


def parse_file(path: str, data: bytes) -> ModuleDef:
    """Parse one source file into its language-neutral representation."""
    language = language_for_path(path)
    if language is None:
        raise ValueError(f"unsupported file type: {path}")
    return extractor_for(language).parse(path, data)
