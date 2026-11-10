"""Shared machinery for the Tree-sitter based language extractors.

Each language module subclasses :class:`TreeSitterExtractor` and supplies the
grammar-specific knowledge (which node types are calls, strings, branches ...).
Everything that is the same in every language lives here:

* :class:`BodyVisitor` - one iterative pass over a function body that collects
  call sites, local variable types, a complexity estimate and embedded SQL.
* Constant folding of string expressions (:meth:`TreeSitterExtractor.eval_string`)
  so that SQL assembled from literals, constants, concatenation, interpolation and
  string builders is analysed as the statement that actually reaches the database.

Traversals use explicit stacks rather than recursion: legacy code bases contain
expressions (thousand-term string concatenations) deep enough to overflow the
Python call stack.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import ClassVar, Final, Protocol

from tree_sitter import Language as TsLanguage
from tree_sitter import Node, Parser

from parsers.ir import (
    CallSite,
    ConstantDef,
    ConstantRef,
    FunctionDef,
    Language,
    ModuleDef,
    SqlStatement,
)
from parsers.sql_analysis import analyze_sql, looks_like_sql

_WS_RE: Final = re.compile(r"\s+")
_SIMPLE_NAME_RE: Final = re.compile(r"[A-Za-z_][\w.]*")
_IDENTIFIER_STRING_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,63}(?:\.[A-Za-z_][A-Za-z0-9_]{2,63})?")
_CONSTANT_NAME_RE: Final = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z]{3,}[A-Z0-9]*")
_TYPE_NAME_RE: Final = re.compile(r"[A-Za-z_]\w*")
_GENERIC_RE: Final = re.compile(r"^([\w.:]+)\s*[<\[](.*)[>\]]$", re.DOTALL)

#: Generic wrappers that are transparent for method dispatch: calling through an
#: ``Optional<Foo>`` / ``unique_ptr<Foo>`` reaches ``Foo``.
_TRANSPARENT_WRAPPERS: Final = frozenset(
    {
        "Optional", "Provider", "ObjectProvider", "Lazy", "Supplier", "Instance", "AtomicReference",
        "unique_ptr", "shared_ptr", "weak_ptr", "scoped_ptr", "auto_ptr", "reference_wrapper", "optional",
        "Promise", "Awaited", "Readonly", "Partial", "Type", "Final", "ClassVar", "Annotated",
    }
)  # fmt: skip
_TYPE_NOISE_RE: Final = re.compile(
    r"\b(?:final|const|volatile|static|mutable|struct|class|enum|typename|readonly|unsigned|signed)\b|@\w+(?:\([^)]*\))?"
)

#: Maximum number of identifier-like string literals remembered per function.
_MAX_STRING_REFS: Final = 48
#: Concatenations longer than this are not folded (generated code, data tables).
_MAX_CONCAT_OPERANDS: Final = 4000
#: SQL statements longer than this are stored truncated; analysis sees the full text.
MAX_SQL_TEXT: Final = 6000

#: Names of logging / printing calls. String literals passed to them are messages,
#: not queries, even when they happen to begin with a SQL verb.
LOG_CALL_NAMES: Final = frozenset(
    {
        "debug", "info", "warn", "warning", "error", "trace", "fatal", "critical", "exception", "log",
        "println", "print", "printf", "fprintf", "puts", "perror", "printStackTrace", "severe", "fine",
        "finer", "finest", "config", "logf", "debugf", "infof", "warnf", "errorf",
    }
)  # fmt: skip


def line_of(node: Node) -> int:
    """1-based line on which ``node`` starts."""
    return int(node.start_point[0]) + 1


def end_line_of(node: Node) -> int:
    """1-based line on which ``node`` ends."""
    row, column = int(node.end_point[0]), int(node.end_point[1])
    # A node that ends at column 0 really ends on the previous line.
    return row if column == 0 and row > int(node.start_point[0]) else row + 1


def collapse_ws(text: str, limit: int = 240) -> str:
    """Single-line rendering of ``text`` capped at ``limit`` characters."""
    flat = _WS_RE.sub(" ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def simple_type(text: str | None) -> str | None:
    """Reduce a type expression to the simple class name used for call resolution.

    ``final List<Foo>`` -> ``List``; ``std::unique_ptr<ns::Foo>&`` -> ``Foo``;
    ``com.acme.Foo[]`` -> ``Foo``; ``Foo | null`` -> ``Foo``; ``Optional[Foo]`` -> ``Foo``.
    """
    if not text:
        return None
    current = _TYPE_NOISE_RE.sub(" ", text)
    for _ in range(4):
        current = current.replace("[]", " ").strip(" \t\n*&?!")
        if "|" in current:  # TypeScript / Python unions: take the first non-null member
            members = [m.strip() for m in current.split("|")]
            members = [m for m in members if m and m not in ("null", "undefined", "None")]
            if not members:
                return None
            current = members[0]
        match = _GENERIC_RE.match(current)
        if match is None:
            break
        outer = re.split(r"\.|::", match.group(1))[-1]
        if outer not in _TRANSPARENT_WRAPPERS:
            current = match.group(1)
            break
        current = match.group(2).split(",")[0]
    name = re.split(r"\.|::", current.strip())[-1].strip()
    return name if _TYPE_NAME_RE.fullmatch(name) else None


def is_constant_name(name: str) -> bool:
    """True for ``UPPER_SNAKE`` style names conventionally used for constants."""
    return bool(_CONSTANT_NAME_RE.fullmatch(name))


def is_identifier_string(text: str) -> bool:
    """True when a string literal could plausibly be a table name."""
    return not text.startswith("__") and bool(_IDENTIFIER_STRING_RE.fullmatch(text))


def tidy_block(text: str) -> str:
    """Normalise the indentation of a multi-line literal for display.

    Continuation lines of a query written inside code carry the indentation of the
    surrounding code; remove what they have in common.
    """
    lines = [line.rstrip() for line in text.strip("\n").splitlines()]
    if len(lines) < 2:
        return text.strip()
    indents = [len(line) - len(line.lstrip()) for line in lines[1:] if line.strip()]
    common = min(indents, default=0)
    if common < 4:
        # Small offsets are the query's own alignment (``SELECT a`` / ``  FROM t``), not code indentation.
        common = 0
    first = lines[0].lstrip()
    return "\n".join([first, *(line[common:] for line in lines[1:])]).strip()


def make_sql_statement(
    text: str, line: int, end_line: int, *, dynamic: bool = False, used_at: int | None = None
) -> SqlStatement | None:
    """Analyse ``text``; returns ``None`` when it turns out not to reference anything."""
    analysis = analyze_sql(text)
    if analysis.is_empty and not analysis.functions and not dynamic:
        return None
    stored = tidy_block(text)
    if len(stored) > MAX_SQL_TEXT:
        stored = stored[:MAX_SQL_TEXT] + "\n-- … truncated"
    return SqlStatement(
        text=stored,
        line=line,
        end_line=end_line,
        operation=analysis.operation,
        reads=analysis.reads,
        writes=analysis.writes,
        write_ops=analysis.write_ops,
        routines=analysis.routines,
        functions=analysis.functions,
        dynamic=dynamic,
        used_at=used_at,
    )


class Source:
    """A source file's bytes plus decoding helpers."""

    __slots__ = ("data", "names", "path", "skip_ids")

    def __init__(self, path: str, data: bytes) -> None:
        self.path = path
        self.data = data
        #: Ids of nodes body visitors must not descend into (already accounted for).
        self.skip_ids: set[int] = set()
        #: Per-file name sets an extractor collects for its own hooks (e.g. router variables).
        self.names: dict[str, set[str]] = {}

    def text(self, node: Node) -> str:
        return self.data[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def slice(self, start: int, end: int) -> str:
        return self.data[start:end].decode("utf-8", "replace")

    @property
    def line_count(self) -> int:
        if not self.data:
            return 0
        return self.data.count(b"\n") + (0 if self.data.endswith(b"\n") else 1)


@dataclass(slots=True)
class StrVal:
    """Result of folding a string expression."""

    text: str
    #: Part of the value is only known at runtime.
    dynamic: bool = False
    #: At least one string literal contributed (distinguishes ``"a" + x`` from ``a + b``).
    literal: bool = True


class Scope:
    """Lexically nested table of string constants (module -> class -> nested class).

    Constants of a named (class) scope are also visible file-wide under their
    qualified key ``Class.NAME``, which is how ``Queries.FIND_ALL`` resolves.
    """

    __slots__ = ("defs", "name", "parent", "values")

    def __init__(self, parent: Scope | None = None, name: str | None = None) -> None:
        self.parent = parent
        self.name = name
        self.values: dict[str, StrVal] = {}
        #: Constants whose value is SQL, with their definition site.
        self.defs: dict[str, ConstantDef] = {}

    def lookup(self, name: str) -> StrVal | None:
        scope: Scope | None = self
        while scope is not None:
            value = scope.values.get(name)
            if value is not None:
                return value
            scope = scope.parent
        return None

    def sql_constant(self, name: str) -> ConstantDef | None:
        scope: Scope | None = self
        while scope is not None:
            definition = scope.defs.get(name)
            if definition is not None:
                return definition
            scope = scope.parent
        return None

    def define(self, name: str, value: StrVal, line: int, end_line: int) -> ConstantDef | None:
        """Register a constant; returns its IR definition when it is worth exporting."""
        self.values[name] = value
        root: Scope | None = None
        if self.name and self.parent is not None:
            root = self.parent
            while root.parent is not None:
                root = root.parent
            root.values[f"{self.name}.{name}"] = value
        if not value.literal:
            return None
        if looks_like_sql(value.text):
            definition = ConstantDef(name, value.text, line, end_line, value.dynamic)
            self.defs[name] = definition
            if root is not None:
                root.defs[f"{self.name}.{name}"] = definition
            return definition
        if not value.dynamic and is_identifier_string(value.text):
            return ConstantDef(name, value.text, line, end_line, False)
        return None


Lookup = Callable[[str], StrVal | None]


class Extractor(Protocol):
    """Anything that turns the bytes of one source file into a :class:`~parsers.ir.ModuleDef`."""

    language: ClassVar[Language]

    def parse(self, path: str, data: bytes) -> ModuleDef: ...


#: One parser per grammar per process (parsers are not shared between processes).
_PARSERS: dict[int, Parser] = {}


@dataclass(slots=True)
class Assignment:
    """A declaration-with-initialiser or assignment the body visitor should track."""

    target: str
    #: Initialiser / right-hand side; ``None`` for a builder created empty.
    value: Node | None
    #: ``target += value``
    augmented: bool = False
    #: ``target`` is a string builder / output stream / list of fragments rather than a plain string.
    builder: bool = False
    #: Initial fragments of a list-style builder (``parts = ["SELECT ...", "FROM t"]``).
    parts: list[Node] | None = None
    #: Text implied between fragments (list builders are joined, usually with a space).
    separator: str = ""


@dataclass(slots=True)
class Append:
    """``builder.append(a).append(b)`` / ``stream << a << b`` flattened to one record."""

    target: str
    arguments: list[Node]
    #: Ids of the call nodes that make up the chain, so they are not handled twice.
    chain: list[int] = field(default_factory=list)
    #: The node writes text somewhere that is not a query (``std::cerr << "..."``):
    #: suppress SQL detection below it instead of appending.
    quiet: bool = False


@dataclass(slots=True)
class _Accumulation:
    parts: list[StrVal]
    line: int
    end_line: int
    depth: int
    builder: bool = False
    conditional: bool = False
    inlined: bool = False
    separator: str = ""

    def value(self) -> StrVal:
        return StrVal(
            self.separator.join(part.text for part in self.parts),
            dynamic=self.conditional or any(part.dynamic for part in self.parts),
            literal=any(part.literal for part in self.parts),
        )


class TreeSitterExtractor(ABC):
    """Base class for a language extractor. Instances are stateless and reusable."""

    language: ClassVar[Language]

    #: Node types that are call expressions (handled by :meth:`call_site`).
    CALL_TYPES: ClassVar[frozenset[str]] = frozenset()
    #: Node types that may be the root of a string-valued expression.
    STRING_TYPES: ClassVar[frozenset[str]] = frozenset()
    #: Node types that may be tracked assignments (handled by :meth:`assignment`).
    ASSIGN_TYPES: ClassVar[frozenset[str]] = frozenset()
    #: Node types that declare typed locals (handled by :meth:`local_types`).
    DECL_TYPES: ClassVar[frozenset[str]] = frozenset()
    #: Node types that each add one to the complexity estimate.
    BRANCH_TYPES: ClassVar[frozenset[str]] = frozenset()
    #: Node types that open a named nested scope extracted separately.
    NESTED_TYPES: ClassVar[frozenset[str]] = frozenset()
    #: Node types that may reference a named constant (handled by :meth:`constant_ref`).
    REF_TYPES: ClassVar[frozenset[str]] = frozenset()
    #: Node types skipped entirely inside function bodies (comments, type annotations).
    SKIP_TYPES: ClassVar[frozenset[str]] = frozenset({"comment", "line_comment", "block_comment"})
    #: Node types that bind local names (handled by :meth:`bound_names`).
    BINDING_TYPES: ClassVar[frozenset[str]] = frozenset()
    #: Node types that may append to a string builder (handled by :meth:`append`).
    APPEND_TYPES: ClassVar[frozenset[str]] = frozenset()

    # ----------------------------------------------------------------- grammar

    @abstractmethod
    def grammar(self, path: str) -> TsLanguage:
        """The Tree-sitter grammar to use for ``path`` (must return a cached object)."""

    @abstractmethod
    def extract_module(self, src: Source, root: Node, module: ModuleDef) -> None:
        """Populate ``module`` from the parsed tree."""

    def parse(self, path: str, data: bytes) -> ModuleDef:
        """Parse ``data`` and extract its semantic scopes."""
        grammar = self.grammar(path)
        parser = _PARSERS.get(id(grammar))
        if parser is None:
            parser = _PARSERS[id(grammar)] = Parser(grammar)
        tree = parser.parse(data)
        src = Source(path, data)
        module = ModuleDef(path=path, language=self.language, line_count=src.line_count)
        root = tree.root_node
        if root.has_error:
            module.error_count = count_errors(root)
        self.extract_module(src, root, module)
        return module

    # ------------------------------------------------------- hooks (per language)

    def call_site(self, src: Source, node: Node) -> CallSite | None:
        """Describe the call expression ``node``."""
        return None

    def literal_value(self, src: Source, node: Node, lookup: Lookup) -> StrVal | None:
        """Value of a string literal / template node, or ``None`` if it is not one."""
        return None

    def concat_operands(self, node: Node) -> list[Node] | None:
        """Operands of a string concatenation, in order, or ``None``."""
        return None

    def unwrap(self, node: Node) -> Node:
        """Strip parentheses, casts and similar value-preserving wrappers."""
        return node

    def special_string(self, src: Source, node: Node, lookup: Lookup) -> StrVal | None:
        """Language idioms that produce strings (``String.format``, ``"".join`` ...)."""
        return None

    def assignment(self, src: Source, node: Node) -> Assignment | None:
        return None

    def append(self, src: Source, node: Node) -> Append | None:
        return None

    def local_types(self, src: Source, node: Node) -> Iterable[tuple[str, str]]:
        return ()

    def constant_ref(self, src: Source, node: Node) -> tuple[str | None, str] | None:
        """``(qualifier, name)`` when ``node`` reads a named constant."""
        return None

    def children_to_visit(self, node: Node) -> list[Node]:
        """Children the body visitor should descend into."""
        return node.named_children

    def bound_names(self, src: Source, node: Node) -> Iterable[str]:
        """Local names introduced by ``node`` (callback parameters, destructuring, loop variables)."""
        return ()

    def is_nested_scope(self, src: Source, node: Node) -> bool:
        """Whether a node of a ``NESTED_TYPES`` type really opens a separately extracted scope."""
        return True

    def extra_calls(self, src: Source, node: Node) -> Iterable[CallSite]:
        """Additional call sites implied by a call node, e.g. functions passed as callbacks.

        Callback references to names declared inside the function itself (parameters,
        locals) are discarded by the visitor: only references that can name a function
        defined elsewhere are kept.
        """
        return ()

    # ---------------------------------------------------------- string folding

    def eval_string(self, src: Source, node: Node, lookup: Lookup) -> StrVal | None:
        """Fold a string-valued expression to text.

        Non-constant parts are rendered as ``${name}`` placeholders (the SQL
        analyser treats those as parameters). Returns ``None`` when ``node`` is not
        a string expression at all.
        """
        node = self.unwrap(node)
        literal = self.literal_value(src, node, lookup)
        if literal is not None:
            return literal
        operands = self.concat_operands(node)
        if operands is not None:
            return self.eval_concat(src, operands, lookup)
        if node.type in self.REF_TYPES:
            reference = self.constant_ref(src, node)
            if reference is not None:
                qualifier, name = reference
                found = lookup(name if qualifier is None else f"{qualifier}.{name}")
                if found is not None:
                    return found
        return self.special_string(src, node, lookup)

    def eval_concat(self, src: Source, operands: list[Node], lookup: Lookup) -> StrVal | None:
        if len(operands) > _MAX_CONCAT_OPERANDS:
            return None
        parts: list[str] = []
        dynamic = False
        literal = False
        for operand in operands:
            value = self.eval_string(src, operand, lookup)
            if value is None:
                parts.append(self.placeholder(src, operand))
                dynamic = True
            else:
                parts.append(value.text)
                dynamic = dynamic or value.dynamic
                literal = literal or value.literal
        if not literal:
            return None
        return StrVal("".join(parts), dynamic=dynamic, literal=True)

    def placeholder(self, src: Source, node: Node) -> str:
        """Placeholder text for a runtime-computed part of a string."""
        if node.end_byte - node.start_byte <= 48:
            text = src.text(node)
            if _SIMPLE_NAME_RE.fullmatch(text):
                return "${" + text + "}"
        return "${?}"

    # ------------------------------------------------------------------ helpers

    def visit_body(
        self,
        src: Source,
        fn: FunctionDef,
        roots: Iterable[Node],
        scope: Scope,
    ) -> list[Node]:
        """Analyse a function body; returns nested named scopes found inside it."""
        visitor = BodyVisitor(self, src, fn, scope)
        for root in roots:
            visitor.visit(root)
        visitor.finish()
        return visitor.nested


class BodyVisitor:
    """Collects everything of interest from one function body in a single pass."""

    def __init__(self, extractor: TreeSitterExtractor, src: Source, fn: FunctionDef, scope: Scope) -> None:
        self.ext = extractor
        self.src = src
        self.fn = fn
        self.scope = scope
        self.nested: list[Node] = []
        self._tracked: dict[str, _Accumulation] = {}
        #: Node ids below which string handling is suppressed (already accounted for).
        self._consumed: set[int] = set()
        #: Call nodes that are inner links of an append chain already processed.
        self._chained: set[int] = set()
        self._seen_sql: set[tuple[str, int]] = set()
        self._seen_refs: set[str] = set()
        self._seen_constants: set[tuple[str | None, str]] = set()
        #: Names bound inside the function; a callback reference to one of them cannot
        #: be a reference to a function defined elsewhere.
        self._declared: set[str] = {param.name.lstrip("*.") for param in fn.params}
        self._callbacks: list[CallSite] = []

    # ----------------------------------------------------------------- lookup

    def lookup(self, name: str) -> StrVal | None:
        """Resolve an identifier to a string value: tracked locals, then constants."""
        tracked = self._tracked.get(name)
        if tracked is not None:
            if not tracked.parts:
                return None
            tracked.inlined = True
            return tracked.value()
        return self.scope.lookup(name)

    # ------------------------------------------------------------------ visit

    def visit(self, root: Node) -> None:
        ext = self.ext
        src = self.src
        fn = self.fn
        # (node, string handling suppressed, branch depth)
        stack: list[tuple[Node, bool, int]] = [(root, False, 0)]
        while stack:
            node, quiet, depth = stack.pop()
            kind = node.type
            # Anonymous nodes are tokens; their type is their text and may collide with node types.
            if not node.is_named or kind in ext.SKIP_TYPES or node.id in src.skip_ids:
                continue
            if kind in ext.NESTED_TYPES and node.id != root.id and ext.is_nested_scope(src, node):
                self.nested.append(node)
                continue
            if node.id in self._consumed:
                quiet = True
            if kind in ext.BRANCH_TYPES:
                fn.complexity += 1
                depth += 1
            if kind in ext.BINDING_TYPES:
                self._declared.update(ext.bound_names(src, node))

            if kind in ext.CALL_TYPES:
                call = ext.call_site(src, node)
                if call is not None:
                    fn.calls.append(call)
                    if call.name in LOG_CALL_NAMES:
                        quiet = True
                self._callbacks.extend(ext.extra_calls(src, node))
            if kind in ext.APPEND_TYPES and node.id not in self._chained:
                appended = ext.append(src, node)
                if appended is not None:
                    self._chained.update(appended.chain)
                    if appended.quiet:
                        quiet = True
                    else:
                        self._on_append(appended, node, depth)
            if kind in ext.DECL_TYPES:
                for name, type_name in ext.local_types(src, node):
                    fn.local_types.setdefault(name, type_name)
            if kind in ext.ASSIGN_TYPES:
                assigned = ext.assignment(src, node)
                if assigned is not None:
                    self._declared.add(assigned.target)
                    if not quiet:
                        self._on_assignment(assigned, node, depth)
            if kind in ext.REF_TYPES:
                self._on_reference(node, quiet)
            if not quiet and kind in ext.STRING_TYPES and node.id not in self._consumed:
                value = ext.eval_string(src, node, self.lookup)
                if value is not None and value.literal:
                    self._emit(value.text, line_of(node), end_line_of(node), value.dynamic)
                    quiet = True

            children = ext.children_to_visit(node)
            for index in range(len(children) - 1, -1, -1):
                stack.append((children[index], quiet, depth))

    def finish(self) -> None:
        for accumulation in self._tracked.values():
            self._flush(accumulation)
        self._tracked.clear()
        seen: set[tuple[str | None, str]] = set()
        imported = self.src.names.get("imports", ())
        for callback in self._callbacks:
            key = (callback.receiver, callback.name)
            if key in seen:
                continue
            seen.add(key)
            name = callback.name
            if is_constant_name(name):
                continue
            if (
                callback.receiver is None
                and name not in imported
                and (name in self._declared or self.scope.lookup(name) is not None)
            ):
                continue  # a local value or a constant, not a function defined elsewhere
            self.fn.calls.append(callback)
        self._callbacks.clear()

    # --------------------------------------------------------------- handlers

    def _on_assignment(self, assigned: Assignment, node: Node, depth: int) -> None:
        ext = self.ext
        target = assigned.target
        value = assigned.value
        if assigned.builder and (value is None or assigned.parts is not None):
            accumulation = _Accumulation(
                [], line_of(node), end_line_of(node), depth, builder=True, separator=assigned.separator
            )
            self._replace(target, accumulation)
            for part in assigned.parts or ():
                self._extend(accumulation, ext.eval_string(self.src, part, self.lookup), part, node, depth)
            return
        if value is None:
            return

        augmented = assigned.augmented
        operands: list[Node] | None = None
        if not augmented and target in self._tracked:
            # ``sql = sql + " WHERE ..."`` is an append in disguise.
            candidates = ext.concat_operands(ext.unwrap(value))
            if candidates and self.src.text(candidates[0]) == target:
                augmented = True
                operands = candidates[1:]

        if augmented:
            existing = self._tracked.get(target)
            if existing is None:
                return  # appending to something we never saw initialised
            accumulation = existing
            folded = (
                ext.eval_concat(self.src, operands, self.lookup)
                if operands is not None
                else ext.eval_string(self.src, value, self.lookup)
            )
            self._extend(accumulation, folded, value, node, depth)
            return

        folded = ext.eval_string(self.src, value, self.lookup)
        if folded is None or not folded.literal:
            if assigned.builder:
                self._replace(
                    target,
                    _Accumulation(
                        [],
                        line_of(node),
                        end_line_of(node),
                        depth,
                        builder=True,
                        separator=assigned.separator,
                    ),
                )
            elif target in self._tracked:
                # Re-assigned to something we cannot fold: the old value is complete.
                self._flush(self._tracked.pop(target))
            return
        self._consumed.add(value.id)
        self._replace(
            target,
            _Accumulation(
                [folded],
                line_of(node),
                end_line_of(node),
                depth,
                builder=assigned.builder,
                separator=assigned.separator,
            ),
        )

    def _on_append(self, appended: Append, node: Node, depth: int) -> None:
        accumulation = self._tracked.get(appended.target)
        if accumulation is None or not accumulation.builder:
            return
        for argument in appended.arguments:
            folded = self.ext.eval_string(self.src, argument, self.lookup)
            self._extend(accumulation, folded, argument, node, depth)

    def _extend(
        self, accumulation: _Accumulation, folded: StrVal | None, value: Node, node: Node, depth: int
    ) -> None:
        if folded is None:
            # Not a string we can fold: keep visiting it, there may be queries further down.
            folded = StrVal(self.ext.placeholder(self.src, value), dynamic=True, literal=False)
        else:
            self._consumed.add(value.id)
        accumulation.parts.append(folded)
        accumulation.end_line = max(accumulation.end_line, end_line_of(node))
        if depth > accumulation.depth:
            accumulation.conditional = True

    def _replace(self, target: str, accumulation: _Accumulation) -> None:
        previous = self._tracked.get(target)
        if previous is not None:
            # The variable is being reused for a new value (``sql = "SELECT ..."`` and
            # later ``sql = "UPDATE ..."``): what it held so far is a complete statement.
            self._flush(previous)
        self._tracked[target] = accumulation

    def _on_reference(self, node: Node, quiet: bool) -> None:
        reference = self.ext.constant_ref(self.src, node)
        if reference is None:
            return
        qualifier, name = reference
        local = qualifier is None
        definition = self.scope.sql_constant(name if local else f"{qualifier}.{name}")
        if local or definition is not None:
            if local and name in self._tracked:
                return
            if definition is not None:
                # A SQL constant of this file used directly: the query belongs to this function.
                if not quiet:
                    self._add_sql(
                        make_sql_statement(
                            definition.value,
                            definition.line,
                            definition.end_line,
                            dynamic=definition.dynamic,
                            used_at=line_of(node),
                        )
                    )
                return
            if self.scope.lookup(name) is not None or not is_constant_name(name):
                return
        elif not is_constant_name(name):
            return
        key = (qualifier, name)
        if key not in self._seen_constants:
            self._seen_constants.add(key)
            self.fn.constant_refs.append(ConstantRef(qualifier, name, line_of(node)))

    # ------------------------------------------------------------------ output

    def _flush(self, accumulation: _Accumulation) -> None:
        if accumulation.inlined or not accumulation.parts:
            return
        value = accumulation.value()
        if value.literal:
            self._emit(value.text, accumulation.line, accumulation.end_line, value.dynamic)

    def _emit(self, text: str, line: int, end_line: int, dynamic: bool) -> None:
        if looks_like_sql(text):
            self._add_sql(make_sql_statement(text, line, end_line, dynamic=dynamic))
        elif (
            not dynamic
            and text not in self._seen_refs
            and len(self.fn.string_refs) < _MAX_STRING_REFS
            and is_identifier_string(text)
        ):
            self._seen_refs.add(text)
            self.fn.string_refs.append((text, line))

    def _add_sql(self, statement: SqlStatement | None) -> None:
        if statement is None:
            return
        key = (statement.text, statement.line)
        if key in self._seen_sql:
            return
        self._seen_sql.add(key)
        self.fn.sql.append(statement)


# --------------------------------------------------------------------- tree helpers


def count_errors(root: Node, limit: int = 1000) -> int:
    """Number of ERROR / MISSING nodes below ``root`` (capped at ``limit``)."""
    total = 0
    stack = [root]
    while stack and total < limit:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            total += 1
            continue
        if node.has_error:
            stack.extend(node.children)
    return total


def walk(root: Node, *, stop_at: frozenset[str] = frozenset()) -> Iterator[Node]:
    """Pre-order traversal of ``root``'s descendants; does not descend into ``stop_at`` types."""
    stack = list(reversed(root.children))
    while stack:
        node = stack.pop()
        yield node
        if node.type not in stop_at:
            stack.extend(reversed(node.children))


def flatten_binary(node: Node, operator: str, node_type: str = "binary_expression") -> list[Node] | None:
    """Operands of a left- or right-nested chain of ``operator`` in source order."""
    if node.type != node_type:
        return None
    operator_node = node.child_by_field_name("operator")
    if operator_node is None or operator_node.type != operator:
        return None
    operands: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == node_type:
            op = current.child_by_field_name("operator")
            left = current.child_by_field_name("left")
            right = current.child_by_field_name("right")
            if op is not None and op.type == operator and left is not None and right is not None:
                stack.append(right)
                stack.append(left)
                continue
        operands.append(current)
    return operands


def preceding_comment(node: Node, comment_types: frozenset[str]) -> Node | None:
    """The comment node immediately before ``node`` (no blank line in between)."""
    previous = node.prev_sibling
    if previous is None or previous.type not in comment_types:
        return None
    if int(node.start_point[0]) - int(previous.end_point[0]) > 1:
        return None
    return previous


def leading_comment(
    src: Source, node: Node, comment_types: frozenset[str] = frozenset({"comment"})
) -> str | None:
    """Documentation written directly above ``node``.

    Accepts one block comment or a run of consecutive line comments, with no blank
    line between the comment and the declaration. A comment trailing the previous
    statement on its own line of code is not documentation for ``node``.
    """
    collected: list[Node] = []
    current = node
    while len(collected) < 40:
        previous = current.prev_sibling
        if previous is None or previous.type not in comment_types:
            break
        if int(current.start_point[0]) - int(previous.end_point[0]) > 1:
            break
        before = previous.prev_sibling
        if before is not None and int(before.end_point[0]) == int(previous.start_point[0]):
            break  # trailing comment of the statement before
        collected.append(previous)
        if src.data[previous.start_byte : previous.start_byte + 2] == b"/*":
            break
        current = previous
    if not collected:
        return None
    collected.reverse()
    return clean_doc_comment("\n".join(src.text(comment) for comment in collected))


def clean_doc_comment(text: str, limit: int = 1200) -> str | None:
    """Strip comment markers from a doc comment."""
    body = text.strip()
    if body.startswith("/*"):
        body = body[2:-2] if body.endswith("*/") else body[2:]
        lines = [re.sub(r"^\s*\*+ ?", "", raw) for raw in body.lstrip("*!").splitlines()]
    else:
        lines = [re.sub(r"^\s*(?:///?!?|#+!?|--+) ?", "", raw) for raw in body.splitlines()]
    cleaned = "\n".join(lines).strip()
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


_SIMPLE_ESCAPES: Final = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "`": "`",
    "0": "\0",
    "\n": "",
}
_ESCAPE_RE: Final = re.compile(r"\\(.)", re.DOTALL)


def unescape(text: str) -> str:
    """Decode the common backslash escapes shared by C-family string literals."""
    if "\\" not in text:
        return text
    return _ESCAPE_RE.sub(lambda match: _SIMPLE_ESCAPES.get(match.group(1), match.group(0)), text)
