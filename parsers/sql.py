"""SQL file extractor: tables, views, stored routines, triggers and migration statements.

``.sql`` files are split into statements by a dialect-tolerant splitter that
understands dollar-quoted bodies (PostgreSQL), ``DELIMITER`` directives (MySQL),
``GO`` batches (SQL Server), ``/`` terminators and package bodies (Oracle) and
``COPY ... FROM stdin`` data blocks (pg_dump). Each statement becomes a semantic
scope:

* ``CREATE TABLE`` / ``CREATE VIEW``  -> :class:`~parsers.ir.TableDef`. Column
  definitions are read from the Tree-sitter SQL parse of the statement, with a
  lexical fallback for dialect syntax the grammar rejects.
* ``CREATE FUNCTION`` / ``PROCEDURE`` / package members -> a ``procedure`` scope.
* ``CREATE TRIGGER`` -> a ``trigger`` scope plus a :class:`~parsers.ir.TriggerDef`.
* anything else (seed data, migrations) -> statements of the file's script scope.

Statements inside routine bodies are analysed one by one with
:func:`parsers.sql_analysis.analyze_sql`, so every query keeps its own line span.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import ClassVar, Final

import tree_sitter_sql
from tree_sitter import Language as TsLanguage
from tree_sitter import Node, Parser

from parsers.base import clean_doc_comment, collapse_ws, make_sql_statement
from parsers.ir import (
    MODULE_SCOPE,
    CallSite,
    ColumnDef,
    FunctionDef,
    FunctionKind,
    Language,
    ModuleDef,
    Param,
    SqlStatement,
    TableDef,
    TriggerDef,
)
from parsers.sql_analysis import analyze_sql, normalize_table_name

_GRAMMAR: Final = TsLanguage(tree_sitter_sql.language())

_LEX_RE: Final = re.compile(
    r"""
      (?P<nl>\n)
    | (?P<ws>[ \t\r\f\v]+)
    | (?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))
    | (?P<dollar>\$(?P<tag>[A-Za-z_]\w*|)\$.*?(?:\$(?P=tag)\$|\Z))
    | (?P<string>[NnEeBbXx]?'(?:[^'\\]|''|\\.)*(?:'|\Z)|"(?:[^"]|"")*(?:"|\Z)|`[^`]*(?:`|\Z))
    | (?P<word>[A-Za-z_@\#][\w$\#@]*)
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)
_DIRECTIVE_RE: Final = re.compile(
    r"[ \t]*(?:(?P<go>GO)(?:[ \t]+\d+)?|(?P<slash>/)|DELIMITER[ \t]+(?P<delim>\S+))[ \t]*\r?(?=\n|\Z)",
    re.IGNORECASE,
)
_GO_LINE_RE: Final = re.compile(r"^[ \t]*GO[ \t]*\r?$", re.IGNORECASE | re.MULTILINE)
_SLASH_LINE_RE: Final = re.compile(r"^[ \t]*/[ \t]*\r?$", re.MULTILINE)
_NEXT_WORD_RE: Final = re.compile(r"\s*([A-Za-z_@]\w*|;|\$|')?")
_COPY_STDIN_RE: Final = re.compile(r"^\s*COPY\b.*\bFROM\s+stdin\b", re.IGNORECASE | re.DOTALL)
_COPY_END_RE: Final = re.compile(r"^\\\.[ \t]*\r?$", re.MULTILINE)

_PROCEDURAL_OBJECTS: Final = frozenset(
    {"FUNCTION", "PROCEDURE", "PROC", "TRIGGER", "PACKAGE", "EVENT", "TYPE"}
)
_LOOP_CLOSERS: Final = frozenset({"IF", "LOOP", "WHILE", "REPEAT", "FOR"})
_NOT_BLOCK_AFTER_BEGIN: Final = frozenset({"TRAN", "TRANSACTION", "WORK", "DISTRIBUTED", ";"})

_FLAGS: Final = re.IGNORECASE | re.DOTALL
_NAME: Final = r"""(?:[\w$#@]+|"[^"]+"|`[^`]+`|\[[^\]]+\])"""
_QNAME: Final = rf"{_NAME}(?:\s*\.\s*{_NAME})*"
_CREATE: Final = (
    r"\s*CREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?"
    r"(?:(?:EDITIONABLE|NONEDITIONABLE)\s+)?(?:DEFINER\s*=\s*\S+\s+)?"
)
_TABLE_RE: Final = re.compile(
    _CREATE
    + r"(?:(?:GLOBAL|LOCAL)\s+)?(?:(?:TEMP|TEMPORARY|UNLOGGED|EXTERNAL|TRANSIENT|COLUMN|ROW)\s+)?TABLE\s+"
    + rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_QNAME})",
    _FLAGS,
)
_VIEW_RE: Final = re.compile(
    _CREATE
    + r"(?:(?:FORCE|NOFORCE|TEMP|TEMPORARY|RECURSIVE|SECURE)\s+)*(?:ALGORITHM\s*=\s*\w+\s+)?"
    + r"(?:DEFINER\s*=\s*\S+\s+)?(?:SQL\s+SECURITY\s+\w+\s+)?(?:MATERIALIZED\s+)?VIEW\s+"
    + rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_QNAME})(?P<columns>\s*\([^)]*\))?",
    _FLAGS,
)
_ROUTINE_RE: Final = re.compile(
    _CREATE
    + rf"(?:AGGREGATE\s+)?(?P<kind>FUNCTION|PROCEDURE|PROC)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_QNAME})",
    _FLAGS,
)
_TRIGGER_RE: Final = re.compile(
    _CREATE + rf"(?:CONSTRAINT\s+)?TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_QNAME})", _FLAGS
)
_PACKAGE_BODY_RE: Final = re.compile(_CREATE + rf"PACKAGE\s+BODY\s+(?P<name>{_QNAME})", _FLAGS)
_EVENT = r"(?:INSERT|UPDATE(?:\s+OF\s+[\w\s,\"`]+?)?|DELETE|TRUNCATE)"
_TRIGGER_STANDARD_RE: Final = re.compile(
    rf"(?P<timing>BEFORE|AFTER|INSTEAD\s+OF)\s+(?P<events>{_EVENT}(?:\s+OR\s+{_EVENT})*)\s+ON\s+(?P<table>{_QNAME})",
    _FLAGS,
)
_TRIGGER_TSQL_RE: Final = re.compile(
    rf"ON\s+(?P<table>{_QNAME})\s+(?:WITH\s+[\w\s,]+?\s+)?(?P<timing>AFTER|FOR|INSTEAD\s+OF)\s+"
    r"(?P<events>(?:INSERT|UPDATE|DELETE)(?:\s*,\s*(?:INSERT|UPDATE|DELETE))*)",
    _FLAGS,
)
_EVENT_WORD_RE: Final = re.compile(r"INSERT|UPDATE|DELETE|TRUNCATE", re.IGNORECASE)
_MEMBER_RE: Final = re.compile(
    rf"^[ \t]*(?P<kind>PROCEDURE|FUNCTION)\s+(?P<name>{_NAME})", re.IGNORECASE | re.MULTILINE
)
_ALTER_ADD_RE: Final = re.compile(
    rf"\s*ALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?(?P<table>{_QNAME})\s+ADD\s+(?:COLUMN\s+)?"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<column>{_NAME})\s+(?P<type>[^,;]+)",
    _FLAGS,
)
_RETURNS_RE: Final = re.compile(
    r"\bRETURNS?\s+((?:SETOF\s+|TABLE\s*)?[\w.\"]+(?:\s*\([^)]*\))?)", re.IGNORECASE
)
_BODY_START_RE: Final = re.compile(r"\b(?:AS|IS|BEGIN)\b", re.IGNORECASE)
_LEADING_BLOCK_WORDS_RE: Final = re.compile(r"\s*(?:(?:BEGIN|ELSE|THEN|LOOP|DO|ATOMIC)\b\s*)+", re.IGNORECASE)
_QUERY_WORD_RE: Final = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|MERGE|WITH|TRUNCATE|REPLACE|CALL|EXEC|EXECUTE|PERFORM|LOCK)\b",
    re.IGNORECASE,
)
_FILE_HEADER_RE: Final = re.compile(r"\A\s*((?:--[^\n]*\n)+|/\*.*?\*/)", re.DOTALL)
_TYPE_WORD_RE: Final = re.compile(r"[A-Za-z_]+")
_PARAM_MODES: Final = frozenset({"IN", "OUT", "INOUT", "VARIADIC", "READONLY", "OUTPUT"})
_CONSTRAINT_WORDS: Final = frozenset(
    {
        "PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE", "CHECK",
        "KEY", "INDEX", "FULLTEXT", "SPATIAL", "EXCLUDE",
        "LIKE", "PERIOD",
    }
)  # fmt: skip
_TYPE_ENDERS: Final = frozenset(
    {
        "NOT", "NULL", "DEFAULT", "PRIMARY", "REFERENCES", "UNIQUE", "CHECK", "COMMENT", "AUTO_INCREMENT",
        "AUTOINCREMENT", "GENERATED", "COLLATE", "CONSTRAINT", "IDENTITY", "ON", "ENCODE", "KEY", "AS",
    }
)  # fmt: skip

_MAX_DDL: Final = 4000
_MAX_SCRIPT_STATEMENTS: Final = 400


@dataclass(slots=True)
class Statement:
    """One top-level statement of a SQL script."""

    text: str
    offset: int
    #: Comment block written directly above the statement.
    doc: str | None = None


def split_statements(text: str) -> list[Statement]:
    """Split a SQL script into top-level statements. Routine bodies stay intact."""
    batch_mode = bool(_GO_LINE_RE.search(text))
    slash_mode = bool(_SLASH_LINE_RE.search(text))
    statements: list[Statement] = []
    delimiter = ";"
    length = len(text)
    position = 0
    start = -1
    end = -1
    paren_depth = 0
    block_depth = 0
    saw_begin = False
    expects_block = False
    procedural = False
    package = False
    head: list[str] = []
    after_as = False
    pending_doc: list[str] = []
    newlines = 0
    line_start = True

    def flush() -> None:
        nonlocal start, end, paren_depth, block_depth, saw_begin, expects_block, procedural, package, after_as
        if start >= 0 and end > start:
            doc = clean_doc_comment("\n".join(pending_doc)) if pending_doc else None
            statements.append(Statement(text[start:end], start, doc))
        pending_doc.clear()
        head.clear()
        start = end = -1
        paren_depth = block_depth = 0
        saw_begin = expects_block = procedural = package = after_as = False

    while position < length:
        if line_start:
            line_start = False
            directive = _DIRECTIVE_RE.match(text, position)
            if directive is not None and paren_depth == 0:
                if directive.group("delim"):
                    flush()
                    delimiter = directive.group("delim")
                    position = directive.end()
                    continue
                if directive.group("go") or (
                    directive.group("slash") and (start < 0 or procedural or slash_mode)
                ):
                    flush()
                    position = directive.end()
                    continue
        if delimiter != ";" and text.startswith(delimiter, position):
            flush()
            position += len(delimiter)
            continue
        match = _LEX_RE.match(text, position)
        if match is None:  # pragma: no cover - the ``other`` alternative matches any character
            position += 1
            continue
        kind = match.lastgroup
        lexeme_end = match.end()
        if kind == "nl":
            line_start = True
            if start < 0:
                newlines += 1
                if newlines >= 2:
                    pending_doc.clear()
            position = lexeme_end
            continue
        if kind == "ws":
            position = lexeme_end
            continue
        if kind == "comment":
            if start < 0:
                pending_doc.append(match.group())
                newlines = 0
            position = lexeme_end
            continue

        # A significant lexeme.
        if start < 0:
            start = position
            newlines = 0
        lexeme = match.group()
        if after_as:
            after_as = False
            if kind in ("dollar", "string"):
                expects_block = False  # the body is a quoted string, not a block
        if kind == "word":
            upper = lexeme.upper()
            if len(head) < 8:
                head.append(upper)
                if len(head) == 1 and upper in ("BEGIN", "DECLARE"):
                    following = _NEXT_WORD_RE.match(text, lexeme_end)
                    next_word = (following.group(1) or "").upper() if following else ""
                    if upper == "DECLARE" and not next_word.startswith("@"):
                        procedural = expects_block = True
                    elif upper == "BEGIN" and next_word not in _NOT_BLOCK_AFTER_BEGIN:
                        procedural = True
                elif (
                    head[0] == "CREATE"
                    and upper in _PROCEDURAL_OBJECTS
                    and not procedural
                    and paren_depth == 0
                ):
                    procedural = True
                    package = upper == "PACKAGE"
            if procedural:
                if upper == "BEGIN":
                    following = _NEXT_WORD_RE.match(text, lexeme_end)
                    next_word = (following.group(1) or "").upper() if following else ""
                    if next_word not in _NOT_BLOCK_AFTER_BEGIN or not saw_begin:
                        block_depth += 1
                        saw_begin = True
                elif upper == "CASE":
                    block_depth += 1
                elif upper == "END":
                    following = _NEXT_WORD_RE.match(text, lexeme_end)
                    next_word = (following.group(1) or "").upper() if following else ""
                    if next_word not in _LOOP_CLOSERS:
                        block_depth -= 1
                elif upper in ("AS", "IS") and not saw_begin and paren_depth == 0:
                    expects_block = True
                    after_as = True
        elif kind == "other":
            if lexeme == "(":
                paren_depth += 1
            elif lexeme == ")":
                paren_depth = max(0, paren_depth - 1)
            elif lexeme == ";" and delimiter == ";" and paren_depth == 0:
                if not procedural:
                    done = True
                elif package or batch_mode or slash_mode:
                    done = False
                elif saw_begin:
                    done = block_depth <= 0
                else:
                    done = not expects_block
                if done:
                    statement_text = text[start:end] if end > start else ""
                    flush()
                    position = lexeme_end
                    if _COPY_STDIN_RE.match(statement_text):
                        terminator = _COPY_END_RE.search(text, position)
                        position = terminator.end() if terminator is not None else length
                    continue
        end = lexeme_end
        position = lexeme_end
    flush()
    return statements


def mask_comments(text: str) -> str:
    """Replace comments by spaces (newlines kept) so offsets and line numbers are preserved."""
    if "--" not in text and "/*" not in text:
        return text
    pieces: list[str] = []
    for match in _LEX_RE.finditer(text):
        lexeme = match.group()
        pieces.append(re.sub(r"[^\n]", " ", lexeme) if match.lastgroup == "comment" else lexeme)
    return "".join(pieces)


def display_name(raw: str) -> str:
    """Unqualified identifier as written, without quoting."""
    last = re.split(r"\s*\.\s*", raw.strip())[-1]
    return last[1:-1] if last[:1] in ('"', "`", "[") else last


def _schema_of(raw: str) -> str | None:
    parts = re.split(r"\s*\.\s*", raw.strip())
    return display_name(parts[-2]) if len(parts) >= 2 else None


class SqlExtractor:
    """Extractor for ``.sql`` scripts."""

    language: ClassVar[Language] = Language.SQL

    def parse(self, path: str, data: bytes) -> ModuleDef:
        text = data.decode("utf-8", "replace")
        line_count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        module = ModuleDef(path=path, language=Language.SQL, line_count=line_count)
        lines = _LineIndex(text)
        script = FunctionDef(
            name=MODULE_SCOPE,
            qualified_name=MODULE_SCOPE,
            kind=FunctionKind.SCRIPT,
            start_line=1,
            end_line=max(line_count, 1),
            signature="script statements",
        )
        seen_script: set[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = set()
        tables: dict[str, TableDef] = {}

        header = _FILE_HEADER_RE.match(text)
        if header is not None:
            module.docstring = clean_doc_comment(header.group(1))
        statements = split_statements(text)
        for statement in statements:
            masked = mask_comments(statement.text)
            start_line = lines.line(statement.offset)
            end_line = lines.line(statement.offset + max(len(statement.text.rstrip()) - 1, 0))

            if (match := _PACKAGE_BODY_RE.match(masked)) is not None:
                self._package_body(module, statement, masked, match, lines)
            elif (match := _ROUTINE_RE.match(masked)) is not None:
                module.functions.append(
                    self._routine(
                        statement.text, masked, match.group("name"), match.end(), statement.offset, lines,
                        doc=statement.doc, qualifier=None,
                    )
                )  # fmt: skip
            elif (match := _TRIGGER_RE.match(masked)) is not None:
                self._trigger(module, statement, masked, match, lines)
            elif (match := _VIEW_RE.match(masked)) is not None:
                table = self._view(statement, masked, match, start_line, end_line)
                module.tables.append(table)
            elif (match := _TABLE_RE.match(masked)) is not None:
                table = self._table(module, statement, masked, match, start_line, end_line)
                module.tables.append(table)
                tables[table.name.lower()] = table
            else:
                if _COPY_STDIN_RE.match(masked):
                    continue  # bulk data load from a dump
                added = _ALTER_ADD_RE.match(masked)
                if added is not None:
                    self._alter_add(tables, added)
                if len(script.sql) < _MAX_SCRIPT_STATEMENTS:
                    sql = make_sql_statement(statement.text, start_line, end_line)
                    if sql is not None and (sql.reads or sql.writes or sql.routines):
                        key = (sql.operation, sql.reads, sql.writes, sql.routines)
                        if key not in seen_script:
                            seen_script.add(key)
                            script.sql.append(sql)
                            for routine in sql.routines:
                                script.calls.append(CallSite(routine, start_line))
        if script.sql:
            module.functions.append(script)
        return module

    # ------------------------------------------------------------------- tables

    def _table(
        self,
        module: ModuleDef,
        statement: Statement,
        masked: str,
        match: re.Match[str],
        start_line: int,
        end_line: int,
    ) -> TableDef:
        raw_name = match.group("name")
        table = TableDef(
            name=display_name(raw_name),
            kind="table",
            start_line=start_line,
            end_line=end_line,
            ddl=_truncate(statement.text.strip(), _MAX_DDL),
            schema=_schema_of(raw_name),
        )
        rest = masked[match.end() :]
        if re.match(r"\s*(?:AS\b|\(\s*SELECT\b)", rest, re.IGNORECASE):  # CREATE TABLE x AS SELECT ...
            table.reads = analyze_sql(rest).reads
            return table
        columns = _columns_from_tree(statement.text)
        table.columns = columns if columns is not None else _columns_lexical(rest)
        return table

    def _view(
        self, statement: Statement, masked: str, match: re.Match[str], start_line: int, end_line: int
    ) -> TableDef:
        raw_name = match.group("name")
        table = TableDef(
            name=display_name(raw_name),
            kind="view",
            start_line=start_line,
            end_line=end_line,
            ddl=_truncate(statement.text.strip(), _MAX_DDL),
            schema=_schema_of(raw_name),
        )
        columns = match.group("columns")
        if columns:
            table.columns = [
                ColumnDef(display_name(column), "")
                for column in columns.strip(" ()\n\t").split(",")
                if column.strip()
            ]
        self_name = normalize_table_name(raw_name)
        table.reads = tuple(name for name in analyze_sql(masked[match.end() :]).reads if name != self_name)
        return table

    def _alter_add(self, tables: dict[str, TableDef], match: re.Match[str]) -> None:
        table = tables.get(display_name(match.group("table")).lower())
        column = display_name(match.group("column"))
        if table is None or column.upper() in _CONSTRAINT_WORDS:
            return
        if all(existing.name.lower() != column.lower() for existing in table.columns):
            table.columns.append(ColumnDef(column, _column_type(match.group("type").split())))

    # ----------------------------------------------------------------- routines

    def _routine(
        self,
        text: str,
        masked: str,
        raw_name: str,
        name_end: int,
        offset: int,
        lines: _LineIndex,
        *,
        doc: str | None,
        qualifier: str | None,
        kind: FunctionKind = FunctionKind.PROCEDURE,
    ) -> FunctionDef:
        """Build the scope for a routine whose source is ``text`` (located at ``offset`` in the file)."""
        name = display_name(raw_name)
        start_line = lines.line(offset)
        end_line = lines.line(offset + max(len(text.rstrip()) - 1, 0))
        params_text, params_end = _balanced(masked, name_end)
        header_end = _BODY_START_RE.search(masked, params_end)
        if not params_text and header_end is not None and "@" in masked[name_end : header_end.start()]:
            params_text = masked[name_end : header_end.start()]  # T-SQL: parameters without parentheses
        header = masked[
            : header_end.start() if header_end is not None else min(len(masked), params_end + 200)
        ]
        returns = _RETURNS_RE.search(header, params_end)
        fn = FunctionDef(
            name=name,
            qualified_name=f"{qualifier}.{name}" if qualifier else name,
            kind=kind,
            start_line=start_line,
            end_line=end_line,
            signature=collapse_ws(header),
            params=_params(params_text),
            return_type=collapse_ws(returns.group(1), 60) if returns is not None else None,
            docstring=doc,
            name_line=lines.line(offset + masked.find(raw_name)) if raw_name in masked else start_line,
        )
        body_start = header_end.start() if header_end is not None else params_end
        self._analyse_body(fn, text, masked, body_start, offset, lines)
        return fn

    def _analyse_body(
        self, fn: FunctionDef, text: str, masked: str, body_start: int, offset: int, lines: _LineIndex
    ) -> None:
        """Split a routine body into statements; record queries and routine invocations."""
        self_name = fn.name.lower()
        seen_calls: set[str] = set()
        for piece_start, piece_end in _body_pieces(masked, body_start):
            piece = masked[piece_start:piece_end]
            lead = _LEADING_BLOCK_WORDS_RE.match(piece)
            if lead is not None:
                piece_start += lead.end()
                piece = masked[piece_start:piece_end]
            # Drop leading control-flow lines (``IF x THEN``) so the scope starts at the query itself.
            while "\n" in piece:
                first_line, _, remainder = piece.partition("\n")
                if _QUERY_WORD_RE.search(first_line):
                    break
                piece_start += len(first_line) + 1
                piece = remainder
            if not piece.strip():
                continue
            stripped_lead = len(piece) - len(piece.lstrip())
            line = lines.line(offset + piece_start + stripped_lead)
            end_line = lines.line(offset + piece_start + max(len(piece.rstrip()) - 1, 0))
            analysis = analyze_sql(piece)
            for name in (*analysis.routines, *analysis.functions):
                if name != self_name and name not in seen_calls:
                    seen_calls.add(name)
                    fn.calls.append(CallSite(name, line))
            if analysis.reads or analysis.writes:
                fn.complexity += len(
                    re.findall(r"\b(?:IF|ELSIF|ELSEIF|WHEN|WHILE|LOOP)\b", piece, re.IGNORECASE)
                )
                fn.sql.append(
                    SqlStatement(
                        text=_truncate(_tidy(text[piece_start:piece_end]), _MAX_DDL),
                        line=line,
                        end_line=end_line,
                        operation=analysis.operation,
                        reads=analysis.reads,
                        writes=analysis.writes,
                        write_ops=analysis.write_ops,
                        routines=analysis.routines,
                        functions=analysis.functions,
                    )
                )

    def _package_body(
        self, module: ModuleDef, statement: Statement, masked: str, match: re.Match[str], lines: _LineIndex
    ) -> None:
        """Oracle ``CREATE PACKAGE BODY``: every member procedure / function is its own scope."""
        package = display_name(match.group("name"))
        members = list(_MEMBER_RE.finditer(masked, match.end()))
        for index, member in enumerate(members):
            member_end = members[index + 1].start() if index + 1 < len(members) else len(masked)
            member_text = statement.text[member.start() : member_end]
            member_masked = masked[member.start() : member_end]
            if not re.search(r"\bBEGIN\b", member_masked, re.IGNORECASE):
                continue  # forward declaration
            doc = _comment_above(statement.text, member.start())
            module.functions.append(
                self._routine(
                    member_text.rstrip(),
                    member_masked.rstrip(),
                    member.group("name"),
                    member.end() - member.start(),
                    statement.offset + member.start(),
                    lines,
                    doc=doc,
                    qualifier=package,
                )
            )

    def _trigger(
        self, module: ModuleDef, statement: Statement, masked: str, match: re.Match[str], lines: _LineIndex
    ) -> None:
        details = _TRIGGER_STANDARD_RE.search(masked, match.end()) or _TRIGGER_TSQL_RE.search(
            masked, match.end()
        )
        fn = self._routine(
            statement.text,
            masked,
            match.group("name"),
            match.end(),
            statement.offset,
            lines,
            doc=statement.doc,
            qualifier=None,
            kind=FunctionKind.TRIGGER,
        )
        fn.params = []
        if details is not None:
            # Everything after ``ON <table>`` is body (inline statements or EXECUTE FUNCTION fn()).
            fn.sql.clear()
            fn.calls.clear()
            self._analyse_body(fn, statement.text, masked, details.end(), statement.offset, lines)
            fn.signature = collapse_ws(masked[: details.end()])
            timing = re.sub(r"\s+", " ", details.group("timing").upper())
            events = tuple(
                dict.fromkeys(event.upper() for event in _EVENT_WORD_RE.findall(details.group("events")))
            )
            module.triggers.append(
                TriggerDef(
                    name=fn.name,
                    table=display_name(details.group("table")),
                    events=events,
                    timing="AFTER" if timing == "FOR" else timing,
                    start_line=fn.start_line,
                    end_line=fn.end_line,
                )
            )
        module.functions.append(fn)


# ------------------------------------------------------------------------- helpers


class _LineIndex:
    """Maps character offsets to 1-based line numbers."""

    def __init__(self, text: str) -> None:
        self._starts = [0]
        self._starts.extend(match.end() for match in re.finditer(r"\n", text))

    def line(self, offset: int) -> int:
        return bisect_right(self._starts, offset)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n-- … truncated"


def _tidy(text: str) -> str:
    lines = [line.rstrip() for line in text.strip("\n").splitlines()]
    if not lines:
        return ""
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    common = min(indents, default=0)
    return "\n".join(line[common:] for line in lines).strip()


def _balanced(text: str, start: int) -> tuple[str, int]:
    """Content of the parenthesised group starting at or after ``start`` and the offset after it."""
    opening = re.compile(r"\s*\(").match(text, start)
    if opening is None:
        return "", start
    depth = 0
    for index in range(opening.end() - 1, min(len(text), opening.end() + 20000)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[opening.end() : index], index + 1
    return "", start


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for match in _LEX_RE.finditer(text):
        lexeme = match.group()
        if match.lastgroup == "other":
            if lexeme == "(":
                depth += 1
            elif lexeme == ")":
                depth -= 1
            elif lexeme == "," and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(lexeme)
    if current:
        parts.append("".join(current))
    return parts


def _params(text: str) -> list[Param]:
    params: list[Param] = []
    for part in _split_top_level(text):
        words = [word for word in part.split() if word.upper() not in _PARAM_MODES]
        if not words:
            continue
        name = words[0].strip('"`[]')
        type_name = words[1].strip(",") if len(words) > 1 and words[1].upper() not in _PARAM_MODES else None
        if type_name and type_name.upper() in ("DEFAULT", ":=", "="):
            type_name = None
        if re.fullmatch(r"[@\w$#]+", name):
            params.append(Param(name, type_name))
    return params


def _column_type(words: list[str]) -> str:
    kept: list[str] = []
    for word in words:
        leading = _TYPE_WORD_RE.match(word)
        inside_parentheses = "(" in "".join(kept).rsplit(")", 1)[-1]
        if leading is not None and leading.group().upper() in _TYPE_ENDERS and not inside_parentheses:
            break
        kept.append(word)
    return " ".join(kept).strip(" ,")


def _columns_lexical(rest: str) -> list[ColumnDef]:
    """Columns from the text following ``CREATE TABLE <name>`` without using a grammar."""
    body, _end = _balanced(rest, 0)
    columns: list[ColumnDef] = []
    for part in _split_top_level(body):
        words = part.split()
        if not words or words[0].upper().strip('"`[]') in _CONSTRAINT_WORDS:
            continue
        name_match = re.match(rf"\s*({_NAME})\s*(.*)", part, re.DOTALL)
        if name_match is None:
            continue
        name = display_name(name_match.group(1))
        if not re.fullmatch(r"[\w$# ]+", name):
            continue
        columns.append(ColumnDef(name, _column_type(name_match.group(2).split())))
    return columns


_parser: Parser | None = None


def _columns_from_tree(statement: str) -> list[ColumnDef] | None:
    """Columns of a ``CREATE TABLE`` statement from its Tree-sitter parse.

    Returns ``None`` when the grammar could not make sense of the column list
    (unsupported dialect syntax); the caller then falls back to lexical parsing.
    """
    global _parser  # noqa: PLW0603 - one lazily created parser per process
    if len(statement) > 200_000:
        return None
    if _parser is None:
        _parser = Parser(_GRAMMAR)
    data = statement.encode("utf-8", "replace")
    root = _parser.parse(data).root_node
    definitions: Node | None = None
    stack = [root]
    while stack and definitions is None:
        node = stack.pop()
        if node.type == "column_definitions":
            definitions = node
        elif node.type in ("program", "statement", "create_table", "ERROR"):
            stack.extend(reversed(node.named_children))
    if definitions is None:
        return None
    columns: list[ColumnDef] = []
    for child in definitions.named_children:
        if child.type == "ERROR":
            # An unparsed fragment between columns: do not trust a partial list.
            if any(grandchild.type == "column_definition" for grandchild in child.named_children):
                return None
            continue
        if child.type != "column_definition":
            continue
        name = child.child_by_field_name("name")
        type_node = child.child_by_field_name("type")
        if name is None or type_node is None or child.has_error:
            return None
        column = data[name.start_byte : name.end_byte].decode("utf-8", "replace")
        type_text = data[type_node.start_byte : type_node.end_byte].decode("utf-8", "replace")
        columns.append(ColumnDef(display_name(column), collapse_ws(type_text, 80)))
    return columns or None


def _body_pieces(masked: str, start: int) -> list[tuple[int, int]]:
    """``(start, end)`` offsets of the statements inside a routine body.

    Quoted bodies (``AS $$ ... $$``, ``AS '...'``) are entered; ``;`` separates
    statements at parenthesis depth zero.
    """
    pieces: list[tuple[int, int]] = []
    stack: list[tuple[int, int]] = [(start, len(masked))]
    while stack:
        region_start, region_end = stack.pop()
        piece_start = region_start
        depth = 0
        for match in _LEX_RE.finditer(masked, region_start, region_end):
            kind = match.lastgroup
            lexeme = match.group()
            if kind == "dollar" and len(lexeme) > 40:
                tag_length = len(match.group("tag")) + 2
                if piece_start < match.start():
                    pieces.append((piece_start, match.start()))
                stack.append(
                    (match.start() + tag_length, max(match.start() + tag_length, match.end() - tag_length))
                )
                piece_start = match.end()
            elif kind == "string" and len(lexeme) > 60 and lexeme[:1] == "'" and depth == 0 and ";" in lexeme:
                # A routine body written as a plain string literal: CREATE FUNCTION ... AS '...';
                if piece_start < match.start():
                    pieces.append((piece_start, match.start()))
                stack.append((match.start() + 1, match.end() - 1))
                piece_start = match.end()
            elif kind == "other":
                if lexeme == "(":
                    depth += 1
                elif lexeme == ")":
                    depth = max(0, depth - 1)
                elif lexeme == ";" and depth == 0:
                    if piece_start < match.start():
                        pieces.append((piece_start, match.start()))
                    piece_start = match.end()
        if piece_start < region_end:
            pieces.append((piece_start, region_end))
    pieces.sort()
    return pieces


def _comment_above(text: str, offset: int) -> str | None:
    """Comment lines directly above ``offset`` (used for members of a package body)."""
    before = text[:offset].rstrip(" \t")
    lines = before.split("\n")
    if lines and not lines[-1].strip():
        lines.pop()
    collected: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if (
            stripped.startswith("--")
            or stripped.startswith("/*")
            or stripped.endswith("*/")
            or stripped.startswith("*")
        ):
            collected.append(stripped)
            if stripped.startswith("/*"):
                break
        else:
            break
    if not collected:
        return None
    collected.reverse()
    return clean_doc_comment("\n".join(collected))
