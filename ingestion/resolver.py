"""Cross-file symbol resolution: turns per-file call sites into graph edges.

Static call resolution for dynamically dispatched or untyped code is inherently
approximate, so every resolved edge carries

* ``resolution`` - *how* the target was found (``self``, ``typed-receiver``,
  ``import``, ``unique-name`` ...), and
* ``confidence`` - a score in ``(0, 1]`` that reflects the strength of that evidence.

Edges below :data:`DEFAULT_MIN_CONFIDENCE` are stored but hidden from default
traversals; the UI and the LLM context present them as *possible* callers. An
edge is never invented without evidence: a call whose target cannot be narrowed
down is left unresolved and recorded on the calling function instead.

Resolution order for a call site (first hit wins):

1. the receiver's static type (declared local/parameter/field type, ``this``/``self``,
   freshly constructed object, static class reference), walking up super types and,
   for abstract targets, down to the implementations;
2. lexical scope: nested functions, the enclosing class (Java/C++ implicit ``this``),
   functions of the same file;
3. imports, following re-exports ("barrel" files) up to three levels;
4. name-only matching, which is restricted to names that are unique in the
   repository (or nearly so) and always scored low.

Callbacks and method references (``CallSite.kind == "reference"``) stop after step 3:
a bare identifier passed as an argument is far too weak a signal for a global match.
"""

from __future__ import annotations

import posixpath
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Final, TypeVar

from graph_engine.schema import NodeLabel, make_id
from parsers.base import make_sql_statement
from parsers.ir import (
    CallSite,
    ClassDef,
    ConstantDef,
    FunctionDef,
    FunctionKind,
    ImportDef,
    Language,
    ModuleDef,
    SqlStatement,
)
from parsers.sql_analysis import looks_like_sql

#: Edges below this confidence are hidden from default graph traversals.
DEFAULT_MIN_CONFIDENCE: Final = 0.5

FAMILY: Final[dict[Language, str]] = {
    Language.JAVA: "java",
    Language.PYTHON: "python",
    Language.JAVASCRIPT: "js",
    Language.TYPESCRIPT: "js",
    Language.CPP: "cpp",
    Language.SQL: "sql",
}

_JS_EXTENSIONS: Final = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_SELF_NAMES: Final = frozenset({"this", "self", "cls"})
_NOT_A_SYMBOL: Final = frozenset({None, "*", "default"})
_SYNTHETIC_KINDS: Final = frozenset({FunctionKind.SCRIPT, FunctionKind.ROUTE})
_MAX_IMPLEMENTATIONS: Final = 8
_MAX_AMBIGUOUS: Final = 3
_MAX_UNRESOLVED_NAMES: Final = 25
_MAX_CALL_LINES: Final = 20
_MAX_SCOPE_DEPTH: Final = 8

#: Method names so common in standard libraries that a name-only match is meaningless.
_COMMON_METHODS: Final = frozenset(
    """
    get set put add remove delete contains size length isEmpty clear keys values items entries
    toString equals hashCode compareTo clone append push pop shift unshift slice splice concat join split
    map filter reduce forEach find some every sort reverse includes indexOf format replace trim
    strip lower upper startswith endswith startsWith endsWith substring substr charAt open close read write
    flush next hasNext iterator stream collect of then catch finally resolve reject on once emit call apply
    bind log info warn warning error debug trace print println printf fatal exception json send status end
    render execute query prepare commit rollback begin cursor fetchall fetchone fetchmany connect run start
    stop init update insert save load parse build create count sum min max abs round str int float len
    list dict tuple range enumerate zip isinstance getattr setattr hasattr super type id name value data
    toJSON stringify getTime now valueOf copy exists first last empty c_str extend encode decode
    setString setInt setLong getString getInt getLong executeQuery executeUpdate prepareStatement
    """.split()  # noqa: SIM905 - a word list reads better than 150 quoted literals
)

_ENTITY_DECORATORS: Final = frozenset({"Entity", "Table", "Document", "MappedSuperclass"})


@dataclass(slots=True, eq=False)
class ModuleInfo:
    definition: ModuleDef
    family: str
    file_id: str
    #: Classes by simple and by qualified name.
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    #: Functions callable by their bare name inside this file.
    functions: dict[str, list[FunctionInfo]] = field(default_factory=dict)
    by_qualified: dict[str, FunctionInfo] = field(default_factory=dict)
    #: Local binding name -> import.
    imports: dict[str, ImportDef] = field(default_factory=dict)
    #: Imports that make every symbol of the target visible (``import *``, ``#include``, ``export *``).
    wildcards: list[ImportDef] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.definition.path

    @property
    def directory(self) -> str:
        return posixpath.dirname(self.definition.path)


@dataclass(slots=True, eq=False)
class ClassInfo:
    id: str
    module: ModuleInfo
    definition: ClassDef
    methods: dict[str, list[FunctionInfo]] = field(default_factory=dict)
    #: Resolved super types with the relationship kind (``extends`` / ``implements``).
    bases: list[tuple[ClassInfo, str]] = field(default_factory=list)
    subclasses: list[ClassInfo] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def path(self) -> str:
        return self.module.definition.path


@dataclass(slots=True, eq=False)
class FunctionInfo:
    id: str
    module: ModuleInfo
    definition: FunctionDef
    cls: ClassInfo | None = None
    parent: FunctionInfo | None = None

    @property
    def name(self) -> str:
        return self.definition.name


@dataclass(slots=True)
class NamespaceRef:
    """An object-literal namespace: ``export const api = { list() {...} }``."""

    module: ModuleInfo
    prefix: str


@dataclass(slots=True)
class _Receiver:
    target: ClassInfo | ModuleInfo | NamespaceRef | None = None
    factor: float = 1.0
    how: str = "typed-receiver"
    #: The receiver is known to be defined outside the repository (library type or package).
    external: bool = False


_UNKNOWN: Final = _Receiver()
_EXTERNAL: Final = _Receiver(external=True)

Target = tuple[FunctionInfo, float, str]
T = TypeVar("T", ModuleInfo, ClassInfo)


@dataclass(slots=True)
class ResolvedCall:
    source: str
    target: str
    #: Call-site lines backing the edge at its final confidence level.
    lines: list[int]
    confidence: float
    resolution: str
    #: Call sites that on their own would only justify a hidden (low-confidence) edge.
    weak_lines: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ConstantQuery:
    """A SQL constant defined in one place and executed by a function elsewhere."""

    function_id: str
    statement: SqlStatement
    #: File in which the constant is defined (``statement.line`` refers to this file).
    path: str
    name: str


@dataclass(slots=True)
class Resolution:
    calls: dict[tuple[str, str], ResolvedCall] = field(default_factory=dict)
    #: ``(class id, super class id, kind)``
    inherits: list[tuple[str, str, str]] = field(default_factory=list)
    constant_queries: list[ConstantQuery] = field(default_factory=list)
    #: ``(function id, lower-cased name, line)`` - identifier-like strings that may name a table.
    name_references: list[tuple[str, str, int]] = field(default_factory=list)
    #: Calls that could not be bound to code in the repository, per function id.
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    #: Methods that override or implement a method of a super type.
    overrides: set[str] = field(default_factory=set)
    #: Functions passed around as callbacks / handlers.
    referenced: set[str] = field(default_factory=set)

    def add_call(self, source: str, target: str, line: int, confidence: float, resolution: str) -> None:
        if source == target:
            resolution = "recursion"
        confidence = round(min(max(confidence, 0.05), 1.0), 2)
        existing = self.calls.get((source, target))
        if existing is None:
            existing = self.calls[source, target] = ResolvedCall(source, target, [], confidence, resolution)
        elif confidence > existing.confidence:
            existing.confidence = confidence
            existing.resolution = resolution
        # A guess must not borrow the credibility of a well-supported call site on the same edge.
        bucket = existing.lines if confidence >= DEFAULT_MIN_CONFIDENCE else existing.weak_lines
        if line not in bucket and len(bucket) < _MAX_CALL_LINES:
            bucket.append(line)

    def finish(self) -> None:
        """Settle the cited lines of every edge once all call sites are known."""
        for call in self.calls.values():
            if not call.lines:
                call.lines = call.weak_lines
            call.lines.sort()


class SymbolTable:
    """Indexes every definition of a repository and resolves references between them."""

    def __init__(self, repository_id: str, modules: list[ModuleDef]) -> None:
        self.repository_id = repository_id
        self.modules: dict[str, ModuleInfo] = {}
        self.functions: list[FunctionInfo] = []
        self.classes: list[ClassInfo] = []
        #: Lower-cased entity class name -> lower-cased table name (ORM mappings).
        self.entity_tables: dict[str, str] = {}
        self._classes_by_name: dict[tuple[str, str], list[ClassInfo]] = defaultdict(list)
        self._functions_by_name: dict[tuple[str, str], list[FunctionInfo]] = defaultdict(list)
        self._methods_by_name: dict[tuple[str, str], list[FunctionInfo]] = defaultdict(list)
        self._java_classes: dict[tuple[str, str], ClassInfo] = {}
        self._python_modules: dict[str, list[ModuleInfo]] = defaultdict(list)
        self._by_stem: dict[str, ModuleInfo] = {}
        self._routines: dict[str, list[FunctionInfo]] = defaultdict(list)
        self._import_cache: dict[tuple[str, str, str | None, int], ModuleInfo | None] = {}
        self._suffix_cache: dict[tuple[str, str, str], ModuleInfo | None] = {}
        self._ancestor_cache: dict[int, list[ClassInfo]] = {}
        self._descendant_cache: dict[int, list[ClassInfo]] = {}
        #: Set while resolving one call: the target is repository code even if no edge results.
        self._bound = False
        for module in sorted(modules, key=lambda m: m.path):
            self._index_module(module)
        self._link()

    # ---------------------------------------------------------------- indexing

    def _index_module(self, module: ModuleDef) -> None:
        family = FAMILY[module.language]
        info = ModuleInfo(module, family, make_id(NodeLabel.FILE, self.repository_id, module.path))
        self.modules[module.path] = info
        stem = _strip_extension(module.path)
        self._by_stem.setdefault(stem, info)
        if family == "python":
            parts = stem.split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for start in range(len(parts)):
                self._python_modules[".".join(parts[start:])].append(info)
        for imported in module.imports:
            if imported.alias:
                info.imports.setdefault(imported.alias, imported)
            elif imported.name == "*" or family == "cpp":
                info.wildcards.append(imported)

        used_ids: set[str] = set()

        def unique_id(label: NodeLabel, qualified_name: str, line: int) -> str:
            node_id = make_id(label, self.repository_id, module.path, qualified_name)
            if node_id in used_ids:  # the same name defined twice in one file
                node_id = make_id(label, self.repository_id, module.path, qualified_name, line)
            used_ids.add(node_id)
            return node_id

        def add_function(definition: FunctionDef, cls: ClassInfo | None) -> FunctionInfo:
            node_id = unique_id(NodeLabel.FUNCTION, definition.qualified_name, definition.start_line)
            function = FunctionInfo(node_id, info, definition, cls)
            self.functions.append(function)
            info.by_qualified.setdefault(definition.qualified_name, function)
            return function

        for class_def in module.classes:
            cls = ClassInfo(
                unique_id(NodeLabel.CLASS, class_def.qualified_name, class_def.start_line), info, class_def
            )
            self.classes.append(cls)
            info.classes.setdefault(class_def.name, cls)
            info.classes.setdefault(class_def.qualified_name, cls)
            self._classes_by_name[family, class_def.name].append(cls)
            if family == "java" and module.package:
                self._java_classes.setdefault((module.package, class_def.name), cls)
            for method in class_def.methods:
                function = add_function(method, cls)
                cls.methods.setdefault(method.name, []).append(function)
                self._methods_by_name[family, method.name].append(function)
            table = class_def.table
            if table is None and _ENTITY_DECORATORS & set(class_def.decorators):
                table = class_def.name  # JPA default: the entity name is the table name
            if table:
                self.entity_tables.setdefault(class_def.name.lower(), table.lower())

        for function_def in module.functions:
            function = add_function(function_def, None)
            if function_def.kind in _SYNTHETIC_KINDS:
                continue
            if family == "sql":
                if function_def.kind is not FunctionKind.TRIGGER:
                    for key in {function_def.name.lower(), function_def.qualified_name.lower()}:
                        self._routines[key].append(function)
                continue
            if function_def.owner is not None or function_def.parent_function is not None:
                continue  # out-of-line methods and nested functions are attached in _link
            if family == "js" and function_def.qualified_name != function_def.name:
                continue  # ``api.list``: reachable through its namespace only
            info.functions.setdefault(function_def.name, []).append(function)
            self._functions_by_name[family, function_def.name].append(function)

    def _link(self) -> None:
        """Attach out-of-line methods and nested functions, then resolve super types."""
        for function in self.functions:
            definition = function.definition
            if function.cls is None and definition.owner is not None:
                family = function.module.family
                simple = re.split(r"::|\.", definition.owner)[-1]
                owner = self._class_by_name(function.module, simple)
                if owner is not None:
                    function.cls = owner[0]
                    owner[0].methods.setdefault(definition.name, []).append(function)
                    self._methods_by_name[family, definition.name].append(function)
                else:  # the "owner" is a namespace, not a class we know: a plain function
                    function.module.functions.setdefault(definition.name, []).append(function)
                    self._functions_by_name[family, definition.name].append(function)
            if definition.parent_function is not None:
                function.parent = function.module.by_qualified.get(definition.parent_function)
        for cls in self.classes:
            for names, kind in ((cls.definition.bases, "extends"), (cls.definition.interfaces, "implements")):
                for name in names:
                    resolved = self._class_by_name(cls.module, name.rsplit(".", 1)[-1].rsplit("::", 1)[-1])
                    if resolved is not None and resolved[0] is not cls:
                        cls.bases.append((resolved[0], kind))
                        resolved[0].subclasses.append(cls)

    # ------------------------------------------------------------ module lookup

    def resolve_import(self, module: ModuleInfo, imported: ImportDef) -> ModuleInfo | None:
        """The module of this repository that an import refers to, or ``None`` for external code."""
        key = (module.path, imported.module, imported.name, imported.level)
        if key not in self._import_cache:
            found: ModuleInfo | None = None
            if module.family == "python":
                found = self._python_import(module, imported)
            elif module.family == "js":
                found = self._js_import(module, imported.module)
            elif module.family == "cpp":
                found = self._by_suffix(imported.module, module)
            self._import_cache[key] = found
        return self._import_cache[key]

    def _python_import(self, module: ModuleInfo, imported: ImportDef) -> ModuleInfo | None:
        dotted = imported.module
        symbol = imported.name if imported.name not in _NOT_A_SYMBOL else None
        if imported.level:
            base = module.directory.split("/") if module.directory else []
            if imported.level > 1:
                base = base[: max(len(base) - (imported.level - 1), 0)]
            parts = base + (dotted.split(".") if dotted else [])
            candidates = ["/".join(parts)] if parts else []
            if symbol:
                candidates.insert(0, "/".join([*parts, symbol]))  # ``from . import sibling``
            for candidate in candidates:
                found = self._python_file(candidate)
                if found is not None:
                    return found
            return None
        names = [dotted]
        if symbol:
            names.insert(0, f"{dotted}.{symbol}")  # ``from package import module``
        for name in names:
            found = self._python_absolute(name, module)
            if found is not None:
                return found
        return None

    def _python_file(self, stem: str) -> ModuleInfo | None:
        for candidate in (stem, f"{stem}/__init__"):
            found = self._by_stem.get(candidate)
            if found is not None and found.family == "python":
                return found
        return None

    def _python_absolute(self, dotted: str, origin: ModuleInfo) -> ModuleInfo | None:
        """Resolve ``a.b.c`` against plausible source roots.

        Each directory above the importing file is tried as an import root, nearest
        first: this covers the script directory, ``src/`` layouts and Python 2 implicit
        relative imports. A name that belongs to the standard library is only accepted
        from a directory that is not itself a package, so ``import logging`` inside
        package ``app`` never binds to ``app/logging.py``. A file elsewhere in the
        repository is accepted only for dotted names, or for a unique top-level module.
        """
        matches = self._python_modules.get(dotted)
        if not matches:
            return None
        relative = dotted.replace(".", "/")
        is_stdlib = dotted.split(".", 1)[0] in sys.stdlib_module_names
        directory = origin.directory
        while True:
            if not (is_stdlib and self._is_python_package(directory)):
                found = self._python_file(f"{directory}/{relative}" if directory else relative)
                if found is not None:
                    return found
            if not directory:
                break
            directory = posixpath.dirname(directory)
        if is_stdlib:
            return None
        if "." in dotted:
            return _nearest(matches, origin)
        shallow = [
            match for match in matches if not self._is_python_package(posixpath.dirname(match.directory))
        ]
        return shallow[0] if len(shallow) == 1 and len(matches) == 1 else None

    def _is_python_package(self, directory: str) -> bool:
        if not directory:
            return False
        found = self._by_stem.get(f"{directory}/__init__")
        return found is not None and found.family == "python"

    def _js_import(self, module: ModuleInfo, specifier: str) -> ModuleInfo | None:
        if specifier.startswith("."):
            return self._js_path(posixpath.normpath(posixpath.join(module.directory, specifier)))
        if specifier.startswith(("@/", "~/")):  # conventional tsconfig path alias
            return self._by_suffix(specifier[2:], module)
        if specifier.startswith(("src/", "app/", "lib/")):  # baseUrl-relative
            return self._js_path(specifier)
        return None  # a package

    def _js_path(self, target: str) -> ModuleInfo | None:
        stem, extension = posixpath.splitext(target)
        candidates = [stem, target] if extension in _JS_EXTENSIONS else [target]
        candidates.append(f"{target}/index")
        for candidate in candidates:
            found = self._by_stem.get(candidate)
            if found is not None and found.family == "js":
                return found
        return None

    def _by_suffix(self, tail: str, origin: ModuleInfo) -> ModuleInfo | None:
        """The file whose path ends with ``tail``, nearest to ``origin``.

        C/C++ includes name a concrete file, so they match on the full path;
        everything else matches with the extension ignored (``@/components`` may be
        ``components.ts`` or ``components/index.tsx``).
        """
        while tail.startswith(("./", "../")):
            tail = tail.split("/", 1)[1]
        if not tail:
            return None
        key = (origin.family, origin.directory, tail)
        if key not in self._suffix_cache:
            matches: list[ModuleInfo]
            if origin.family == "cpp":
                local = self.modules.get(posixpath.normpath(posixpath.join(origin.directory, tail)))
                matches = (
                    [local]
                    if local is not None
                    else [
                        info
                        for path, info in self.modules.items()
                        if path == tail or path.endswith("/" + tail)
                    ]
                )
            else:
                stem = _strip_extension(tail)
                matches = [
                    info
                    for path, info in self._by_stem.items()
                    if info.family == origin.family
                    and (path == stem or path.endswith(("/" + stem, "/" + stem + "/index")))
                ]
            matches = [match for match in matches if match is not origin and match.family == origin.family]
            self._suffix_cache[key] = _nearest(matches, origin) if matches else None
        return self._suffix_cache[key]

    # ------------------------------------------------------------- class lookup

    def _class_by_name(self, module: ModuleInfo, name: str) -> tuple[ClassInfo, float] | None:
        """Resolve a simple type name as seen from ``module``: ``(class, confidence factor)``."""
        if not name:
            return None
        local = module.classes.get(name)
        if local is not None:
            return local, 1.0
        family = module.family
        imported = module.imports.get(name)
        if imported is not None:
            if family == "java":
                found = self._java_classes.get((imported.module, imported.name or name))
                if found is not None:
                    return found, 1.0
            else:
                target = self.resolve_import(module, imported)
                if target is None:
                    return None  # imported from a package: not ours, whatever shares its name
                symbol = imported.name if imported.name not in _NOT_A_SYMBOL else None
                cls = target.classes.get(symbol or name)
                if cls is None and symbol is None and target.definition.default_export:
                    cls = target.classes.get(target.definition.default_export)
                if cls is not None:
                    return cls, 1.0
                forwarded = self._reexported_class(target, symbol or name, 0)
                return (forwarded, 0.95) if forwarded is not None else None
        for wildcard in module.wildcards:
            target = self.resolve_import(module, wildcard)
            if target is not None and name in target.classes:
                return target.classes[name], 1.0
        candidates = self._classes_by_name.get((family, name), [])
        if not candidates:
            return None
        if family == "java":
            package = module.definition.package
            same_package = [c for c in candidates if c.module.definition.package == package]
            if len(same_package) == 1:
                return same_package[0], 1.0
            wildcard_packages = {
                w.module for w in module.definition.imports if w.name == "*" and not w.is_static
            }
            starred = [c for c in candidates if c.module.definition.package in wildcard_packages]
            if len(starred) == 1:
                return starred[0], 0.95
        if len(candidates) == 1:
            return candidates[0], 0.9 if family in ("java", "cpp") else 0.75
        return _nearest(candidates, module), 0.6

    def _reexported_class(self, module: ModuleInfo, name: str, depth: int) -> ClassInfo | None:
        """Follow barrel files: ``export * from './x'`` / ``from .x import A``."""
        if depth > 3:
            return None
        forwards = list(module.wildcards)
        named = module.imports.get(name)
        if named is not None:
            forwards.insert(0, named)
        for forward in forwards:
            target = self.resolve_import(module, forward)
            if target is None or target is module:
                continue
            symbol = name if forward.name in _NOT_A_SYMBOL else str(forward.name)
            found = target.classes.get(symbol) or self._reexported_class(target, symbol, depth + 1)
            if found is not None:
                return found
        return None

    def ancestors(self, cls: ClassInfo) -> list[ClassInfo]:
        """Super types of ``cls``, nearest first."""
        cached = self._ancestor_cache.get(id(cls))
        if cached is None:
            seen: dict[int, ClassInfo] = {}
            queue = [base for base, _kind in cls.bases]
            while queue:
                current = queue.pop(0)
                if id(current) in seen or current is cls:
                    continue
                seen[id(current)] = current
                queue.extend(base for base, _kind in current.bases)
            cached = self._ancestor_cache[id(cls)] = list(seen.values())
        return cached

    def descendants(self, cls: ClassInfo, limit: int = 200) -> list[ClassInfo]:
        """Sub types of ``cls``, nearest first."""
        cached = self._descendant_cache.get(id(cls))
        if cached is None:
            seen: dict[int, ClassInfo] = {}
            queue = list(cls.subclasses)
            while queue and len(seen) < limit:
                current = queue.pop(0)
                if id(current) in seen or current is cls:
                    continue
                seen[id(current)] = current
                queue.extend(current.subclasses)
            cached = self._descendant_cache[id(cls)] = list(seen.values())
        return cached

    def _field_type(self, cls: ClassInfo, name: str) -> str | None:
        for candidate in (cls, *self.ancestors(cls)):
            found = candidate.definition.fields.get(name)
            if found:
                return found
        return None

    def find_method(
        self, cls: ClassInfo, name: str, arg_count: int | None
    ) -> tuple[list[FunctionInfo], bool]:
        """Methods ``name`` of ``cls`` or of the nearest super type defining one: ``(methods, inherited)``."""
        for index, candidate in enumerate((cls, *self.ancestors(cls))):
            methods = candidate.methods.get(name)
            if methods:
                return _by_arity(methods, arg_count), index > 0
        return [], False

    def routines(self, name: str) -> list[FunctionInfo]:
        """Stored routines matching a (possibly schema-qualified) name, case-insensitively."""
        key = name.lower()
        found = self._routines.get(key)
        if not found and "." in key:
            parts = key.split(".")
            found = self._routines.get(".".join(parts[-2:])) or self._routines.get(parts[-1])
        return found[:_MAX_AMBIGUOUS] if found else []

    # ---------------------------------------------------------------- resolving

    def resolve(self) -> Resolution:
        result = Resolution()
        for cls in self.classes:
            seen_bases: set[str] = set()
            for base, kind in cls.bases:
                if base.id not in seen_bases:
                    seen_bases.add(base.id)
                    result.inherits.append((cls.id, base.id, kind))
            ancestors = self.ancestors(cls)
            if ancestors:
                for name, methods in cls.methods.items():
                    if any(name in ancestor.methods for ancestor in ancestors):
                        result.overrides.update(method.id for method in methods)
        for function in self.functions:
            self._resolve_function(function, result)
        result.finish()
        return result

    def _resolve_function(self, function: FunctionInfo, result: Resolution) -> None:
        definition = function.definition
        unresolved: dict[str, None] = {}
        for call in definition.calls:
            self._bound = False
            targets = self._resolve_call(function, call)
            if not targets:
                if (
                    not self._bound
                    and call.kind != "reference"
                    and function.module.family != "sql"  # SQL builtins would drown the list
                    and len(unresolved) < _MAX_UNRESOLVED_NAMES
                ):
                    label = self._unresolved_label(function, call)
                    if label:
                        unresolved[label] = None
                continue
            for target, confidence, resolution in targets:
                if call.kind == "reference":
                    result.referenced.add(target.id)
                result.add_call(function.id, target.id, call.line, confidence, resolution)
        if unresolved:
            result.unresolved[function.id] = list(unresolved)

        if function.module.family != "sql":  # stored routines invoked from embedded SQL
            for statement in definition.sql:
                self._sql_calls(function, statement, result)

        for reference in definition.constant_refs:
            constant = self._resolve_constant(function, reference.qualifier, reference.name)
            if constant is None:
                continue
            path, value = constant
            if looks_like_sql(value.value):
                query = make_sql_statement(
                    value.value, value.line, value.end_line, dynamic=value.dynamic, used_at=reference.line
                )
                if query is not None:
                    result.constant_queries.append(ConstantQuery(function.id, query, path, value.name))
                    self._sql_calls(function, query, result)
            elif _IDENTIFIER_RE.match(value.value):
                result.name_references.append((function.id, value.value.lower(), reference.line))
        for text, line in definition.string_refs:
            routines = self.routines(text) if function.module.family != "sql" else []
            if routines:  # ``cursor.callproc("sp_post_charge", ...)``
                distinctive = "_" in text or len(text) >= 8
                for routine in routines:
                    result.add_call(
                        function.id, routine.id, line, 0.7 if distinctive else 0.4, "name-reference"
                    )
            else:
                result.name_references.append((function.id, text.lower(), line))

    @staticmethod
    def _unresolved_label(function: FunctionInfo, call: CallSite) -> str | None:
        """How an unbound call is listed on its function: mostly calls into libraries."""
        receiver = call.receiver
        if receiver is None or receiver == "<expr>" or receiver in _SELF_NAMES or receiver == "super":
            return None if call.name in _COMMON_METHODS else call.name
        if receiver.startswith("type:"):
            return f"{receiver[5:]}.{call.name}"
        root = re.split(r"\.|::|->", receiver, maxsplit=1)[0]
        if root in function.module.imports or root[:1].isupper():
            return f"{receiver}.{call.name}"  # ``requests.get``, ``StringUtils.isBlank``
        return None if call.name in _COMMON_METHODS else f"{receiver}.{call.name}"

    def _sql_calls(self, function: FunctionInfo, statement: SqlStatement, result: Resolution) -> None:
        line = statement.used_at or statement.line
        for name in statement.routines:
            for routine in self.routines(name):
                result.add_call(function.id, routine.id, line, 0.9, "sql-call")
        for name in statement.functions:
            for routine in self.routines(name):
                result.add_call(function.id, routine.id, line, 0.8, "sql-function")

    def _resolve_constant(
        self, function: FunctionInfo, qualifier: str | None, name: str
    ) -> tuple[str, ConstantDef] | None:
        """Find the definition of a named constant: ``(path of the defining file, definition)``."""
        module = function.module
        if qualifier is not None:
            resolved = self._class_by_name(module, qualifier)
            if resolved is not None:
                for candidate in (resolved[0], *self.ancestors(resolved[0])):
                    constant = candidate.definition.constants.get(name)
                    if constant is not None:
                        return candidate.path, constant
            imported = module.imports.get(qualifier)
            target = self.resolve_import(module, imported) if imported is not None else None
            if target is not None and name in target.definition.constants:
                return target.path, target.definition.constants[name]
            return None
        cls = self._enclosing_class(function)
        if cls is not None:  # the parser folds same-class constants; inherited ones arrive here
            for candidate in self.ancestors(cls):
                constant = candidate.definition.constants.get(name)
                if constant is not None:
                    return candidate.path, constant
        imported = module.imports.get(name)
        if imported is not None:
            if module.family == "java":
                owner = self._static_import_owner(imported) if imported.is_static else None
                if owner is not None and name in owner.definition.constants:
                    return owner.path, owner.definition.constants[name]
                return None
            target = self.resolve_import(module, imported)
            symbol = imported.name if imported.name not in _NOT_A_SYMBOL else name
            if target is not None and symbol is not None and symbol in target.definition.constants:
                return target.path, target.definition.constants[symbol]
            return None
        for wildcard in module.wildcards:
            if module.family == "java":
                owner = self._static_import_owner(wildcard) if wildcard.is_static else None
                if owner is not None and name in owner.definition.constants:
                    return owner.path, owner.definition.constants[name]
                continue
            target = self.resolve_import(module, wildcard)
            if target is not None and name in target.definition.constants:
                return target.path, target.definition.constants[name]
        return None

    def _static_import_owner(self, imported: ImportDef) -> ClassInfo | None:
        package, _, class_name = imported.module.rpartition(".")
        return self._java_classes.get((package, class_name))

    # ------------------------------------------------------------ call resolution

    def _resolve_call(self, function: FunctionInfo, call: CallSite) -> list[Target]:
        module = function.module
        family = module.family
        name = call.name
        if family == "sql":
            return [(routine, 0.9, "sql-call") for routine in self.routines(name)]

        cls = self._enclosing_class(function)
        if call.kind == "super":  # ``super(...)`` inside a constructor
            if cls is not None:
                for base, kind in cls.bases:
                    if kind == "extends":
                        return self._constructors(base, call.arg_count, 0.9, "super")
            return []
        if call.receiver is None:
            return self._resolve_bare(function, cls, call)
        if call.receiver == "super":
            if cls is not None:
                for ancestor in self.ancestors(cls):
                    if name in ancestor.methods:
                        return [(m, 0.9, "super") for m in _by_arity(ancestor.methods[name], call.arg_count)]
            return []
        if cls is None and family == "js" and call.receiver == "this":
            # ``this`` inside an object-literal member refers to the sibling members.
            prefix = function.definition.qualified_name.rpartition(".")[0]
            sibling = module.by_qualified.get(f"{prefix}.{name}" if prefix else name)
            if sibling is not None and sibling.definition.kind not in _SYNTHETIC_KINDS:
                return [(sibling, 0.85, "self")]

        scale = 0.9 if call.kind == "reference" else 1.0
        receiver = self._receiver(function, cls, call.receiver)
        owner = receiver.target
        if isinstance(owner, ClassInfo):
            methods, inherited = self.find_method(owner, name, call.arg_count)
            if methods:
                is_self = receiver.how == "self"
                confidence = (0.95 if is_self else 0.9) * receiver.factor * scale
                how = "inherited" if inherited and is_self else receiver.how
                targets: list[Target] = [(method, confidence, how) for method in methods]
                return targets + self._implementations(owner, methods, call.arg_count, confidence)
            if receiver.how == "self":
                # Template method: the hook is only defined by subclasses.
                hooks: list[Target] = [
                    (method, 0.7 * scale, "subclass")
                    for sub in self.descendants(owner)
                    for method in _by_arity(sub.methods.get(name, []), call.arg_count)
                ]
                return hooks[:_MAX_IMPLEMENTATIONS]
            self._bound = True  # a known class without such a method (inherited from a library type)
            return []
        if isinstance(owner, ModuleInfo):
            return self._module_member(owner, name, call.arg_count, 0.9 * receiver.factor * scale, 0)
        if isinstance(owner, NamespaceRef):
            member = owner.module.by_qualified.get(f"{owner.prefix}.{name}")
            return [(member, 0.9 * scale, receiver.how)] if member is not None else []
        if receiver.external or call.kind == "reference":
            return []
        return self._by_method_name(function, call)

    def _enclosing_class(self, function: FunctionInfo) -> ClassInfo | None:
        current: FunctionInfo | None = function
        for _ in range(_MAX_SCOPE_DEPTH):
            if current is None:
                return None
            if current.cls is not None:
                return current.cls
            current = current.parent
        return None

    def _local_type(self, function: FunctionInfo, name: str) -> str | None:
        current: FunctionInfo | None = function
        for _ in range(_MAX_SCOPE_DEPTH):
            if current is None:
                return None
            found = current.definition.local_types.get(name)
            if found:
                return found
            current = current.parent
        return None

    def _constructors(
        self, cls: ClassInfo, arg_count: int | None, confidence: float, how: str
    ) -> list[Target]:
        self._bound = True
        for index, candidate in enumerate((cls, *self.ancestors(cls))):
            constructors = [
                method
                for methods in candidate.methods.values()
                for method in methods
                if method.definition.kind is FunctionKind.CONSTRUCTOR
            ]
            if constructors:
                # Without an explicit constructor the inherited one runs.
                factor = 1.0 if index == 0 else 0.9
                return [(m, confidence * factor, how) for m in _by_arity(constructors, arg_count)]
        return []

    def _implementations(
        self, owner: ClassInfo, methods: list[FunctionInfo], arg_count: int | None, confidence: float
    ) -> list[Target]:
        """A call through an interface or abstract method also reaches its implementations."""
        if not all(method.definition.is_abstract for method in methods):
            return []
        name = methods[0].name
        found = [
            method
            for sub in self.descendants(owner)
            for method in _by_arity(sub.methods.get(name, []), arg_count)
            if not method.definition.is_abstract
        ][:_MAX_IMPLEMENTATIONS]
        ceiling = 0.85 if len(found) == 1 else 0.7 if len(found) <= _MAX_AMBIGUOUS else 0.6
        return [(method, min(confidence, ceiling), "implementation") for method in found]

    def _resolve_bare(self, function: FunctionInfo, cls: ClassInfo | None, call: CallSite) -> list[Target]:
        module = function.module
        family = module.family
        name = call.name
        is_reference = call.kind == "reference"
        scale = 0.9 if is_reference else 1.0

        if call.kind == "new":
            resolved = self._class_by_name(module, name)
            if resolved is None:
                return []
            return self._constructors(resolved[0], call.arg_count, 0.95 * resolved[1], "constructor")

        # A function nested in this one or in an enclosing one.
        current: FunctionInfo | None = function
        for _ in range(_MAX_SCOPE_DEPTH):
            if current is None:
                break
            nested = module.by_qualified.get(f"{current.definition.qualified_name}.{name}")
            if nested is not None and nested.definition.parent_function is not None:
                return [(nested, 0.95 * scale, "nested")]
            current = current.parent

        # Java / C++: an unqualified call inside a class is a call on ``this``.
        if cls is not None and family in ("java", "cpp"):
            methods, inherited = self.find_method(cls, name, call.arg_count)
            if methods:
                how = "inherited" if inherited else "self"
                targets: list[Target] = [(method, 0.95 * scale, how) for method in methods]
                return targets + self._implementations(cls, methods, call.arg_count, 0.95)
            outer_name = cls.definition.owner
            if outer_name:  # an inner class calling a method of its enclosing class
                outer = cls.module.classes.get(outer_name)
                if outer is not None:
                    methods, _ = self.find_method(outer, name, call.arg_count)
                    if methods:
                        return [(method, 0.9 * scale, "outer-class") for method in methods]
            if family == "java":
                hooks: list[Target] = [
                    (method, 0.7 * scale, "subclass")
                    for sub in self.descendants(cls)
                    for method in _by_arity(sub.methods.get(name, []), call.arg_count)
                ]
                if hooks:
                    return hooks[:_MAX_IMPLEMENTATIONS]

        local = module.functions.get(name)
        if local:
            return [(target, 0.95 * scale, "same-file") for target in _by_arity(local, call.arg_count)]
        local_class = module.classes.get(name)
        if local_class is not None and family != "java":  # ``Foo(...)`` constructs in Python / JS / C++
            return self._constructors(local_class, call.arg_count, 0.95 * scale, "constructor")

        imported = module.imports.get(name)
        if imported is not None:
            if family == "java":
                owner = self._static_import_owner(imported) if imported.is_static else None
                if owner is None:
                    return []
                methods, _ = self.find_method(owner, imported.name or name, call.arg_count)
                return [(method, 0.9 * scale, "static-import") for method in methods]
            target = self.resolve_import(module, imported)
            if target is None:
                return []  # an external package
            if imported.name in (None, "default"):
                exported = target.definition.default_export
                found = (
                    self._module_member(target, exported, call.arg_count, 0.9 * scale, 0) if exported else []
                )
                return found or self._module_member(target, name, call.arg_count, 0.8 * scale, 0)
            symbol = name if imported.name == "*" else str(imported.name)
            return self._module_member(target, symbol, call.arg_count, 0.9 * scale, 0)

        for wildcard in module.wildcards:
            if family == "java":
                owner = self._static_import_owner(wildcard) if wildcard.is_static else None
                if owner is not None:
                    methods, _ = self.find_method(owner, name, call.arg_count)
                    if methods:
                        return [(method, 0.85 * scale, "static-import") for method in methods]
                continue
            target = self.resolve_import(module, wildcard)
            if target is not None:
                found = self._module_member(target, name, call.arg_count, 0.85 * scale, 0)
                if found:
                    return found

        if is_reference:
            return []
        candidates = self._functions_by_name.get((family, name), [])
        if not candidates:
            resolved = self._class_by_name(module, name) if family != "java" else None
            if resolved is not None:
                return self._constructors(resolved[0], call.arg_count, 0.8 * resolved[1], "constructor")
            return []
        candidates = _by_arity(candidates, call.arg_count)
        if family == "cpp":
            return self._cpp_free_function(module, candidates)
        if len(candidates) == 1:
            return [(candidates[0], 0.6, "unique-name")]
        if len(candidates) <= _MAX_AMBIGUOUS:
            return [(candidate, 0.3, "ambiguous") for candidate in candidates]
        return []

    def _cpp_free_function(self, module: ModuleInfo, candidates: list[FunctionInfo]) -> list[Target]:
        """C/C++ links by name: prefer the definition whose file matches an included header."""
        included = {posixpath.basename(_strip_extension(include.module)) for include in module.wildcards}
        via_header = [
            candidate
            for candidate in candidates
            if posixpath.basename(_strip_extension(candidate.module.path)) in included
        ]
        if len(via_header) == 1:
            return [(via_header[0], 0.9, "include")]
        if len(candidates) == 1:
            return [(candidates[0], 0.8, "unique-name")]
        if len(candidates) <= _MAX_AMBIGUOUS:
            return [(candidate, 0.4, "ambiguous") for candidate in candidates]
        return []

    def _module_member(
        self, module: ModuleInfo, name: str, arg_count: int | None, confidence: float, depth: int
    ) -> list[Target]:
        """A function, class constructor or default-export member called ``name`` in ``module``."""
        functions = module.functions.get(name)
        if functions:
            return [(target, confidence, "import") for target in _by_arity(functions, arg_count)]
        cls = module.classes.get(name)
        if cls is not None:
            return self._constructors(cls, arg_count, confidence, "import")
        exported = module.definition.default_export
        if exported:
            default_class = module.classes.get(exported)
            if default_class is not None:  # ``module.exports = new Service()``
                methods, _ = self.find_method(default_class, name, arg_count)
                if methods:
                    return [(method, confidence * 0.95, "import") for method in methods]
            member = module.by_qualified.get(f"{exported}.{name}")
            if member is not None:
                return [(member, confidence * 0.95, "import")]
        if depth < 3:  # barrel files and package __init__ re-exports
            forwards = list(module.wildcards)
            named = module.imports.get(name)
            if named is not None:
                forwards.insert(0, named)
            for forward in forwards:
                target = self.resolve_import(module, forward)
                if target is not None and target is not module:
                    symbol = name if forward.name in _NOT_A_SYMBOL else str(forward.name)
                    found = self._module_member(target, symbol, arg_count, confidence * 0.95, depth + 1)
                    if found:
                        return found
        return []

    def _receiver(self, function: FunctionInfo, cls: ClassInfo | None, receiver: str) -> _Receiver:
        """What a receiver expression denotes: a class (instance or static), a module or a namespace."""
        module = function.module
        family = module.family
        if receiver == "<expr>":
            return _UNKNOWN
        if receiver.startswith("type:"):
            resolved = self._class_by_name(module, receiver[5:])
            return _Receiver(resolved[0], resolved[1], "typed-receiver") if resolved else _EXTERNAL
        segments = re.split(r"\.|::|->", receiver)
        first, rest = segments[0], segments[1:]
        current: ClassInfo | ModuleInfo
        factor = 1.0
        how = "typed-receiver"

        if first in _SELF_NAMES:
            if cls is None:
                return _UNKNOWN
            current = cls
            how = "self" if not rest else "field"
        else:
            type_name = self._local_type(function, first)
            if type_name is None and cls is not None and family in ("java", "cpp"):
                type_name = self._field_type(cls, first)  # implicit ``this.``
                how = "field"
            if type_name is not None:
                resolved = self._class_by_name(module, type_name)
                if resolved is None:
                    return _EXTERNAL  # a library type: String, List, Connection ...
                current, factor = resolved
            elif first in module.imports and family != "java":
                imported = module.imports[first]
                target = self.resolve_import(module, imported)
                if target is None:
                    return _EXTERNAL  # an external package: never guess
                if imported.name is None and family == "python" and "." in imported.module:
                    # ``import a.b.c`` binds ``a``: the receiver must spell out the whole path.
                    path = imported.module.split(".")
                    if imported.alias == path[0]:
                        if rest[: len(path) - 1] != path[1:]:
                            return _UNKNOWN
                        rest = rest[len(path) - 1 :]
                current, how = target, "import"
                if imported.name not in _NOT_A_SYMBOL:
                    symbol = str(imported.name)
                    named_class = target.classes.get(symbol) or self._reexported_class(target, symbol, 0)
                    if named_class is not None:
                        current = named_class
                    elif any(key.startswith(symbol + ".") for key in target.by_qualified):
                        return _UNKNOWN if rest else _Receiver(NamespaceRef(target, symbol), 1.0, "import")
                    elif symbol in target.functions or symbol in target.definition.constants:
                        return _UNKNOWN  # a function or value, not something with methods we know
                    else:  # ``from package import module`` already resolved to the module
                        pass
                elif imported.name == "default":
                    exported = target.definition.default_export
                    default_class = target.classes.get(exported) if exported else None
                    if default_class is not None:
                        current = default_class
            else:
                resolved = self._class_by_name(module, first)
                if resolved is not None:
                    current, factor = resolved
                    how = "static"
                elif any(key.startswith(first + ".") for key in module.by_qualified):
                    return _UNKNOWN if rest else _Receiver(NamespaceRef(module, first), 1.0, "namespace")
                else:
                    return _UNKNOWN

        for segment in rest:
            if isinstance(current, ClassInfo):
                type_name = self._field_type(current, segment)
                if type_name is None:
                    return _UNKNOWN
                resolved = self._class_by_name(current.module, type_name)
                if resolved is None:
                    return _EXTERNAL
                current, factor, how = resolved[0], factor * resolved[1], "field"
            else:
                nested = current.classes.get(segment)
                if nested is None:
                    return _UNKNOWN
                current, how = nested, "import"
        return _Receiver(current, factor, how)

    def _by_method_name(self, function: FunctionInfo, call: CallSite) -> list[Target]:
        """Last resort for a receiver of unknown type: match by method name, scored low."""
        name = call.name
        if name in _COMMON_METHODS or len(name) < 3:
            return []
        family = function.module.family
        candidates = [
            method
            for method in self._methods_by_name.get((family, name), [])
            if method.definition.kind is not FunctionKind.CONSTRUCTOR
        ]
        candidates = _by_arity(candidates, call.arg_count)
        if not candidates:
            return []
        receiver = call.receiver or ""
        hint = _normalise_hint(re.split(r"\.|::|->", receiver)[-1]) if receiver != "<expr>" else ""
        if len(hint) >= 3:
            matching = [
                method
                for method in candidates
                if method.cls is not None and _names_related(hint, _normalise_hint(method.cls.name))
            ]
            concrete = [method for method in matching if not method.definition.is_abstract] or matching
            if concrete and len(concrete) <= _MAX_AMBIGUOUS:
                confidence = 0.7 if len(matching) == 1 else 0.6
                return [(method, confidence, "name-match") for method in concrete]
        owners = {id(method.cls) for method in candidates}
        if len(owners) == 1:
            return [(method, 0.5 if hint else 0.4, "unique-method") for method in candidates]
        # One method across a small type hierarchy (interface + implementations) counts as one.
        roots = {id(self._root(method.cls)) for method in candidates if method.cls is not None}
        if len(roots) == 1 and len(candidates) <= _MAX_IMPLEMENTATIONS:
            concrete = [method for method in candidates if not method.definition.is_abstract] or candidates
            return [(method, 0.45, "unique-method") for method in concrete]
        if len(candidates) <= _MAX_AMBIGUOUS:
            return [(method, 0.3, "ambiguous") for method in candidates]
        return []

    def _root(self, cls: ClassInfo) -> ClassInfo:
        ancestors = self.ancestors(cls)
        return ancestors[-1] if ancestors else cls


# ------------------------------------------------------------------------- helpers

_IDENTIFIER_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_$.]{1,62}$")
_HINT_NOISE_RE: Final = re.compile(r"^(?:m_|_+|the|my|p_)|(?:impl|_)+$", re.IGNORECASE)
_SOURCE_EXTENSION_RE: Final = re.compile(
    r"\.(?:d\.ts|tsx?|jsx?|mjs|cjs|mts|cts|py|pyi|java|sql|c|cc|cpp|cxx|h|hh|hpp|hxx|ipp|inl)$", re.IGNORECASE
)


def _strip_extension(path: str) -> str:
    return _SOURCE_EXTENSION_RE.sub("", path)


def _by_arity(candidates: list[FunctionInfo], arg_count: int | None) -> list[FunctionInfo]:
    """Prefer overloads whose parameter count matches; keep all when none does."""
    if arg_count is None or len(candidates) <= 1:
        return candidates
    matching = [
        candidate
        for candidate in candidates
        if len(candidate.definition.params) == arg_count
        or any(param.name.startswith(("*", "...")) for param in candidate.definition.params)
    ]
    return matching or candidates


def _nearest(candidates: list[T], origin: ModuleInfo) -> T:
    """The candidate whose file shares the longest directory prefix with ``origin``."""
    origin_parts = origin.path.split("/")

    def rank(candidate: T) -> tuple[int, int, str]:
        path = candidate.path
        shared = 0
        for left, right in zip(path.split("/"), origin_parts, strict=False):
            if left != right:
                break
            shared += 1
        return (-shared, len(path), path)

    return min(candidates, key=rank)


def _normalise_hint(name: str) -> str:
    return _HINT_NOISE_RE.sub("", name).replace("_", "").lower()


def _names_related(variable: str, class_name: str) -> bool:
    """``accountDao`` ~ ``LegacyAccountDao``; ``dao`` ~ ``AccountDao``; two letters are too vague."""
    if not variable or not class_name:
        return False
    if variable == class_name:
        return True
    shorter, longer = sorted((variable, class_name), key=len)
    return len(shorter) >= 3 and (longer.endswith(shorter) or longer.startswith(shorter))
