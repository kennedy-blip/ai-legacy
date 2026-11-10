"""Language-neutral intermediate representation produced by the Tree-sitter extractors.

Source files are chunked into *semantic scopes* - module, class, function/method
and database query - never into arbitrary line windows. Every scope records its
exact line span so downstream consumers (graph nodes, embeddings, citations) refer
to real program structure.

The IR is deliberately free of graph or storage concerns and contains only
plain, picklable dataclasses so that files can be parsed in worker processes.
Cross-file concerns (which definition a call refers to) are resolved later by
:mod:`ingestion.resolver`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Language(StrEnum):
    JAVA = "java"
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    CPP = "cpp"
    SQL = "sql"


class FunctionKind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    #: SQL stored procedure / function.
    PROCEDURE = "procedure"
    #: SQL trigger.
    TRIGGER = "trigger"
    #: Synthetic scope for a handler registered by a call rather than declared by name:
    #: ``app.get("/x", (req, res) => ...)``, ``cron.schedule("0 2 * * *", () => ...)``.
    ROUTE = "route"
    #: Synthetic scope for statements executed at module import / script level.
    SCRIPT = "script"


class ClassKind(StrEnum):
    CLASS = "class"
    INTERFACE = "interface"
    ENUM = "enum"
    STRUCT = "struct"
    RECORD = "record"


#: Qualified name of the synthetic per-file scope holding module-level statements.
MODULE_SCOPE = "<module>"


@dataclass(slots=True)
class Param:
    name: str
    type: str | None = None


@dataclass(slots=True)
class CallSite:
    """A call expression found inside a function body.

    ``receiver`` is the normalised source text of the expression the method is
    invoked on: ``None`` for a bare call ``foo()``, ``"this"``/``"self"``/``"super"``
    for the current object, a dotted path such as ``"this.dao"`` or ``"Flags"`` for
    simple receivers, and ``"<expr>"`` when the receiver is itself a complex
    expression (for example the result of another call).
    """

    name: str
    line: int
    receiver: str | None = None
    arg_count: int | None = None
    #: ``call`` | ``new`` (constructor) | ``reference`` (method reference / callback) | ``super``
    kind: str = "call"


@dataclass(slots=True)
class SqlStatement:
    """A database query scope: SQL text plus the tables and routines it touches."""

    text: str
    line: int
    end_line: int
    operation: str
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    #: ``(table, operation)`` pairs, e.g. ``("tbl_ledger_audit", "INSERT")``.
    write_ops: tuple[tuple[str, str], ...] = ()
    #: Routines invoked explicitly (``CALL x``, ``EXEC x``, ``PERFORM x()``).
    routines: tuple[str, ...] = ()
    #: Identifiers applied like functions; matched against known stored functions later.
    functions: tuple[str, ...] = ()
    #: True when part of the text is computed at runtime (concatenation, interpolation).
    dynamic: bool = False
    #: Line where a named SQL constant is *used*, when ``line`` is where it is defined.
    used_at: int | None = None


@dataclass(slots=True)
class EntryPoint:
    """Marks a function that is invoked from outside the code base."""

    #: ``http`` | ``scheduled`` | ``main`` | ``message`` | ``task`` | ``cli``
    kind: str
    method: str | None = None
    route: str | None = None
    #: Cron expression, queue/topic name, command name ...
    detail: str | None = None


@dataclass(slots=True)
class ConstantRef:
    """A use of a (possibly cross-class) named constant, e.g. ``Queries.FIND_FLAGGED``."""

    qualifier: str | None
    name: str
    line: int


@dataclass(slots=True)
class ConstantDef:
    """A string constant defined at class or module level."""

    name: str
    value: str
    line: int
    end_line: int
    dynamic: bool = False


@dataclass(slots=True)
class FunctionDef:
    name: str
    qualified_name: str
    kind: FunctionKind
    start_line: int
    end_line: int
    signature: str = ""
    params: list[Param] = field(default_factory=list)
    return_type: str | None = None
    docstring: str | None = None
    decorators: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    is_abstract: bool = False
    #: Qualified name of the class this function belongs to, if any.
    owner: str | None = None
    #: Qualified name of the enclosing function for nested definitions.
    parent_function: str | None = None
    calls: list[CallSite] = field(default_factory=list)
    sql: list[SqlStatement] = field(default_factory=list)
    constant_refs: list[ConstantRef] = field(default_factory=list)
    #: Short identifier-like string literals ``(text, line)``; candidates for table-name references.
    string_refs: list[tuple[str, int]] = field(default_factory=list)
    #: Declared or inferred types of locals and parameters: variable name -> type name.
    local_types: dict[str, str] = field(default_factory=dict)
    entry_point: EntryPoint | None = None
    #: 1 + number of branching constructs (a cheap cyclomatic-complexity estimate).
    complexity: int = 1
    #: Line of the declared name - the citation anchor. ``start_line`` may be earlier
    #: because the scope's extent includes leading annotations / decorators.
    name_line: int = 0

    @property
    def anchor_line(self) -> int:
        return self.name_line or self.start_line


@dataclass(slots=True)
class ClassDef:
    name: str
    qualified_name: str
    kind: ClassKind
    start_line: int
    end_line: int
    bases: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    docstring: str | None = None
    decorators: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    #: Field / attribute name -> declared or inferred type name.
    fields: dict[str, str] = field(default_factory=dict)
    constants: dict[str, ConstantDef] = field(default_factory=dict)
    methods: list[FunctionDef] = field(default_factory=list)
    #: Qualified name of the enclosing class for nested classes.
    owner: str | None = None
    #: Database table this class is mapped to by an ORM annotation, if declared.
    table: str | None = None
    #: Line of the declared name - the citation anchor (see :class:`FunctionDef`).
    name_line: int = 0

    @property
    def anchor_line(self) -> int:
        return self.name_line or self.start_line


@dataclass(slots=True)
class ImportDef:
    """An import binding.

    ``module`` is the module/package path as written. ``name`` is the imported
    symbol (``None`` when the module itself is bound, ``"*"`` for wildcard or
    namespace imports). ``alias`` is the local name the binding is visible under.
    """

    module: str
    name: str | None
    alias: str | None
    line: int
    is_static: bool = False
    #: Number of leading dots of a Python relative import.
    level: int = 0


@dataclass(slots=True)
class ColumnDef:
    name: str
    type: str


@dataclass(slots=True)
class TableDef:
    name: str
    #: ``table`` | ``view``
    kind: str
    start_line: int
    end_line: int
    columns: list[ColumnDef] = field(default_factory=list)
    ddl: str = ""
    schema: str | None = None
    #: Tables a view selects from.
    reads: tuple[str, ...] = ()


@dataclass(slots=True)
class TriggerDef:
    """Binds a table event to the function scope that represents the trigger."""

    name: str
    table: str
    events: tuple[str, ...]
    timing: str
    start_line: int
    end_line: int


@dataclass(slots=True)
class ModuleDef:
    """Everything extracted from one source file."""

    path: str
    language: Language
    package: str | None = None
    docstring: str | None = None
    imports: list[ImportDef] = field(default_factory=list)
    #: All classes in the file, nested ones included (flattened).
    classes: list[ClassDef] = field(default_factory=list)
    #: Functions not owned by a class: module-level, nested, routes and the module scope.
    functions: list[FunctionDef] = field(default_factory=list)
    constants: dict[str, ConstantDef] = field(default_factory=dict)
    tables: list[TableDef] = field(default_factory=list)
    triggers: list[TriggerDef] = field(default_factory=list)
    line_count: int = 0
    #: Number of Tree-sitter ERROR / MISSING nodes; non-zero means a partial parse.
    error_count: int = 0
    #: Qualified name of the function or class that is the module's default export
    #: (``module.exports = X`` / ``export default X``).
    default_export: str | None = None

    def all_functions(self) -> list[FunctionDef]:
        """Every function scope in the file, including class methods."""
        functions = list(self.functions)
        for cls in self.classes:
            functions.extend(cls.methods)
        return functions
