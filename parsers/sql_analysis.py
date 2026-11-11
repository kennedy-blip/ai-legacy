"""Dialect-tolerant extraction of table and routine references from SQL text.

Why this is not a grammar-based parser
--------------------------------------
SQL found in legacy application code is rarely a complete, standard statement. It
carries driver placeholders (``?``, ``:id``, ``%s``, ``@p``, ``#{x}``), vendor syntax
(``[dbo].[Orders]``, ``FOR UPDATE``, ``{call sp(?)}``) and holes where a table name
or predicate is concatenated in at runtime. A strict grammar reports an error for
most of those and yields no usable tree.

The question Archaeo-Code needs answered is narrower than "parse this statement":
*which tables does it read, which does it mutate, and which routines does it call?*
That is decided by a handful of keywords (``FROM``, ``JOIN``, ``INTO``, ``UPDATE`` ...)
and the identifiers that follow them, so this module tokenises the text and walks
the tokens with a small state machine. Host-language structure and the column
definitions of ``.sql`` files still come from Tree-sitter (:mod:`parsers.sql`), and
``tests/unit/test_sql_analysis.py`` cross-checks this scanner against the
Tree-sitter SQL grammar on statements that both accept.

Known limits (by design):

* Table identity is the unqualified, lower-cased name, so ``sales.orders`` and
  ``archive.orders`` are treated as one table.
* Unquoted tables named like a clause keyword (``order``, ``group``) are not seen.
* A table name computed at runtime is reported as unknown rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_TOKEN_RE: Final = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>--[^\n]*|/\*.*?\*/)
    | (?P<string>'(?:[^'\\]|''|\\.)*')
    | (?P<param>
          \?
        | %\([^)]*\)[sd] | %[sd]
        | \$\d+
        | [\#$]\{[^}]*\}
        | \{\w*\}
        | :[A-Za-z_]\w*
        | @@?[A-Za-z_]\w*
      )
    | (?P<word>
          (?:[A-Za-z_][\w$\#]*|"[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\])
          (?:\s*\.\s*(?:[A-Za-z_][\w$\#]*|"[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\]|\*))*
      )
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<punct>[(),;])
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

_NAME_PART_RE: Final = re.compile(r"[A-Za-z_][\w$#]*")

#: Keywords that end a FROM table list at the current parenthesis depth.
_FROM_ENDERS: Final = frozenset(
    [
        "WHERE",
        "GROUP",
        "ORDER",
        "HAVING",
        "LIMIT",
        "OFFSET",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "MINUS",
        "SET",
        "VALUES",
        "RETURNING",
        "WINDOW",
        "FETCH",
        "FOR",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "LOOP",
        "BEGIN",
        "SELECT",
        "DO",
        "CONFLICT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
    ]
)

#: Keywords that may sit between FROM/JOIN and the table name without cancelling it.
_TABLE_PREFIXES: Final = frozenset({"LATERAL", "ONLY", "TABLE", "TABLES"})

#: Words that are never a table name, alias or routine in the positions inspected.
#: Kept deliberately short: a word listed here cannot be recognised as an unquoted
#: table name, so only clause-level syntax belongs in it.
_KEYWORDS: Final = (
    frozenset(
        [
            "ALL",
            "ALTER",
            "AND",
            "ANY",
            "AS",
            "ASC",
            "BETWEEN",
            "BY",
            "CALL",
            "CASCADE",
            "CASE",
            "CREATE",
            "CROSS",
            "DEFAULT",
            "DESC",
            "DISTINCT",
            "DROP",
            "EACH",
            "ELSIF",
            "EXEC",
            "EXECUTE",
            "EXISTS",
            "FROM",
            "FULL",
            "IF",
            "IGNORE",
            "ILIKE",
            "IMMEDIATE",
            "IN",
            "INNER",
            "INTO",
            "IS",
            "JOIN",
            "LEFT",
            "LIKE",
            "LOCK",
            "MATCHED",
            "NATURAL",
            "NO",
            "NOT",
            "NOWAIT",
            "NULL",
            "OF",
            "ON",
            "OR",
            "OUTER",
            "OVER",
            "PARTITION",
            "PERFORM",
            "RECURSIVE",
            "REPLACE",
            "RESTRICT",
            "RETURN",
            "RIGHT",
            "SKIP",
            "STRAIGHT_JOIN",
            "TRUNCATE",
            "USING",
            "WAIT",
            "WITH",
        ]
    )
    | _FROM_ENDERS
    | _TABLE_PREFIXES
)

#: Functions whose argument list uses FROM as plain syntax: ``EXTRACT(YEAR FROM d)``.
_FROM_FUNCTIONS: Final = frozenset({"EXTRACT", "SUBSTRING", "TRIM", "OVERLAY", "POSITION"})

#: T-SQL table hints written as ``FROM Orders (NOLOCK)``; the table is not a function call.
_TABLE_HINTS: Final = frozenset(
    {
        "NOLOCK",
        "READUNCOMMITTED",
        "READCOMMITTED",
        "UPDLOCK",
        "ROWLOCK",
        "HOLDLOCK",
        "TABLOCK",
        "TABLOCKX",
        "READPAST",
    }
)

_INSERT_MODIFIERS: Final = frozenset(
    {
        "LOW_PRIORITY",
        "DELAYED",
        "HIGH_PRIORITY",
        "IGNORE",
        "OVERWRITE",
        "OR",
        "REPLACE",
        "ROLLBACK",
        "ABORT",
        "FAIL",
        "TABLE",
    }
)
_UPDATE_MODIFIERS: Final = frozenset(
    {"LOW_PRIORITY", "IGNORE", "ONLY", "OR", "ROLLBACK", "ABORT", "REPLACE", "FAIL"}
)
_DDL_NOISE: Final = frozenset(
    {
        "OR",
        "REPLACE",
        "TEMP",
        "TEMPORARY",
        "GLOBAL",
        "LOCAL",
        "UNLOGGED",
        "UNIQUE",
        "MATERIALIZED",
        "IF",
        "NOT",
        "EXISTS",
        "ONLY",
        "CONCURRENTLY",
    }
)
_DDL_OBJECTS: Final = frozenset(
    {"TABLE", "VIEW", "INDEX", "FUNCTION", "PROCEDURE", "TRIGGER", "SEQUENCE", "SCHEMA", "DATABASE", "TYPE"}
)
_MAIN_VERBS: Final = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"})
_CURSOR_FETCH: Final = frozenset({"FETCH", "NEXT", "PRIOR", "FIRST", "LAST"})
#: Helpers that wrap dynamic SQL after EXECUTE; they are not stored routines.
_DYNAMIC_SQL_HELPERS: Final = frozenset({"format", "concat", "sp_executesql"})

#: Pseudo-tables and English words that appear after FROM in prose, never real tables.
_NOT_TABLES: Final = frozenset(
    {
        # pseudo-tables
        "dual", "sysdummy1", "inserted", "deleted", "excluded", "stdin", "stdout",
        # prose
        "the", "an", "this", "that", "these", "those", "your", "my", "our", "its", "here", "there",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class SqlAnalysis:
    """Tables and routines referenced by a piece of SQL."""

    #: Dominant operation: SELECT / INSERT / UPDATE / DELETE / MERGE / TRUNCATE / CALL / LOCK / DDL / UNKNOWN.
    operation: str
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    #: ``(table, operation)`` pairs in order of appearance.
    write_ops: tuple[tuple[str, str], ...] = ()
    #: Routines invoked explicitly (``CALL x``, ``EXEC x``, ``PERFORM x()``).
    routines: tuple[str, ...] = ()
    #: Identifiers applied like functions (``foo(...)``); may name stored functions.
    functions: tuple[str, ...] = ()
    #: ``("CREATE TABLE", "tbl")``-style pairs for DDL statements.
    ddl: tuple[tuple[str, str], ...] = ()

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.reads, *self.writes)))

    @property
    def is_empty(self) -> bool:
        """True when no table, routine or DDL target was found."""
        return not (self.reads or self.writes or self.routines or self.ddl)


@dataclass(slots=True)
class _Token:
    kind: str
    raw: str
    #: Upper-cased text for unquoted single-part words; empty otherwise.
    upper: str


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for match in _TOKEN_RE.finditer(text):
        kind = match.lastgroup or "other"
        if kind in ("ws", "comment"):
            continue
        raw = match.group()
        upper = raw.upper() if kind == "word" and _NAME_PART_RE.fullmatch(raw) else ""
        tokens.append(_Token(kind, raw, upper))
    return tokens


def normalize_table_name(raw: str) -> str | None:
    """Reduce a possibly quoted / schema-qualified identifier to its identity.

    Returns the unqualified, lower-cased name, or ``None`` when the text cannot be
    a table name (pseudo-tables, prose words, wildcard paths).
    """
    last = re.split(r"\s*\.\s*", raw.strip())[-1]
    if last[:1] in ('"', "`", "["):
        last = last[1:-1]
    if not _NAME_PART_RE.fullmatch(last):
        return None
    name = last.lower()
    return None if name in _NOT_TABLES else name


class _Scanner:
    """Single left-to-right pass over the tokens of one SQL text."""

    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        # Results accumulated over all statements in the text.
        self.reads: dict[str, None] = {}
        self.write_ops: dict[tuple[str, str], None] = {}
        self.routines: dict[str, None] = {}
        self.functions: dict[str, None] = {}
        self.ddl: dict[tuple[str, str], None] = {}
        self.verbs: list[str] = []
        self._consumed_from: set[int] = set()
        self._begin_statement()

    def _begin_statement(self) -> None:
        # Per-parenthesis-depth state.
        self._in_from: list[bool] = [False]
        self._special: list[bool] = [False]
        # Per-statement state. CTE names and aliases are scoped to one statement.
        self._stmt_reads: dict[str, None] = {}
        self._stmt_writes: dict[tuple[str, str], None] = {}
        self._ctes: set[str] = set()
        self._aliases: dict[str, str] = {}
        self._expect_table = False
        self._alias_target: str | None = None
        self._statement_start = True
        self._cte_prelude = False
        self._using_reads = False
        self._multi_insert = False
        self._verb: str | None = None

    def _end_statement(self) -> None:
        for name in self._stmt_reads:
            if name not in self._ctes:
                self.reads[name] = None
        for name, operation in self._stmt_writes:
            target = self._aliases.get(name, name)
            if target not in self._ctes:
                self.write_ops[(target, operation)] = None
        self._begin_statement()

    # ------------------------------------------------------------- token helpers

    def _word(self, index: int) -> _Token | None:
        if 0 <= index < len(self.tokens) and self.tokens[index].kind == "word":
            return self.tokens[index]
        return None

    def _is_keyword(self, index: int, *words: str) -> bool:
        token = self._word(index)
        return token is not None and token.upper in words

    def _is_identifier(self, index: int) -> bool:
        token = self._word(index)
        return token is not None and token.upper not in _KEYWORDS

    def _is_raw(self, index: int, raw: str) -> bool:
        return 0 <= index < len(self.tokens) and self.tokens[index].raw == raw

    def _skip(self, index: int, words: frozenset[str]) -> int:
        while self._word(index) is not None and self.tokens[index].upper in words:
            index += 1
        return index

    # ------------------------------------------------------------------ recorders

    def _read(self, raw: str) -> str | None:
        name = normalize_table_name(raw)
        if name is not None:
            self._stmt_reads[name] = None
        return name

    def _write(self, raw: str, operation: str) -> str | None:
        name = normalize_table_name(raw)
        if name is not None:
            self._stmt_writes[(name, operation)] = None
        return name

    def _function(self, raw: str) -> None:
        name = normalize_table_name(raw)
        if name is not None:
            self.functions[name] = None

    # ----------------------------------------------------------------------- scan

    def scan(self) -> None:
        index = 0
        count = len(self.tokens)
        while index < count:
            token = self.tokens[index]
            if token.kind == "punct":
                self._punctuation(index, token)
                index += 1
            elif token.kind != "word":
                # A placeholder, literal or operator where a table was expected means the
                # name is computed at runtime: report nothing rather than guess.
                self._expect_table = False
                self._alias_target = None
                self._statement_start = False
                index += 1
            elif token.upper in _KEYWORDS:
                index = self._keyword(index, token)
            else:
                self._identifier(index, token)
                index += 1
        self._end_statement()

    def _punctuation(self, index: int, token: _Token) -> None:
        if token.raw == "(":
            previous = self._word(index - 1)
            self._in_from.append(False)
            self._special.append(previous is not None and previous.upper in _FROM_FUNCTIONS)
            self._expect_table = False
            self._alias_target = None
        elif token.raw == ")":
            if len(self._in_from) > 1:
                self._in_from.pop()
                self._special.pop()
            self._expect_table = False
            self._alias_target = None
        elif token.raw == ",":
            self._expect_table = self._in_from[-1]
            self._alias_target = None
        else:  # ";"
            self._end_statement()

    def _identifier(self, index: int, token: _Token) -> None:
        self._statement_start = False
        if self._cte_prelude and len(self._in_from) == 1:
            name = normalize_table_name(token.raw)
            if name is not None:
                self._ctes.add(name)
            return
        if self._expect_table:
            self._expect_table = False
            if self._is_raw(index + 1, "(") and not self._is_keyword(index + 2, *_TABLE_HINTS):
                # FROM generate_series(...) / unnest(...): a table function, not a table.
                self._function(token.raw)
                return
            self._alias_target = self._read(token.raw)
            return
        if self._alias_target is not None:
            alias = normalize_table_name(token.raw)
            if alias is not None:
                self._aliases[alias] = self._alias_target
            self._alias_target = None
            return
        if self._is_raw(index + 1, "("):
            self._function(token.raw)

    def _keyword(self, index: int, token: _Token) -> int:
        """Handle a keyword; returns the index of the next token to process."""
        word = token.upper
        first = self._statement_start
        self._statement_start = False
        depth_zero = len(self._in_from) == 1

        if word != "AS":
            self._alias_target = None
        if word not in _TABLE_PREFIXES:
            self._expect_table = False
        if word in _FROM_ENDERS:
            self._in_from[-1] = False
        if word in _MAIN_VERBS and depth_zero:
            self._cte_prelude = False
            self.verbs.append(word)
            if self._verb is None:
                self._verb = word

        if word == "WITH":
            if first:
                self._cte_prelude = True
        elif word == "FROM":
            if index in self._consumed_from or self._special[-1] or self._is_non_table_from(index):
                return index + 1
            self._in_from[-1] = True
            self._expect_table = True
        elif word in ("JOIN", "STRAIGHT_JOIN"):
            self._expect_table = True
        elif word == "USING":
            if self._using_reads and self._is_identifier(index + 1):
                self._in_from[-1] = True
                self._expect_table = True
        elif word == "INTO":
            # Oracle multi-table insert: INSERT ALL INTO a ... INTO b ...
            # Any other later INTO (``RETURNING id INTO v_id``) assigns a variable.
            if self._verb == "INSERT" and self._multi_insert:
                return max(self._target(index + 1, "INSERT"), index + 1)
        elif word in ("INSERT", "REPLACE"):
            return self._insert(index)
        elif word == "UPDATE":
            return self._update(index)
        elif word == "DELETE":
            return self._delete(index)
        elif word == "MERGE":
            return self._merge(index)
        elif word == "TRUNCATE":
            return self._truncate(index)
        elif word in ("CALL", "EXEC", "EXECUTE", "PERFORM"):
            return self._call(index)
        elif word in ("CREATE", "ALTER", "DROP"):
            return self._ddl(index, word)
        elif word == "LOCK" and self._is_keyword(index + 1, "TABLE", "TABLES"):
            self._in_from[-1] = True
            self._expect_table = True
            self.verbs.append("LOCK")
            return index + 2
        return index + 1

    def _is_non_table_from(self, index: int) -> bool:
        """FROM that does not introduce a table list.

        ``a IS [NOT] DISTINCT FROM b`` is a comparison and ``FETCH NEXT FROM cur``
        reads from a cursor.
        """
        if self._is_keyword(index - 1, "DISTINCT") and self._is_keyword(index - 2, "IS", "NOT"):
            return True
        return self._is_keyword(index - 1, *_CURSOR_FETCH) and (
            self._is_keyword(index - 1, "FETCH") or self._is_keyword(index - 2, "FETCH")
        )

    def _target(self, index: int, operation: str) -> int:
        """Record ``tokens[index]`` as a write target if it is an identifier."""
        if not self._is_identifier(index):
            return index
        self._alias_target = self._write(self.tokens[index].raw, operation)
        return index + 1

    def _insert(self, index: int) -> int:
        if self._is_raw(index + 1, "("):  # REPLACE(str, from, to) or MERGE ... THEN INSERT (...)
            return index + 1
        if self._is_keyword(index + 1, "ALL", "FIRST"):
            self._multi_insert = True  # targets follow in the INTO clauses
            return index + 2
        cursor = self._skip(index + 1, _INSERT_MODIFIERS)
        if self._is_keyword(cursor, "INTO"):
            cursor = self._skip(cursor + 1, frozenset({"TABLE"}))
        elif self._is_keyword(index, "REPLACE"):
            return index + 1  # "OR REPLACE" and friends: not a statement on its own
        return max(self._target(cursor, "INSERT"), index + 1)

    def _update(self, index: int) -> int:
        # ON DUPLICATE KEY UPDATE / FOR UPDATE / ON UPDATE CASCADE are not statements.
        if self._is_keyword(index - 1, "KEY", "FOR", "ON"):
            return index + 1
        if self._is_keyword(index + 1, "STATISTICS") and self._is_identifier(index + 2):
            return index + 2  # T-SQL maintenance command
        cursor = self._skip(index + 1, _UPDATE_MODIFIERS)
        return max(self._target(cursor, "UPDATE"), index + 1)

    def _delete(self, index: int) -> int:
        if self._is_keyword(index - 1, "ON"):  # ON DELETE CASCADE
            return index + 1
        cursor = index + 1
        if self._is_keyword(cursor, "FROM"):
            self._consumed_from.add(cursor)
            cursor = self._skip(cursor + 1, frozenset({"ONLY"}))
        after = self._target(cursor, "DELETE")
        if after == cursor:
            return index + 1  # e.g. "AFTER DELETE ON t", or a runtime-computed target
        self._using_reads = True
        return after

    def _merge(self, index: int) -> int:
        cursor = index + 1
        if self._is_keyword(cursor, "INTO"):
            cursor += 1
        after = self._target(cursor, "MERGE")
        if after != cursor:
            self._using_reads = True
        return max(after, index + 1)

    def _truncate(self, index: int) -> int:
        cursor = self._skip(index + 1, frozenset({"TABLE", "ONLY"}))
        start = cursor
        while self._is_identifier(cursor):
            self._write(self.tokens[cursor].raw, "TRUNCATE")
            cursor += 1
            if not self._is_raw(cursor, ","):
                break
            cursor += 1
        if cursor != start:
            self.verbs.append("TRUNCATE")
        return max(cursor, index + 1)

    def _call(self, index: int) -> int:
        cursor = self._skip(index + 1, frozenset({"FUNCTION", "PROCEDURE", "IMMEDIATE"}))
        # T-SQL: EXEC @rc = dbo.sp_x
        if (
            cursor + 1 < len(self.tokens)
            and self.tokens[cursor].kind == "param"
            and self._is_raw(cursor + 1, "=")
        ):
            cursor += 2
        consumed = index + 1
        if self._is_identifier(cursor):
            name = normalize_table_name(self.tokens[cursor].raw)
            if name is not None and name not in _DYNAMIC_SQL_HELPERS:
                self.routines[name] = None
                self.verbs.append("CALL")
            consumed = cursor + 1
            cursor += 1
        # Dynamic SQL: EXECUTE 'DELETE FROM t ...', EXEC('...'), EXEC sp_executesql N'...',
        # EXECUTE format('INSERT INTO %I ...'). Analyse the literal as SQL in its own right.
        for lookahead in range(cursor, min(cursor + 3, len(self.tokens))):
            candidate = self.tokens[lookahead]
            if candidate.kind == "string":
                self._nested(candidate.raw[1:-1].replace("''", "'"))
                break
            if candidate.raw != "(" and candidate.upper != "N":
                break
        return consumed

    def _nested(self, text: str) -> None:
        inner = _Scanner(_tokenize(text))
        inner.scan()
        for name in inner.reads:
            self._stmt_reads[name] = None
        for pair in inner.write_ops:
            self._stmt_writes[pair] = None
        self.routines.update(inner.routines)
        self.functions.update(inner.functions)
        self.ddl.update(inner.ddl)

    def _ddl(self, index: int, verb: str) -> int:
        cursor = self._skip(index + 1, _DDL_NOISE)
        kind = self._word(cursor)
        if kind is None or kind.upper not in _DDL_OBJECTS:
            return index + 1
        cursor = self._skip(cursor + 1, _DDL_NOISE)
        if self._is_identifier(cursor):
            name = normalize_table_name(self.tokens[cursor].raw)
            if name is not None:
                self.ddl[(f"{verb} {kind.upper}", name)] = None
                self.verbs.append("DDL")
            return cursor + 1
        return index + 1

    # --------------------------------------------------------------------- result

    def result(self) -> SqlAnalysis:
        write_ops = tuple(self.write_ops)
        reads = tuple(self.reads)
        routines = tuple(self.routines)
        ddl = tuple(self.ddl)
        if write_ops:
            operation = write_ops[0][1]
        elif ddl:
            operation = "DDL"
        elif routines and not reads:
            operation = "CALL"
        elif reads:
            operation = "LOCK" if self.verbs[:1] == ["LOCK"] else "SELECT"
        else:
            operation = self.verbs[0] if self.verbs else "UNKNOWN"
        return SqlAnalysis(
            operation=operation,
            reads=reads,
            writes=tuple(dict.fromkeys(name for name, _ in write_ops)),
            write_ops=write_ops,
            routines=routines,
            functions=tuple(name for name in self.functions if name not in self.routines),
            ddl=ddl,
        )


def analyze_sql(text: str) -> SqlAnalysis:
    """Extract the tables read and mutated and the routines called by ``text``.

    ``text`` may contain several statements, procedural code around them and
    driver placeholders; anything unrecognised is skipped.
    """
    scanner = _Scanner(_tokenize(text))
    scanner.scan()
    return scanner.result()


# --------------------------------------------------------------------------- detection

_MIN_SQL_LENGTH: Final = 12
_FLAGS: Final = re.IGNORECASE | re.DOTALL

#: An identifier, or a placeholder standing in for one.
_NAME: Final = r"""(?:[\w$#."`\[\]]+|[$#]\{[^}]*\}|\{\w*\}|\?|%s|%\(\w+\)s|:\w+)"""
_IDENT: Final = r"""[\w$#."`\[\]]+"""

_LEAD_RE: Final = re.compile(
    r"\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*[({]?\s*(?:\?\s*=\s*)?([A-Za-z]+)", re.DOTALL
)
_SELECT_FROM_RE: Final = re.compile(r"""\bFROM\s+[\w"`\[(?:%{#$]""", _FLAGS)
_SELECT_CALL_RE: Final = re.compile(r"\s+(?:\*\s+FROM\s+)?[\w.]+\s*\(", _FLAGS)
_USING_RE: Final = re.compile(r"\bUSING\b", re.IGNORECASE)
_END_RE: Final = re.compile(r"\bEND\b", re.IGNORECASE)
#: Syntax that natural-language sentences beginning with a SQL verb do not contain.
_EVIDENCE_RE: Final = re.compile(
    r"[=*<>]|(?<![\w?!.])\?|%s|:\w|\b(?:WHERE|JOIN|VALUES|UNION)\b", re.IGNORECASE
)
_COMPARISON_RE: Final = re.compile(r"[=<>?]|:\w|\bIN\b|\bLIKE\b|\bIS\b", re.IGNORECASE)

_SHAPES: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    verb: tuple(re.compile(pattern, _FLAGS) for pattern in patterns)
    for verb, patterns in {
        "INSERT": (
            rf"\s+(?:\w+\s+){{0,3}}?INTO\s+{_NAME}",
            rf"\s+{_IDENT}\s*\([^)]*\)\s*(?:VALUES|SELECT)\b",
        ),
        "REPLACE": (rf"\s+INTO\s+{_NAME}",),
        "UPDATE": (rf"""\s+(?:\w+\s+){{0,2}}?{_NAME}(?:\s+(?:AS\s+)?\w+)?\s+SET\s+[\w("`\[]""",),
        "DELETE": (rf"\s+FROM\s+{_NAME}", rf"\s+{_IDENT}(?:\s+\w+)?\s+(?:FROM|WHERE)\b"),
        "WITH": (
            r"\s+(?:RECURSIVE\s+)?\w+\s*(?:\([^)]*\)\s*)?AS\s*(?:NOT\s+MATERIALIZED\s*|MATERIALIZED\s*)?\(",
        ),
        "CALL": (rf"\s+{_IDENT}\s*(?:\(|;?\s*$)",),
        "EXEC": (r"\s+(?:@\w+\s*=\s*)?(?:(?:[\w\[\]]+\.)+[\w\[\]]+|\w*_\w+)",),
        "TRUNCATE": (rf"\s+TABLE\s+{_NAME}",),
        "DDL": (
            r"\s+(?:(?:OR|REPLACE|TEMP|TEMPORARY|UNIQUE|MATERIALIZED|GLOBAL|LOCAL|UNLOGGED)\s+)*"
            r"(?:TABLE|VIEW|INDEX|FUNCTION|PROCEDURE|TRIGGER|SEQUENCE)\s+\S",
        ),
        "LOCK": (rf"\s+TABLES?\s+{_NAME}",),
    }.items()
}
_SHAPES["EXECUTE"] = _SHAPES["EXEC"]
_SHAPES["CREATE"] = _SHAPES["ALTER"] = _SHAPES["DROP"] = _SHAPES["DDL"]

_MERGE_RE: Final = re.compile(rf"\s+(?:INTO\s+)?{_NAME}", _FLAGS)
_TRUNCATE_BARE_RE: Final = re.compile(rf"\s+{_IDENT}\s*;?\s*$", _FLAGS)
#: HQL / JPQL shorthand without a SELECT clause: ``from Account a where a.id = :id``.
_HQL_FROM_RE: Final = re.compile(
    r"\s+[A-Z]\w*(?:\s+(?:as\s+|AS\s+)?\w+)?\s+(?i:WHERE|JOIN|LEFT|INNER|ORDER\s+BY|GROUP\s+BY|FETCH)\b",
    re.DOTALL,
)


def looks_like_sql(text: str) -> bool:
    """Cheap test for whether a string literal is worth analysing as SQL.

    Tuned for precision. The text must begin with a SQL verb *and* show the
    structure that verb requires, and a sentence-case verb ("Select ...",
    "Delete ...") additionally needs syntax that prose lacks, so messages such as
    "Update failed" or "Delete from cart" are not mistaken for queries.
    """
    if len(text) < _MIN_SQL_LENGTH:
        return False
    lead = _LEAD_RE.match(text)
    if lead is None:
        return False
    word = lead.group(1)
    verb = word.upper()
    rest = text[lead.end() :]
    sentence_case = len(word) > 1 and word[0].isupper() and word[1:].islower()
    if sentence_case and not _EVIDENCE_RE.search(rest):
        return False

    if verb == "SELECT":
        if not rest[:1].isspace():
            return False
        return bool(_SELECT_FROM_RE.search(rest) or _SELECT_CALL_RE.match(rest))
    if verb == "MERGE":
        return bool(_MERGE_RE.match(rest) and _USING_RE.search(rest))
    if verb == "TRUNCATE":
        return bool(_SHAPES["TRUNCATE"][0].match(rest) or (word.isupper() and _TRUNCATE_BARE_RE.match(rest)))
    if verb == "FROM":
        return bool(_HQL_FROM_RE.match(rest) and _COMPARISON_RE.search(rest))
    if verb in ("BEGIN", "DECLARE"):
        return ";" in rest and bool(_END_RE.search(rest))
    shapes = _SHAPES.get(verb)
    return shapes is not None and any(shape.match(rest) for shape in shapes)
