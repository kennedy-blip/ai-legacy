"""Turns parsed files plus resolved symbols into graph nodes and relationships.

The builder is pure: it performs no I/O and returns plain node / edge lists, so
the same output can be written to any :class:`graph_engine.store.GraphStore`.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Final

from graph_engine.models import GraphEdge, GraphNode
from graph_engine.schema import NodeLabel, RelType, make_id
from ingestion.discovery import ParsedFile, is_test_path
from ingestion.resolver import DEFAULT_MIN_CONFIDENCE, ClassInfo, FunctionInfo, Resolution, SymbolTable
from parsers.ir import MODULE_SCOPE, FunctionDef, FunctionKind, ModuleDef, SqlStatement, TableDef

_MAX_DOC: Final = 2000
_MAX_SQL: Final = 6000
_MAX_DDL: Final = 6000
_MAX_LIST: Final = 40
#: Confidence of a table link inferred from a bare string literal naming the table.
NAME_REFERENCE_CONFIDENCE: Final = 0.4
#: Confidence of table links extracted from SQL assembled at runtime.
DYNAMIC_SQL_CONFIDENCE: Final = 0.8

_ACCESSOR_RE: Final = re.compile(r"^(?:get|set|is|has)[A-Z_]\w*$")
_BOILERPLATE_NAMES: Final = frozenset(
    {
        "toString", "equals", "hashCode", "compareTo", "clone", "finalize", "__repr__", "__str__", "__eq__",
        "__hash__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__len__", "__iter__", "__enter__",
        "__exit__", "__bool__", "__contains__", "__getitem__", "__setitem__", "__del__", "valueOf", "toJSON",
    }
)  # fmt: skip
_NEUTRAL_DECORATORS: Final = frozenset(
    {"staticmethod", "classmethod", "property", "Override", "override", "abstractmethod", "SuppressWarnings",
     "Deprecated", "final", "cached_property", "lru_cache", "cache", "wraps", "contextmanager"}
)  # fmt: skip
_TEST_DECORATORS: Final = frozenset(
    {"Test", "ParameterizedTest", "Before", "After", "BeforeEach", "AfterEach", "BeforeClass", "AfterClass",
     "BeforeAll", "AfterAll", "fixture", "pytest.fixture", "pytest.mark.parametrize"}
)  # fmt: skip
_TEST_NAME_RE: Final = re.compile(r"^(?:test[A-Z_]\w*|test|setUp|tearDown|setUpClass|tearDownClass)$")


@dataclass(slots=True)
class BuiltGraph:
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], GraphEdge] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    def add_node(self, node: GraphNode) -> GraphNode:
        self.nodes[node.id] = node
        return node

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges[edge.key] = edge


@dataclass(slots=True)
class _TableAccess:
    operations: dict[str, None] = field(default_factory=dict)
    lines: dict[int, None] = field(default_factory=dict)
    confidence: float = 0.0
    via: str = "query"


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def query_label(statement: SqlStatement) -> str:
    """A short human-readable name for a query scope: ``UPDATE tbl_account``."""
    targets = list(dict.fromkeys([*statement.writes, *statement.reads]))[:2] or list(statement.routines[:1])
    suffix = " …" if len(set(statement.writes) | set(statement.reads)) > 2 else ""
    return f"{statement.operation} {', '.join(targets)}{suffix}".strip() or "SQL"


class GraphBuilder:
    """Builds the static part of the knowledge graph for one repository."""

    def __init__(self, repository_id: str, symbols: SymbolTable, resolution: Resolution) -> None:
        self.repository_id = repository_id
        self.symbols = symbols
        self.resolution = resolution
        self.graph = BuiltGraph()
        self._tables: dict[str, GraphNode] = {}
        self._access: dict[tuple[str, str, RelType], _TableAccess] = defaultdict(_TableAccess)
        self._query_counts: Counter[str] = Counter()
        self._functions: dict[str, FunctionInfo] = {function.id: function for function in symbols.functions}

    # ------------------------------------------------------------------ helpers

    def _node(self, node_id: str, label: NodeLabel, properties: dict[str, Any]) -> GraphNode:
        return self.graph.add_node(GraphNode(node_id, label, self.repository_id, properties))

    def _edge(self, source: str, target: str, rel: RelType, properties: dict[str, Any] | None = None) -> None:
        self.graph.add_edge(GraphEdge(source, target, rel, self.repository_id, properties or {}))

    def table_id(self, name: str) -> str:
        return make_id(NodeLabel.DATABASE_TABLE, self.repository_id, name.lower())

    def _table(self, name: str) -> GraphNode:
        """The node of table ``name`` (unqualified, case-insensitive), created on first reference."""
        key = name.lower()
        found = self._tables.get(key)
        if found is None:
            found = self._tables[key] = self._node(
                self.table_id(key),
                NodeLabel.DATABASE_TABLE,
                {"name": key, "kind": "table", "declared": False},
            )
        return found

    # --------------------------------------------------------------------- build

    def build(self, parsed: list[ParsedFile]) -> BuiltGraph:
        repository = self.repository_id
        languages: Counter[str] = Counter()
        lines_by_language: Counter[str] = Counter()
        for item in parsed:
            module = item.module
            file_id = make_id(NodeLabel.FILE, repository, item.path)
            properties: dict[str, Any] = {
                "name": posixpath.basename(item.path),
                "path": item.path,
                "directory": posixpath.dirname(item.path),
                "size_bytes": item.size,
                "sha1": item.sha1,
                "is_test": is_test_path(item.path),
            }
            if module is None:
                properties["parse_error"] = item.error or "not parsed"
                properties["line_count"] = 0
            else:
                language = module.language.value
                languages[language] += 1
                lines_by_language[language] += module.line_count
                properties.update(
                    {
                        "language": language,
                        "line_count": module.line_count,
                        "start_line": 1,
                        "end_line": max(module.line_count, 1),
                        "package": module.package,
                        "docstring": _clip(module.docstring, _MAX_DOC),
                        "syntax_errors": module.error_count,
                        "imports": [imported.module for imported in module.imports][:_MAX_LIST],
                    }
                )
            self._node(file_id, NodeLabel.FILE, properties)
            self._edge(repository_node_id(repository), file_id, RelType.CONTAINS)

        # Tables declared by DDL come first so later references attach to them.
        for module_info in self.symbols.modules.values():
            for table in module_info.definition.tables:
                self._declare_table(module_info.definition, module_info.file_id, table)

        for cls in self.symbols.classes:
            self._class_node(cls)
        for function in self.symbols.functions:
            self._function_node(function)
        for module_info in self.symbols.modules.values():
            self._triggers(module_info.definition)
            for table in module_info.definition.tables:
                for source in table.reads:  # a view depends on its base tables
                    self._edge(
                        self.table_id(table.name),
                        self._table(source).id,
                        RelType.READS_FROM_DB,
                        {"operations": ["SELECT"], "via": "view", "confidence": 1.0},
                    )

        self._calls_and_inheritance()
        self._constant_queries()
        self._name_references()
        self._flush_table_access()
        self._flags()

        self.graph.stats = {
            "files": len(parsed),
            "files_parsed": sum(1 for item in parsed if item.module is not None),
            "files_failed": sum(1 for item in parsed if item.module is None),
            "files_with_syntax_errors": sum(1 for item in parsed if item.module and item.module.error_count),
            "languages": dict(languages),
            "lines_by_language": dict(lines_by_language),
            "lines": sum(lines_by_language.values()),
            "classes": len(self.symbols.classes),
            "functions": len(self.symbols.functions),
            "queries": sum(self._query_counts.values()),
            "tables": len(self._tables),
            "calls": len(self.resolution.calls),
            "calls_low_confidence": sum(
                1 for call in self.resolution.calls.values() if call.confidence < DEFAULT_MIN_CONFIDENCE
            ),
        }
        return self.graph

    # --------------------------------------------------------------------- nodes

    def _declare_table(self, module: ModuleDef, file_id: str, table: TableDef) -> None:
        key = table.name.lower()
        existing = self._tables.get(key)
        if existing is not None and existing.get("declared") and existing.get("kind") == "table":
            return  # first CREATE TABLE wins; later ALTER-style redefinitions do not move it
        node = self._node(
            self.table_id(key),
            NodeLabel.DATABASE_TABLE,
            {
                "name": key,
                "display_name": table.name,
                "schema": table.schema,
                "kind": table.kind,
                "declared": True,
                "path": module.path,
                "start_line": table.start_line,
                "end_line": table.end_line,
                "line": table.start_line,
                "columns": [f"{column.name} {column.type}".strip() for column in table.columns][:200],
                "column_count": len(table.columns),
                "ddl": _clip(table.ddl, _MAX_DDL),
                "language": "sql",
            },
        )
        self._tables[key] = node
        self._edge(file_id, node.id, RelType.CONTAINS)

    def _class_node(self, cls: ClassInfo) -> None:
        definition = cls.definition
        module = cls.module.definition
        self._node(
            cls.id,
            NodeLabel.CLASS,
            {
                "name": definition.name,
                "qualified_name": definition.qualified_name,
                "path": module.path,
                "language": module.language.value,
                "kind": definition.kind.value,
                "start_line": definition.start_line,
                "end_line": definition.end_line,
                "line": definition.anchor_line,
                "docstring": _clip(definition.docstring, _MAX_DOC),
                "bases": definition.bases[:_MAX_LIST],
                "interfaces": definition.interfaces[:_MAX_LIST],
                "decorators": definition.decorators[:_MAX_LIST],
                "modifiers": definition.modifiers[:_MAX_LIST],
                "fields": [f"{name}: {kind}" for name, kind in definition.fields.items()][:_MAX_LIST],
                "table": definition.table.lower() if definition.table else None,
                "outer_class": definition.owner,
                "file_id": cls.module.file_id,
                "method_count": sum(len(methods) for methods in cls.methods.values()),
                "is_test": is_test_path(module.path),
            },
        )
        self._edge(cls.module.file_id, cls.id, RelType.CONTAINS)

    def _function_node(self, function: FunctionInfo) -> None:
        definition = function.definition
        module = function.module.definition
        entry = definition.entry_point
        cls = function.cls
        name = definition.name
        if definition.kind is FunctionKind.SCRIPT:
            name = f"{posixpath.basename(module.path)} (module scope)"
        properties: dict[str, Any] = {
            "name": name,
            "qualified_name": definition.qualified_name
            if definition.qualified_name != MODULE_SCOPE
            else name,
            "path": module.path,
            "language": module.language.value,
            "kind": definition.kind.value,
            "start_line": definition.start_line,
            "end_line": definition.end_line,
            "line": definition.anchor_line,
            "loc": definition.end_line - definition.start_line + 1,
            "signature": _clip(definition.signature, 600),
            "docstring": _clip(definition.docstring, _MAX_DOC),
            "decorators": definition.decorators[:_MAX_LIST],
            "modifiers": definition.modifiers[:_MAX_LIST],
            "params": [f"{p.name}: {p.type}" if p.type else p.name for p in definition.params][:_MAX_LIST],
            "return_type": definition.return_type,
            "is_abstract": definition.is_abstract,
            "complexity": definition.complexity,
            "file_id": function.module.file_id,
            "class_id": cls.id if cls else None,
            "class_name": cls.definition.qualified_name if cls else None,
            "parent_function": definition.parent_function,
            "is_entry_point": entry is not None,
            "unresolved_calls": self.resolution.unresolved.get(function.id, [])[:_MAX_LIST],
        }
        if entry is not None:
            properties.update(
                {
                    "entry_kind": entry.kind,
                    "http_method": entry.method,
                    "route": entry.route,
                    "entry_detail": entry.detail,
                }
            )
        self._node(function.id, NodeLabel.FUNCTION, properties)
        if cls is not None:
            self._edge(cls.id, function.id, RelType.CONTAINS)
        if cls is None or cls.module is not function.module:
            self._edge(function.module.file_id, function.id, RelType.CONTAINS)
        for statement in definition.sql:
            self._query(function, statement, path=module.path, constant=None)

    def _query(
        self, function: FunctionInfo, statement: SqlStatement, *, path: str, constant: str | None
    ) -> None:
        """Add a DatabaseQuery scope under ``function`` and record the tables it touches."""
        index = self._query_counts[function.id]
        self._query_counts[function.id] += 1
        query_id = make_id(NodeLabel.DATABASE_QUERY, self.repository_id, function.id, index)
        is_sql_file = function.module.family == "sql"
        reads = [self._canonical(name, is_sql_file) for name in statement.reads]
        writes = [self._canonical(name, is_sql_file) for name in statement.writes]
        executed_at = statement.used_at or statement.line
        self._node(
            query_id,
            NodeLabel.DATABASE_QUERY,
            {
                "name": query_label(statement),
                "qualified_name": f"{function.definition.qualified_name}#sql{index + 1}",
                "path": path,
                "language": "sql",
                "start_line": statement.line,
                "end_line": statement.end_line,
                "line": statement.line,
                "operation": statement.operation,
                "sql": _clip(statement.text, _MAX_SQL),
                "dynamic": statement.dynamic,
                "tables_read": reads[:_MAX_LIST],
                "tables_written": writes[:_MAX_LIST],
                "routines": list(statement.routines)[:_MAX_LIST],
                "constant_name": constant,
                "function_id": function.id,
                "function_name": function.definition.qualified_name,
                "function_path": function.module.path,
                "executed_at_line": executed_at,
            },
        )
        self._edge(function.id, query_id, RelType.CONTAINS)
        confidence = DYNAMIC_SQL_CONFIDENCE if statement.dynamic else 1.0
        operations = dict(statement.write_ops)
        for name in dict.fromkeys(reads):
            table = self._table(name)
            self._edge(
                query_id,
                table.id,
                RelType.READS_FROM_DB,
                {"operations": ["SELECT"], "confidence": confidence},
            )
            self._record_access(
                function.id, table.id, RelType.READS_FROM_DB, "SELECT", executed_at, confidence
            )
        for raw, name in dict.fromkeys(zip(statement.writes, writes, strict=True)):
            operation = operations.get(raw, statement.operation)
            table = self._table(name)
            self._edge(
                query_id,
                table.id,
                RelType.MUTATES_TABLE,
                {"operations": [operation], "confidence": confidence},
            )
            self._record_access(
                function.id, table.id, RelType.MUTATES_TABLE, operation, executed_at, confidence
            )

    def _canonical(self, name: str, is_sql_file: bool) -> str:
        """Map an ORM entity name used in HQL/JPQL to its table; other names pass through."""
        key = name.lower()
        if is_sql_file or (key in self._tables and self._tables[key].get("declared")):
            return key
        return self.symbols.entity_tables.get(key, key)

    def _record_access(
        self, function_id: str, table_id: str, rel: RelType, operation: str, line: int, confidence: float
    ) -> None:
        access = self._access[function_id, table_id, rel]
        access.operations[operation] = None
        access.lines[line] = None
        access.confidence = max(access.confidence, confidence)

    def _flush_table_access(self) -> None:
        for (function_id, table_id, rel), access in self._access.items():
            self._edge(
                function_id,
                table_id,
                rel,
                {
                    "operations": list(access.operations),
                    "lines": sorted(access.lines)[:_MAX_LIST],
                    "confidence": access.confidence,
                    "via": access.via,
                },
            )

    def _triggers(self, module: ModuleDef) -> None:
        if not module.triggers:
            return
        info = self.symbols.modules[module.path]
        for trigger in module.triggers:
            function = info.by_qualified.get(trigger.name)
            if function is None:
                continue
            table = self._table(trigger.table)
            self._edge(
                table.id,
                function.id,
                RelType.FIRES_TRIGGER,
                {
                    "events": [event.upper() for event in trigger.events],
                    "timing": trigger.timing,
                    "lines": [trigger.start_line],
                    "confidence": 1.0,
                },
            )
            node = self.graph.nodes[function.id]
            node.properties.update(
                {"trigger_table": table.name, "trigger_events": [event.upper() for event in trigger.events]}
            )

    # --------------------------------------------------------------------- edges

    def _calls_and_inheritance(self) -> None:
        for call in self.resolution.calls.values():
            self._edge(
                call.source,
                call.target,
                RelType.CALLS,
                {
                    "lines": call.lines[:_MAX_LIST],
                    "confidence": call.confidence,
                    "resolution": call.resolution,
                    # Call sites that only a name match ties to this target; never cited as fact.
                    "possible_lines": call.weak_lines[:_MAX_LIST]
                    if call.weak_lines is not call.lines
                    else None,
                },
            )
        for source, target, kind in self.resolution.inherits:
            self._edge(source, target, RelType.INHERITS_FROM, {"kind": kind})

    def _constant_queries(self) -> None:
        for constant in self.resolution.constant_queries:
            function = self._functions.get(constant.function_id)
            if function is not None:
                self._query(function, constant.statement, path=constant.path, constant=constant.name)

    def _name_references(self) -> None:
        """Link functions to tables they name in a plain string (``jdbc.delete("tbl_x", ...)``)."""
        per_function: Counter[str] = Counter()
        for function_id, name, line in self.resolution.name_references:
            table = self._tables.get(name.rsplit(".", 1)[-1])
            if table is None or ("_" not in name and len(name) < 6) or per_function[function_id] >= 8:
                continue
            if any(
                (function_id, table.id, rel) in self._access
                for rel in (RelType.READS_FROM_DB, RelType.MUTATES_TABLE)
            ):
                continue
            per_function[function_id] += 1
            access = self._access[function_id, table.id, RelType.READS_FROM_DB]
            access.operations["REFERENCE"] = None
            access.lines[line] = None
            access.confidence = NAME_REFERENCE_CONFIDENCE
            access.via = "name_reference"

    # --------------------------------------------------------------------- flags

    def _flags(self) -> None:
        fan_in: Counter[str] = Counter()
        fan_out: Counter[str] = Counter()
        any_caller: set[str] = set()
        for call in self.resolution.calls.values():
            if call.source == call.target:
                continue
            any_caller.add(call.target)
            if call.confidence >= DEFAULT_MIN_CONFIDENCE:
                fan_in[call.target] += 1
                fan_out[call.source] += 1
        fired = {edge.target for edge in self.graph.edges.values() if edge.type is RelType.FIRES_TRIGGER}
        for function in self.symbols.functions:
            node = self.graph.nodes[function.id]
            definition = function.definition
            is_test = _is_test(function)
            boilerplate = _is_boilerplate(definition)
            node.properties.update(
                {
                    "fan_in": fan_in[function.id],
                    "fan_out": fan_out[function.id],
                    "query_count": self._query_counts[function.id],
                    "is_test": is_test,
                    "is_boilerplate": boilerplate,
                    "is_override": function.id in self.resolution.overrides,
                    "dead_candidate": self._dead_candidate(function, any_caller, fired, is_test, boilerplate),
                }
            )

    def _dead_candidate(
        self, function: FunctionInfo, any_caller: set[str], fired: set[str], is_test: bool, boilerplate: bool
    ) -> bool:
        """No static caller was found and nothing suggests an invisible one.

        This is a *candidate* flag. Anything a framework, a subclass contract, a
        callback registration or external code could invoke is excluded, at the
        price of missing some genuinely dead code.
        """
        definition = function.definition
        if function.id in any_caller or function.id in fired or is_test or boilerplate:
            return False
        if definition.entry_point is not None or definition.is_abstract:
            return False
        if definition.kind is not FunctionKind.FUNCTION and definition.kind is not FunctionKind.METHOD:
            return False  # constructors, routes, scripts, stored routines, triggers
        if function.module.family == "sql":
            return False  # stored routines are called by clients we cannot see
        if function.id in self.resolution.overrides or function.id in self.resolution.referenced:
            return False
        if definition.name in (
            "main",
            "__init__",
            "__new__",
            "__call__",
            "run",
        ) or definition.name.startswith("__"):
            return False
        if any(decorator.split("(")[0] not in _NEUTRAL_DECORATORS for decorator in definition.decorators):
            return False  # registered with a framework through an annotation / decorator
        cls = function.cls
        if cls is not None:
            declared = len(cls.definition.bases) + len(cls.definition.interfaces)
            if declared > len(cls.bases):
                return False  # extends a library type: may implement its contract (doGet, run, ...)
        return function.module.definition.default_export != definition.qualified_name


def repository_node_id(repository_id: str) -> str:
    return make_id(NodeLabel.REPOSITORY, repository_id)


def _is_test(function: FunctionInfo) -> bool:
    definition = function.definition
    if is_test_path(function.module.path):
        return True
    if any(decorator.split("(")[0] in _TEST_DECORATORS for decorator in definition.decorators):
        return True
    return bool(_TEST_NAME_RE.match(definition.name)) and function.module.family in ("python", "java")


def _is_boilerplate(definition: FunctionDef) -> bool:
    """Accessors and object-protocol methods that carry no business logic."""
    if definition.sql or definition.entry_point is not None:
        return False
    size = definition.end_line - definition.start_line + 1
    if definition.name in _BOILERPLATE_NAMES:
        return size <= 25
    if _ACCESSOR_RE.match(definition.name):
        return size <= 4 and len(definition.calls) <= 1 and definition.complexity <= 1
    return False
