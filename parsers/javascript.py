"""JavaScript and TypeScript extractor.

JavaScript has many ways to define the same thing, and legacy code bases use all of
them. Besides ES classes and function declarations this extractor understands

* functions bound by assignment (``const f = () => ...``, ``exports.f = function ...``,
  ``module.exports = { f() {...} }``, ``return { f: function ... }``),
* prototype-style classes (``Foo.prototype.bar = function ...``, ``util.inherits``,
  ``Base.extend({...})``),
* module wrappers (IIFE / UMD / AMD): definitions inside them are module-level,
* handlers registered by a call - Express-style routes and cron schedules become
  synthetic entry-point scopes named after the route,
* NestJS-style decorators, TypeScript parameter properties and type annotations.

Anonymous callbacks belong to the function that contains them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import ClassVar, Final

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language as TsLanguage
from tree_sitter import Node

from parsers.base import (
    Append,
    Assignment,
    Lookup,
    Scope,
    Source,
    StrVal,
    TreeSitterExtractor,
    clean_doc_comment,
    collapse_ws,
    end_line_of,
    flatten_binary,
    leading_comment,
    line_of,
    simple_type,
    unescape,
)
from parsers.ir import (
    MODULE_SCOPE,
    CallSite,
    ClassDef,
    ClassKind,
    EntryPoint,
    FunctionDef,
    FunctionKind,
    ImportDef,
    Language,
    ModuleDef,
    Param,
)

_JS: Final = TsLanguage(tree_sitter_javascript.language())
_TS: Final = TsLanguage(tree_sitter_typescript.language_typescript())
_TSX: Final = TsLanguage(tree_sitter_typescript.language_tsx())

_FUNCTION_DECLS: Final = frozenset({"function_declaration", "generator_function_declaration"})
_FUNCTION_EXPRS: Final = frozenset({"function_expression", "generator_function", "arrow_function"})
_FUNCTION_TYPES: Final = _FUNCTION_DECLS | _FUNCTION_EXPRS | {"method_definition"}
_CLASS_DECLS: Final = frozenset(
    {"class_declaration", "abstract_class_declaration", "interface_declaration", "enum_declaration"}
)
_CLASS_TYPES: Final = _CLASS_DECLS | {"class"}
_CLASS_KINDS: Final = {
    "class_declaration": ClassKind.CLASS,
    "abstract_class_declaration": ClassKind.CLASS,
    "class": ClassKind.CLASS,
    "interface_declaration": ClassKind.INTERFACE,
    "enum_declaration": ClassKind.ENUM,
}
#: Expression wrappers that do not change which value is being produced.
_TRANSPARENT: Final = frozenset(
    {
        "parenthesized_expression", "as_expression", "satisfies_expression", "type_assertion",
        "non_null_expression", "await_expression",
    }
)  # fmt: skip
_THIS_ALIASES: Final = frozenset({"this", "self", "that", "_this", "_self", "vm"})
_GLOBAL_OBJECTS: Final = frozenset({"window", "global", "globalThis"})

_HTTP_VERBS: Final = frozenset({"get", "post", "put", "delete", "patch", "all", "head", "options", "del"})
_SCHEDULE_CALLS: Final = frozenset({"schedule", "scheduleJob"})
_HANDLER_TYPES: Final = _FUNCTION_EXPRS | {"identifier", "member_expression", "call_expression", "array"}
_ROUTER_NAME_RE: Final = re.compile(r"(?:^|[a-z_$])(?:app|router|routes?|server|fastify)$", re.IGNORECASE)
_ROUTER_FACTORIES: Final = frozenset(
    {"express", "Router", "fastify", "createServer", "Hono", "Koa", "KoaRouter"}
)

#: Higher-order calls whose first argument *is* the function being defined:
#: ``const getUser = catchAsync(async (req, res) => ...)``.
_NOT_FUNCTION_WRAPPERS: Final = frozenset(
    {
        "setTimeout", "setInterval", "setImmediate", "require", "define", "describe", "it", "test", "before",
        "after", "beforeEach", "afterEach", "useEffect",
        "useLayoutEffect", "useMemo", "useState", "useReducer",
        "useRef", "$", "jQuery", "Promise", "express", "Router", "createServer",
    }
)  # fmt: skip
_WRAPPER_METHODS: Final = frozenset(
    {
        "memo",
        "forwardRef",
        "debounce",
        "throttle",
        "promisify",
        "memoize",
        "once",
        "wrap",
        "useCallback",
        "curry",
    }
)
_CLASS_FACTORIES: Final = frozenset({"extend", "createClass"})

#: APIs that are handed functions to call later; member expressions passed to them are callbacks.
_CALLBACK_APIS: Final = frozenset(
    {
        "then", "catch", "finally", "forEach", "map", "filter", "reduce", "some", "every", "flatMap", "on",
        "once", "addEventListener", "addListener", "subscribe", "use", "setTimeout", "setInterval",
        "setImmediate", "nextTick",
    }
)  # fmt: skip
_PATTERN_NAMES: Final = frozenset({"identifier", "shorthand_property_identifier_pattern"})
_NOT_CALLBACKS: Final = frozenset(
    {
        "undefined", "null", "NaN", "Infinity", "arguments",
        "module", "exports", "console", "document", "window",
        "process", "global", "globalThis", "__dirname", "__filename",
        "JSON", "Math", "Object", "Array", "Date",
    }
)  # fmt: skip

_STRING_PASSTHROUGH: Final = frozenset({"trim", "trimStart", "trimEnd", "trimLeft", "trimRight", "toString"})
_FORMAT_CALLS: Final = frozenset({"format", "sprintf", "vsprintf"})

_NEST_HTTP: Final = {
    "Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH", "All": "ANY",
    "Head": "HEAD", "Options": "OPTIONS",
}  # fmt: skip
_NEST_SCHEDULE: Final = frozenset({"Cron", "Interval", "Timeout"})
_NEST_MESSAGE: Final = frozenset(
    {
        "MessagePattern",
        "EventPattern",
        "SubscribeMessage",
        "OnEvent",
        "Process",
        "RabbitSubscribe",
        "SqsMessageHandler",
    }
)
_NEST_GRAPHQL: Final = frozenset({"Query", "Mutation", "Subscription", "ResolveField"})
_NEXT_ROUTE_FILE_RE: Final = re.compile(r"(?:^|/)app/(.*?)/?route\.[cm]?[jt]sx?$")
_NEXT_API_FILE_RE: Final = re.compile(r"(?:^|/)pages/(api/.*?)(?:/index)?\.[cm]?[jt]sx?$")
_NEXT_VERBS: Final = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


@dataclass(slots=True)
class _Context:
    """Where the value of an expression ends up: a variable, a property, an export."""

    #: Property path of the binding, global/export prefixes removed. Empty for a default export.
    chain: list[str]
    anchor: Node
    line: int
    exported: bool = False
    default: bool = False
    #: The binding is a member of an object literal.
    member: bool = False
    #: The binding is the class body object of ``Base.extend({...})``.
    class_base: str | None = None
    #: The binding is a direct member of such a class body.
    class_like: bool = False
    #: Base class and declaration of the class a ``class_like`` member belongs to.
    owner_base: str | None = None
    owner_anchor: Node | None = None


@dataclass(slots=True)
class _Name:
    """The name a function or class is known by, derived from its binding context."""

    name: str
    line: int
    anchor: Node
    #: Class the function is attached to (prototype, static or ``this.x`` assignment).
    owner: str | None = None
    #: Object literal the function is a member of (``api`` in ``const api = { f() {} }``).
    namespace: str | None = None
    exported: bool = False
    default: bool = False
    static: bool = False
    on_this: bool = False
    class_base: str | None = None
    owner_anchor: Node | None = None


@dataclass(slots=True)
class _Registration:
    entry: EntryPoint
    label: str
    call: Node
    handlers: list[Node]


@dataclass(slots=True)
class _State:
    src: Source
    module: ModuleDef
    scope: Scope
    classes: dict[str, ClassDef] = field(default_factory=dict)
    implicit: set[str] = field(default_factory=set)
    inherits: dict[str, str] = field(default_factory=dict)
    #: ``this.x`` types assigned inside plain functions, by function qualified name.
    this_types: dict[str, dict[str, str]] = field(default_factory=dict)
    labels: dict[str, int] = field(default_factory=dict)
    is_main: bool = False


class JavaScriptExtractor(TreeSitterExtractor):
    language: ClassVar[Language] = Language.JAVASCRIPT

    CALL_TYPES = frozenset(
        {"call_expression", "new_expression", "jsx_self_closing_element", "jsx_opening_element"}
    )
    STRING_TYPES = frozenset({"string", "template_string", "binary_expression", "call_expression"})
    ASSIGN_TYPES = frozenset(
        {"variable_declarator", "assignment_expression", "augmented_assignment_expression"}
    )
    APPEND_TYPES = frozenset({"call_expression"})
    DECL_TYPES = frozenset({"variable_declarator", "assignment_expression"})
    BRANCH_TYPES = frozenset(
        {
            "if_statement", "for_statement", "for_in_statement", "while_statement", "do_statement",
            "catch_clause", "ternary_expression", "switch_case",
        }
    )  # fmt: skip
    NESTED_TYPES = _FUNCTION_TYPES | _CLASS_TYPES | {"call_expression"}
    REF_TYPES = frozenset({"identifier", "member_expression"})
    BINDING_TYPES = _FUNCTION_EXPRS | {
        "variable_declarator",
        "catch_clause",
        "for_in_statement",
        "method_definition",
    }
    SKIP_TYPES = frozenset(
        {
            "comment", "import_statement", "hash_bang_line", "decorator", "type_annotation", "type_arguments",
            "type_parameters", "type_alias_declaration", "ambient_declaration", "function_signature",
            "jsx_closing_element", "regex",
        }
    )  # fmt: skip

    def grammar(self, path: str) -> TsLanguage:
        return _JS

    # ------------------------------------------------------------------ module

    def extract_module(self, src: Source, root: Node, module: ModuleDef) -> None:
        state = _State(src, module, Scope())
        src.names["routers"] = set()
        first = root.named_children[0] if root.named_child_count else None
        if first is not None and first.type == "hash_bang_line":
            state.is_main = True
            first = first.next_named_sibling
        if first is not None and first.type == "comment" and src.text(first).startswith("/*"):
            module.docstring = clean_doc_comment(src.text(first))

        for statement in self._module_statements(src, root):
            self._prepass(state, statement)
        # ``const { handler } = require('./x')`` binds a function defined elsewhere, not a local value.
        src.names["imports"] = {imported.alias for imported in module.imports if imported.alias}

        script = FunctionDef(
            name=MODULE_SCOPE,
            qualified_name=MODULE_SCOPE,
            kind=FunctionKind.SCRIPT,
            start_line=1,
            end_line=max(module.line_count, 1),
            signature="module-level code",
        )
        nested = self.visit_body(src, script, [root], state.scope)
        self._emit_nested(state, nested, None)
        if state.is_main:
            script.entry_point = EntryPoint("main")
        if script.calls or script.sql or state.is_main:
            script.local_types.clear()
            module.functions.append(script)
        self._finish_implicit_classes(state)

    def _module_statements(self, src: Source, root: Node) -> Iterator[Node]:
        """Top-level statements, looking through module wrappers (IIFE, UMD, AMD ``define``)."""
        stack: list[tuple[Node, int]] = [(child, 0) for child in reversed(root.named_children)]
        while stack:
            node, depth = stack.pop()
            yield node
            if node.type == "export_statement":
                declaration = node.child_by_field_name("declaration")
                if declaration is not None:
                    stack.append((declaration, depth))
            elif node.type == "expression_statement" and depth < 3:
                for body in self._wrapper_bodies(node):
                    stack.extend((child, depth + 1) for child in reversed(body.named_children))

    def _wrapper_bodies(self, statement: Node) -> list[Node]:
        expression = statement.named_children[0] if statement.named_child_count else None
        while expression is not None and expression.type in ("parenthesized_expression", "unary_expression"):
            expression = expression.named_children[0] if expression.named_child_count else None
        if expression is None or expression.type != "call_expression":
            return []
        candidates: list[Node] = []
        callee = expression.child_by_field_name("function")
        if callee is not None:
            if callee.type == "member_expression":  # (function () { ... }).call(this)
                callee = callee.child_by_field_name("object")
            while (
                callee is not None and callee.type == "parenthesized_expression" and callee.named_child_count
            ):
                callee = callee.named_children[0]
            if callee is not None:
                candidates.append(callee)
        arguments = expression.child_by_field_name("arguments")
        if arguments is not None:
            candidates.extend(arguments.named_children)
        bodies: list[Node] = []
        for candidate in candidates:
            if candidate.type in _FUNCTION_EXPRS:
                body = candidate.child_by_field_name("body")
                if body is not None and body.type == "statement_block":
                    bodies.append(body)
        return bodies

    def _prepass(self, state: _State, statement: Node) -> None:
        """Imports, constants and inheritance idioms - needed before any body is analysed."""
        src = state.src
        kind = statement.type
        if kind == "import_statement":
            state.module.imports.extend(self._imports(src, statement))
        elif kind in ("lexical_declaration", "variable_declaration"):
            for declarator in statement.named_children:
                if declarator.type == "variable_declarator":
                    self._prepass_declarator(state, declarator)
        elif kind == "export_statement":
            self._prepass_export(state, statement)
        elif kind == "if_statement":
            condition = statement.child_by_field_name("condition")
            if condition is not None and "require.main" in src.text(condition):
                state.is_main = True
        elif kind == "expression_statement" and statement.named_child_count:
            expression = statement.named_children[0]
            if expression.type == "assignment_expression":
                self._prepass_assignment(state, expression)
            elif expression.type == "call_expression":
                module = self._required_module(src, expression)
                if module is not None:
                    state.module.imports.append(ImportDef(module, None, None, line_of(expression)))
                self._prepass_inherits_call(state, expression)

    def _prepass_declarator(self, state: _State, declarator: Node) -> None:
        src = state.src
        name = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name is None or value is None:
            return
        line = line_of(declarator)
        required = self._require_source(src, value)
        if required is not None:
            module, member = required
            if name.type == "identifier":
                state.module.imports.append(ImportDef(module, member, src.text(name), line))
            elif name.type == "object_pattern" and member is None:
                for item in name.named_children:
                    if item.type == "shorthand_property_identifier_pattern":
                        state.module.imports.append(ImportDef(module, src.text(item), src.text(item), line))
                    elif item.type == "pair_pattern":
                        key = item.child_by_field_name("key")
                        alias = item.child_by_field_name("value")
                        if key is not None and alias is not None and alias.type == "identifier":
                            state.module.imports.append(
                                ImportDef(module, src.text(key), src.text(alias), line)
                            )
            return
        if name.type != "identifier":
            return
        target = src.text(name)
        if self._is_router_factory(src, value):
            src.names["routers"].add(target)
            return
        folded = self.eval_string(src, value, state.scope.lookup)
        if folded is not None and folded.literal:
            definition = state.scope.define(target, folded, line, end_line_of(declarator))
            if definition is not None:
                state.module.constants[target] = definition
            # Defining a query is not running it: keep it out of the module scope.
            src.skip_ids.add(declarator.id)

    def _prepass_export(self, state: _State, statement: Node) -> None:
        src = state.src
        source = statement.child_by_field_name("source")
        line = line_of(statement)
        if source is not None:  # re-exports: export { a, b as c } from './x'; export * from './x'
            module = self._plain_string(src, source)
            if module is None:
                return
            clause = next((c for c in statement.named_children if c.type == "export_clause"), None)
            if clause is None:
                state.module.imports.append(ImportDef(module, "*", None, line))
                return
            for specifier in clause.named_children:
                original = specifier.child_by_field_name("name")
                alias = specifier.child_by_field_name("alias") or original
                if original is not None and alias is not None:
                    state.module.imports.append(ImportDef(module, src.text(original), src.text(alias), line))
            return
        value = statement.child_by_field_name("value")
        if value is not None and any(child.type == "default" for child in statement.children):
            exported = self._exported_symbol(src, value)
            if exported is not None:
                state.module.default_export = exported

    def _prepass_assignment(self, state: _State, assignment: Node) -> None:
        src = state.src
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or right is None:
            return
        chain = self._chain(src, left)
        if chain is None:
            return
        if chain in (["module", "exports"], ["exports", "default"], ["module", "exports", "default"]):
            exported = self._exported_symbol(src, right)
            if exported is not None:
                state.module.default_export = exported
        elif len(chain) == 2 and chain[1] == "prototype":
            # Foo.prototype = Object.create(Base.prototype) / new Base()
            base: str | None = None
            if right.type == "new_expression":
                constructor = right.child_by_field_name("constructor")
                base = simple_type(src.text(constructor)) if constructor is not None else None
            elif right.type == "call_expression":
                arguments = right.child_by_field_name("arguments")
                callee = right.child_by_field_name("function")
                if arguments is not None and callee is not None and src.text(callee) == "Object.create":
                    target = (
                        self._chain(src, arguments.named_children[0]) if arguments.named_child_count else None
                    )
                    if target and len(target) >= 2 and target[-1] == "prototype":
                        base = target[-2]
            if base and base != chain[0]:
                state.inherits[chain[0]] = base

    def _prepass_inherits_call(self, state: _State, call: Node) -> None:
        src = state.src
        callee = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if callee is None or arguments is None or arguments.named_child_count != 2:
            return
        name = src.text(callee).rsplit(".", 1)[-1]
        if name not in ("inherits", "__extends", "setPrototypeOf"):
            return
        child, parent = (self._chain(src, argument) for argument in arguments.named_children)
        if child and parent:
            derived = child[0]
            base = parent[-2] if parent[-1] == "prototype" and len(parent) >= 2 else parent[-1]
            if derived != base:
                state.inherits[derived] = base

    def _exported_symbol(self, src: Source, value: Node) -> str | None:
        """``X`` for ``module.exports = X`` and ``module.exports = new X()``."""
        if value.type == "identifier":
            return src.text(value)
        if value.type == "new_expression":
            constructor = value.child_by_field_name("constructor")
            return simple_type(src.text(constructor)) if constructor is not None else None
        return None

    def _imports(self, src: Source, node: Node) -> list[ImportDef]:
        source = node.child_by_field_name("source")
        module = self._plain_string(src, source) if source is not None else None
        if module is None:
            return []
        line = line_of(node)
        imports: list[ImportDef] = []
        clause = next((child for child in node.named_children if child.type == "import_clause"), None)
        if clause is None:
            return [ImportDef(module, None, None, line)]  # side-effect import
        for child in clause.named_children:
            if child.type == "identifier":
                imports.append(ImportDef(module, "default", src.text(child), line))
            elif child.type == "namespace_import":
                alias = next((c for c in child.named_children if c.type == "identifier"), None)
                if alias is not None:
                    imports.append(ImportDef(module, "*", src.text(alias), line))
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    original = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias") or original
                    if original is not None and alias is not None:
                        imports.append(ImportDef(module, src.text(original), src.text(alias), line))
        return imports

    def _required_module(self, src: Source, call: Node) -> str | None:
        callee = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if (
            callee is None
            or arguments is None
            or callee.type != "identifier"
            or src.text(callee) != "require"
        ):
            return None
        if arguments.type != "arguments" or arguments.named_child_count != 1:
            return None
        return self._plain_string(src, arguments.named_children[0])

    def _require_source(self, src: Source, value: Node) -> tuple[str, str | None] | None:
        """``(module, member)`` for ``require('m')``, ``require('m').member`` and ``require('m')(...)``."""
        member: str | None = None
        current = value
        for _ in range(3):
            if current.type == "call_expression":
                module = self._required_module(src, current)
                if module is not None:
                    return module, member
                callee = current.child_by_field_name("function")
                if callee is None:
                    return None
                current = callee
            elif current.type == "member_expression":
                prop = current.child_by_field_name("property")
                target = current.child_by_field_name("object")
                if prop is None or target is None:
                    return None
                member = src.text(prop)
                current = target
            else:
                return None
        return None

    def _is_router_factory(self, src: Source, value: Node) -> bool:
        node = value
        if node.type == "new_expression":
            constructor = node.child_by_field_name("constructor")
            return constructor is not None and src.text(constructor).rsplit(".", 1)[-1] in _ROUTER_FACTORIES
        if node.type != "call_expression":
            return False
        callee = node.child_by_field_name("function")
        return callee is not None and src.text(callee).rsplit(".", 1)[-1] in _ROUTER_FACTORIES

    # ------------------------------------------------------------------ naming

    def _chain(self, src: Source, node: Node) -> list[str] | None:
        """``a.b.c`` as ``["a", "b", "c"]``; ``None`` for anything that is not a plain property path."""
        parts: list[str] = []
        current: Node | None = node
        for _ in range(8):
            if current is None:
                return None
            kind = current.type
            if kind in ("identifier", "this", "super", "type_identifier"):
                parts.append(src.text(current))
                parts.reverse()
                return parts
            if kind == "member_expression":
                prop = current.child_by_field_name("property")
                if prop is None or prop.type not in ("property_identifier", "private_property_identifier"):
                    return None
                parts.append(src.text(prop))
                current = current.child_by_field_name("object")
            elif kind in _TRANSPARENT and current.named_child_count:
                current = current.named_children[-1 if kind == "type_assertion" else 0]
            else:
                return None
        return None

    def _key_name(self, src: Source, key: Node | None) -> str | None:
        if key is None:
            return None
        if key.type in ("property_identifier", "private_property_identifier", "identifier"):
            return src.text(key)
        if key.type == "string":
            return self._plain_string(src, key)
        return None

    def _context(self, src: Source, node: Node, depth: int = 0) -> _Context | None:
        child = node
        parent = node.parent
        while parent is not None and parent.type in _TRANSPARENT:
            child, parent = parent, parent.parent
        if parent is None or depth > 4:
            return None
        kind = parent.type

        if kind == "variable_declarator":
            name = parent.child_by_field_name("name")
            value = parent.child_by_field_name("value")
            if name is None or value is None or value.id != child.id or name.type != "identifier":
                return None
            declaration = parent.parent
            anchor = parent
            exported = False
            if declaration is not None and declaration.named_child_count == 1:
                anchor = declaration
                if declaration.parent is not None and declaration.parent.type == "export_statement":
                    anchor = declaration.parent
                    exported = True
            return _Context([src.text(name)], anchor, line_of(name), exported=exported)

        if kind == "assignment_expression":
            left = parent.child_by_field_name("left")
            right = parent.child_by_field_name("right")
            if left is None or right is None or right.id != child.id:
                return None
            chain = self._chain(src, left)
            if chain is None:
                return None
            exported = False
            if chain[:2] == ["module", "exports"]:
                chain, exported = chain[2:], True
            elif chain[0] == "exports":
                chain, exported = chain[1:], True
            elif chain[0] in _GLOBAL_OBJECTS and len(chain) > 1:
                chain = chain[1:]
            statement = parent.parent
            anchor = (
                statement if statement is not None and statement.type == "expression_statement" else parent
            )
            default = exported and chain in ([], ["default"])
            return _Context(
                [] if default else chain, anchor, line_of(left), exported=exported, default=default
            )

        if kind == "export_statement":
            return _Context([], parent, line_of(node), exported=True, default=True)

        if kind == "pair":
            value = parent.child_by_field_name("value")
            key = self._key_name(src, parent.child_by_field_name("key"))
            holder = parent.parent
            if (
                value is None
                or value.id != child.id
                or key is None
                or holder is None
                or holder.type != "object"
            ):
                return None
            outer = self._context(src, holder, depth + 1)
            if outer is None:
                return None
            return self._member_context(outer, key, parent)

        if kind == "return_statement" and node.type == "object":
            return _Context([], parent, line_of(node))

        if kind == "arguments":
            call = parent.parent
            if call is None or call.type != "call_expression":
                return None
            callee = call.child_by_field_name("function")
            if callee is None:
                return None
            callee_name = (
                src.text(callee).rsplit(".", 1)[-1] if callee.end_byte - callee.start_byte < 80 else ""
            )
            arguments = parent.named_children
            if (
                node.type == "object"
                and callee.type == "member_expression"
                and callee_name in _CLASS_FACTORIES
            ):
                outer = self._context(src, call, depth + 1)
                target = callee.child_by_field_name("object")
                base = simple_type(src.text(target)) if target is not None else None
                if outer is None or not outer.chain:
                    return None
                return _Context(
                    outer.chain,
                    outer.anchor,
                    outer.line,
                    exported=outer.exported,
                    class_base=base or "Object",
                )
            is_wrapper = (
                callee.type == "identifier" and callee_name not in _NOT_FUNCTION_WRAPPERS
            ) or callee_name in _WRAPPER_METHODS
            if node.type in _FUNCTION_EXPRS and arguments and arguments[0].id == child.id and is_wrapper:
                return self._context(src, call, depth + 1)
        return None

    @staticmethod
    def _member_context(outer: _Context, key: str, anchor: Node) -> _Context:
        """Context of the member ``key`` of an object literal whose own context is ``outer``."""
        in_class_body = outer.class_base is not None
        return _Context(
            [*outer.chain, key],
            anchor,
            line_of(anchor),
            exported=outer.exported,
            member=True,
            class_like=in_class_body,
            owner_base=outer.class_base,
            owner_anchor=outer.anchor if in_class_body else None,
        )

    def _name_of(self, src: Source, node: Node) -> _Name | None:
        """The binding name of a function or class node, or ``None`` when it is anonymous."""
        kind = node.type
        own = node.child_by_field_name("name")
        if kind in _FUNCTION_DECLS or kind in _CLASS_DECLS:
            parent = node.parent
            exported = parent is not None and parent.type == "export_statement"
            default = exported and parent is not None and any(c.type == "default" for c in parent.children)
            anchor = parent if exported and parent is not None else node
            if own is None:
                return (
                    _Name(_stem_name(src.path), line_of(node), anchor, exported=True, default=True)
                    if default
                    else None
                )
            return _Name(src.text(own), line_of(own), anchor, exported=exported, default=default)

        if kind == "method_definition":
            holder = node.parent
            key = self._key_name(src, own)
            if holder is None or holder.type != "object" or key is None:
                return None
            outer = self._context(src, holder)
            if outer is None:
                return None
            return self._name_from_context(src, self._member_context(outer, key, node), None)

        context = self._context(src, node)
        if context is None:
            return None
        own_name = src.text(own) if own is not None else None
        return self._name_from_context(src, context, own_name)

    def _name_from_context(self, src: Source, context: _Context, own_name: str | None) -> _Name | None:
        chain = context.chain
        if not chain:
            if not context.default:
                return None
            name = own_name or _stem_name(src.path)
            return _Name(name, context.line, context.anchor, exported=True, default=True)
        name = chain[-1]
        prefix = chain[:-1]
        result = _Name(
            name,
            context.line,
            context.anchor,
            exported=context.exported,
            class_base=context.owner_base,
            owner_anchor=context.owner_anchor,
        )
        if not prefix:
            return result
        if prefix[0] in _THIS_ALIASES:
            if len(prefix) != 1:
                return None
            result.on_this = True
            return result
        if prefix[-1] == "prototype" and len(prefix) >= 2:
            result.owner = prefix[-2]
        elif context.class_like:
            result.owner = prefix[-1]
        elif len(prefix) == 1 and not context.member and prefix[0][:1].isupper():
            result.owner = prefix[0]
            result.static = True
        else:
            result.namespace = ".".join(prefix)
        return result

    def is_nested_scope(self, src: Source, node: Node) -> bool:
        if not node.is_named:
            return False
        if node.type == "call_expression":
            return self._registrations(src, node) is not None
        return self._name_of(src, node) is not None

    # ------------------------------------------------------------- definitions

    def _emit_nested(self, state: _State, nodes: list[Node], parent: FunctionDef | None) -> None:
        src = state.src
        for node in nodes:
            if node.type == "call_expression":
                for registration in self._registrations(src, node) or ():
                    self._registration(state, registration)
                continue
            name = self._name_of(src, node)
            if name is None:
                continue
            if node.type in _CLASS_TYPES:
                self._class(state, node, name, parent)
            else:
                self._named_function(state, node, name, parent)

    def _ensure_class(self, state: _State, name: str, node: Node) -> ClassDef:
        cls = state.classes.get(name)
        if cls is None:
            cls = ClassDef(
                name=name,
                qualified_name=name,
                kind=ClassKind.CLASS,
                start_line=line_of(node),
                end_line=end_line_of(node),
                name_line=line_of(node),
            )
            state.classes[name] = cls
            state.implicit.add(name)
            state.module.classes.append(cls)
        return cls

    def _named_function(self, state: _State, node: Node, name: _Name, parent: FunctionDef | None) -> None:
        src = state.src
        cls: ClassDef | None = None
        owner = name.owner
        if name.on_this and parent is not None:
            if parent.owner is not None:
                owner = parent.owner
            elif parent.parent_function is None and parent.kind is FunctionKind.FUNCTION:
                owner = parent.name
        if owner is not None:
            cls = self._ensure_class(state, owner, name.owner_anchor or name.anchor)
            if name.class_base and name.class_base not in cls.bases and owner in state.implicit:
                cls.bases.append(name.class_base)
            qualified = f"{cls.qualified_name}.{name.name}"
            parent = None
        elif name.namespace is not None:
            qualified = f"{name.namespace}.{name.name}"
            parent = None
        elif parent is not None:
            qualified = f"{parent.qualified_name}.{name.name}"
        else:
            qualified = name.name

        anchor = name.anchor
        body = node.child_by_field_name("body")
        modifiers = self._function_modifiers(node)
        if name.static:
            modifiers.append("static")
        if name.exported:
            modifiers.append("export")
        fn = FunctionDef(
            name=name.name,
            qualified_name=qualified,
            kind=FunctionKind.METHOD if cls is not None else FunctionKind.FUNCTION,
            start_line=line_of(anchor),
            end_line=end_line_of(anchor),
            signature=self._signature(src, node, body, name.name if node.type in _FUNCTION_EXPRS else None),
            params=self._params(src, node),
            return_type=self._return_type(src, node),
            docstring=leading_comment(src, anchor),
            modifiers=modifiers,
            owner=cls.qualified_name if cls is not None else None,
            parent_function=parent.qualified_name if parent is not None else None,
            name_line=name.line,
        )
        if name.default:
            state.module.default_export = qualified
        fn.entry_point = self._function_entry_point(src, fn, name)
        self._analyse(state, fn, node, state.scope, cls)
        (cls.methods if cls is not None else state.module.functions).append(fn)

    def _analyse(
        self, state: _State, fn: FunctionDef, node: Node, scope: Scope, cls: ClassDef | None
    ) -> None:
        """Visit a function's body and emit whatever is defined inside it."""
        src = state.src
        for param in fn.params:
            if param.type and param.name.isidentifier():
                fn.local_types[param.name] = param.type
        body = node.child_by_field_name("body")
        nested = self.visit_body(src, fn, [body], scope) if body is not None else []
        this_types = {
            key[5:]: fn.local_types.pop(key) for key in [k for k in fn.local_types if k.startswith("this.")]
        }
        resolved: dict[str, str] = {}
        for attribute, type_name in this_types.items():
            actual = fn.local_types.get(type_name[1:]) if type_name.startswith("@") else type_name
            if actual and not actual.startswith("@"):
                resolved[attribute] = actual
        for key in [key for key, value in fn.local_types.items() if value.startswith("@")]:
            del fn.local_types[key]
        if cls is not None:
            for attribute, type_name in resolved.items():
                cls.fields.setdefault(attribute, type_name)
        elif resolved:
            state.this_types[fn.qualified_name] = resolved
        self._emit_nested(state, nested, fn)

    def _function_modifiers(self, node: Node) -> list[str]:
        modifiers: list[str] = []
        for child in node.children:
            if child.is_named:
                if child.type in ("accessibility_modifier", "override_modifier"):
                    modifiers.append(child.text.decode() if child.text else child.type)
                continue
            if child.type in ("async", "static", "get", "set", "abstract", "readonly", "*"):
                modifiers.append("generator" if child.type == "*" else child.type)
        return modifiers

    def _signature(self, src: Source, node: Node, body: Node | None, bound_name: str | None) -> str:
        end = body.start_byte if body is not None else node.end_byte
        text = collapse_ws(src.slice(node.start_byte, end)).rstrip("{ ").rstrip()
        return f"{bound_name} = {text}" if bound_name else text

    def _return_type(self, src: Source, node: Node) -> str | None:
        annotation = node.child_by_field_name("return_type")
        if annotation is None:
            return None
        return collapse_ws(src.text(annotation).lstrip(": "), 80)

    def _params(self, src: Source, node: Node) -> list[Param]:
        single = node.child_by_field_name("parameter")
        if single is not None:
            return [Param(src.text(single))]
        holder = node.child_by_field_name("parameters")
        params: list[Param] = []
        if holder is None:
            return params
        for child in holder.named_children:
            kind = child.type
            type_name: str | None = None
            pattern: Node | None = child
            if kind in ("required_parameter", "optional_parameter"):
                pattern = child.child_by_field_name("pattern")
                annotation = child.child_by_field_name("type")
                if annotation is not None and annotation.named_child_count:
                    type_name = simple_type(src.text(annotation.named_children[0]))
            if pattern is None or pattern.type == "this":
                continue
            if pattern.type == "assignment_pattern":
                pattern = pattern.child_by_field_name("left") or pattern
            if pattern.type == "identifier":
                params.append(Param(src.text(pattern), type_name))
            elif pattern.type == "rest_pattern" or pattern.type in ("object_pattern", "array_pattern"):
                params.append(Param(collapse_ws(src.text(pattern), 40), type_name))
        return params

    def _function_entry_point(self, src: Source, fn: FunctionDef, name: _Name) -> EntryPoint | None:
        if not name.exported or fn.owner is not None:
            return None
        route_file = _NEXT_ROUTE_FILE_RE.search(src.path)
        if route_file is not None and fn.name in _NEXT_VERBS:
            return EntryPoint("http", method=fn.name, route="/" + route_file.group(1).strip("/"))
        api_file = _NEXT_API_FILE_RE.search(src.path)
        if api_file is not None and name.default:
            return EntryPoint("http", method="ANY", route="/" + api_file.group(1))
        if fn.name in ("handler", "lambdaHandler") and fn.params and len(fn.params) <= 3:
            return EntryPoint("message", detail="exported handler (serverless function)")
        return None

    # ------------------------------------------------------------------ classes

    def _class(self, state: _State, node: Node, name: _Name, parent: FunctionDef | None) -> None:
        src = state.src
        anchor = name.anchor
        decorators = [child for child in anchor.children if child.type == "decorator"]
        if anchor.id != node.id:
            decorators.extend(child for child in node.children if child.type == "decorator")
        cls = ClassDef(
            name=name.name,
            qualified_name=name.name,
            kind=_CLASS_KINDS[node.type],
            start_line=line_of(anchor),
            end_line=end_line_of(anchor),
            docstring=leading_comment(src, anchor),
            decorators=[self._decorator_name(src, decorator) for decorator in decorators],
            modifiers=(["abstract"] if node.type == "abstract_class_declaration" else [])
            + (["export"] if name.exported else []),
            name_line=name.line,
        )
        if name.default:
            state.module.default_export = cls.qualified_name
        for child in node.children:
            if child.type == "class_heritage":
                self._heritage(src, child, cls)
            elif child.type == "extends_type_clause":  # interface A extends B, C
                cls.bases.extend(filter(None, (simple_type(src.text(base)) for base in child.named_children)))
        state.classes[cls.name] = cls
        state.module.classes.append(cls)
        body = node.child_by_field_name("body")
        if body is None or cls.kind is ClassKind.ENUM:
            return

        scope = Scope(state.scope, cls.name)
        members: list[tuple[Node, list[Node]]] = []
        pending: list[Node] = []
        for member in body.named_children:
            if member.type == "decorator":
                pending.append(member)
                continue
            members.append(
                (member, pending + [child for child in member.children if child.type == "decorator"])
            )
            pending = []

        # Pass 1: fields and constants, so method bodies can see constants declared below them.
        arrow_methods: list[tuple[Node, Node, Node, list[Node]]] = []
        for member, member_decorators in members:
            if member.type not in ("field_definition", "public_field_definition"):
                continue
            key = member.child_by_field_name("name") or member.child_by_field_name("property")
            field_name = self._key_name(src, key)
            if key is None or field_name is None:
                continue
            value = member.child_by_field_name("value")
            annotation = member.child_by_field_name("type")
            type_name = (
                simple_type(src.text(annotation.named_children[0]))
                if annotation is not None and annotation.named_child_count
                else None
            )
            if value is not None:
                if value.type in _FUNCTION_EXPRS:
                    arrow_methods.append((member, key, value, member_decorators))
                    continue
                type_name = type_name or self._constructed_type(src, value)
                folded = self.eval_string(src, value, scope.lookup)
                if folded is not None and folded.literal:
                    definition = scope.define(field_name, folded, line_of(member), end_line_of(member))
                    if definition is not None:
                        cls.constants[field_name] = definition
            if type_name:
                cls.fields[field_name] = type_name

        # Pass 2: behaviour.
        static_blocks = 0
        for member, member_decorators in members:
            kind = member.type
            if kind == "method_definition" or kind in ("abstract_method_signature", "method_signature"):
                key = member.child_by_field_name("name")
                method_name = self._key_name(src, key)
                if key is not None and method_name is not None:
                    self._method(
                        state, cls, scope, member, member, key, method_name, member_decorators, decorators
                    )
            elif kind == "class_static_block":
                static_blocks += 1
                block = FunctionDef(
                    name="<static>",
                    qualified_name=f"{cls.qualified_name}.<static>"
                    + ("" if static_blocks == 1 else f"#{static_blocks}"),
                    kind=FunctionKind.SCRIPT,
                    start_line=line_of(member),
                    end_line=end_line_of(member),
                    signature="static { … }",
                    owner=cls.qualified_name,
                    modifiers=["static"],
                )
                self._emit_nested(state, self.visit_body(src, block, [member], scope), block)
                if block.calls or block.sql:
                    cls.methods.append(block)
        for member, key, value, member_decorators in arrow_methods:
            self._method(state, cls, scope, member, value, key, src.text(key), member_decorators, decorators)

    def _heritage(self, src: Source, node: Node, cls: ClassDef) -> None:
        clauses = [
            child for child in node.named_children if child.type in ("extends_clause", "implements_clause")
        ]
        if not clauses:  # JavaScript: class_heritage holds the expression itself
            for child in node.named_children:
                base = simple_type(src.text(child))
                if base:
                    cls.bases.append(base)
            return
        for clause in clauses:
            target = cls.bases if clause.type == "extends_clause" else cls.interfaces
            for child in clause.named_children:
                if child.type == "type_arguments":
                    continue
                base = simple_type(src.text(child))
                if base:
                    target.append(base)

    def _method(
        self,
        state: _State,
        cls: ClassDef,
        scope: Scope,
        member: Node,
        function: Node,
        key: Node,
        name: str,
        decorators: list[Node],
        class_decorators: list[Node],
    ) -> None:
        src = state.src
        body = function.child_by_field_name("body")
        is_constructor = name == "constructor" and member.type == "method_definition"
        modifiers = self._function_modifiers(member)
        start = min([line_of(member), *(line_of(decorator) for decorator in decorators)])
        fn = FunctionDef(
            name=name,
            qualified_name=f"{cls.qualified_name}.{name}",
            kind=FunctionKind.CONSTRUCTOR if is_constructor else FunctionKind.METHOD,
            start_line=start,
            end_line=end_line_of(member),
            signature=self._signature(src, member, body, None),
            params=self._params(src, function),
            return_type=self._return_type(src, function),
            docstring=leading_comment(
                src, decorators[0] if decorators and decorators[0].parent == member.parent else member
            ),
            decorators=[self._decorator_name(src, decorator) for decorator in decorators],
            modifiers=modifiers,
            is_abstract=body is None,
            owner=cls.qualified_name,
            name_line=line_of(key),
        )
        if is_constructor:
            self._parameter_properties(src, function, cls)
        fn.entry_point = self._method_entry_point(src, fn, decorators, class_decorators)
        self._analyse(state, fn, function, scope, cls)
        cls.methods.append(fn)

    def _parameter_properties(self, src: Source, function: Node, cls: ClassDef) -> None:
        """TypeScript: ``constructor(private readonly svc: OrderService)`` declares a field."""
        holder = function.child_by_field_name("parameters")
        if holder is None:
            return
        for child in holder.named_children:
            if child.type not in ("required_parameter", "optional_parameter"):
                continue
            is_property = any(
                c.type in ("accessibility_modifier", "readonly", "override_modifier") for c in child.children
            )
            pattern = child.child_by_field_name("pattern")
            annotation = child.child_by_field_name("type")
            if not is_property or pattern is None or annotation is None or not annotation.named_child_count:
                continue
            type_name = simple_type(src.text(annotation.named_children[0]))
            if type_name and pattern.type == "identifier":
                cls.fields[src.text(pattern)] = type_name

    def _decorator_call(self, src: Source, decorator: Node) -> tuple[str, list[Node]]:
        expression = decorator.named_children[0] if decorator.named_child_count else None
        if expression is None:
            return "", []
        if expression.type == "call_expression":
            callee = expression.child_by_field_name("function")
            arguments = expression.child_by_field_name("arguments")
            name = src.text(callee).rsplit(".", 1)[-1] if callee is not None else ""
            return name, (arguments.named_children if arguments is not None else [])
        return src.text(expression).rsplit(".", 1)[-1], []

    def _decorator_name(self, src: Source, decorator: Node) -> str:
        return self._decorator_call(src, decorator)[0]

    def _method_entry_point(
        self, src: Source, fn: FunctionDef, decorators: list[Node], class_decorators: list[Node]
    ) -> EntryPoint | None:
        if not decorators:
            return None
        class_calls = dict(self._decorator_call(src, decorator) for decorator in class_decorators)
        for decorator in decorators:
            name, arguments = self._decorator_call(src, decorator)
            first = self._plain_string(src, arguments[0]) if arguments else None
            if name in _NEST_HTTP and ("Controller" in class_calls or "RestController" in class_calls):
                controller = class_calls.get("Controller") or class_calls.get("RestController") or []
                prefix = self._plain_string(src, controller[0]) if controller else None
                return EntryPoint("http", method=_NEST_HTTP[name], route=_join_route(prefix, first))
            if name in _NEST_SCHEDULE:
                return EntryPoint("scheduled", detail=collapse_ws(src.text(decorator), 80))
            if name in _NEST_MESSAGE:
                return EntryPoint("message", detail=first or name)
            if name in _NEST_GRAPHQL and "Resolver" in class_calls:
                return EntryPoint("http", method="GRAPHQL", route=fn.name)
        return None

    def _finish_implicit_classes(self, state: _State) -> None:
        """Turn ``function Foo() {}`` + ``Foo.prototype.x = ...`` into a class with a constructor."""
        module = state.module
        for name in state.implicit:
            cls = state.classes[name]
            base = state.inherits.get(name)
            if base and base not in cls.bases:
                cls.bases.append(base)
            constructor = next(
                (
                    fn
                    for fn in module.functions
                    if fn.qualified_name == name
                    and fn.kind is FunctionKind.FUNCTION
                    and fn.parent_function is None
                ),
                None,
            )
            if constructor is None:
                continue
            module.functions.remove(constructor)
            constructor.kind = FunctionKind.CONSTRUCTOR
            constructor.name = "constructor"
            constructor.qualified_name = f"{name}.constructor"
            constructor.owner = cls.qualified_name
            cls.methods.insert(0, constructor)
            cls.start_line = constructor.start_line
            cls.end_line = constructor.end_line
            cls.name_line = constructor.name_line
            cls.docstring = constructor.docstring
            for attribute, type_name in state.this_types.get(name, {}).items():
                cls.fields.setdefault(attribute, type_name)
            for fn in module.functions:
                if fn.parent_function == name:
                    fn.parent_function = constructor.qualified_name
        for name, base in state.inherits.items():
            derived = state.classes.get(name)
            if derived is not None and base not in derived.bases:
                derived.bases.append(base)

    # ------------------------------------------------- registered entry points

    def _registrations(self, src: Source, node: Node) -> list[_Registration] | None:
        """Route / schedule registrations made by ``node``, or ``None`` if it is an ordinary call."""
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            return None
        prop = callee.child_by_field_name("property")
        target = callee.child_by_field_name("object")
        arguments = node.child_by_field_name("arguments")
        if prop is None or target is None or arguments is None or arguments.type != "arguments":
            return None
        verb = src.text(prop)
        args = arguments.named_children

        if verb in _SCHEDULE_CALLS:
            if len(args) < 2 or args[-1].type not in _HANDLER_TYPES:
                return None
            expression = self._plain_string(src, args[0])
            if expression is None:
                return None
            return [
                _Registration(
                    EntryPoint("scheduled", detail=expression), f"schedule {expression}", node, args[1:]
                )
            ]

        if verb not in _HTTP_VERBS or not args or args[-1].type not in _HANDLER_TYPES:
            return None
        method = "DELETE" if verb == "del" else "ANY" if verb == "all" else verb.upper()

        if target.type == "call_expression":  # router.route('/x').get(a).post(b)
            registrations: list[_Registration] = []
            pending: list[tuple[str, Node, list[Node]]] = [(method, node, args)]
            current: Node | None = target
            path: str | None = None
            while current is not None and current.type == "call_expression" and len(pending) < 12:
                inner = current.child_by_field_name("function")
                inner_arguments = current.child_by_field_name("arguments")
                if inner is None or inner.type != "member_expression" or inner_arguments is None:
                    break
                inner_prop = inner.child_by_field_name("property")
                inner_args = inner_arguments.named_children
                inner_name = src.text(inner_prop) if inner_prop is not None else ""
                if inner_name == "route" and len(inner_args) == 1:
                    path = self._plain_string(src, inner_args[0])
                    break
                if inner_name not in _HTTP_VERBS or not inner_args:
                    break
                pending.append((inner_name.upper(), current, inner_args))
                current = inner.child_by_field_name("object")
            if path is None:
                return None
            for chained_method, call, handlers in reversed(pending):
                entry = EntryPoint("http", method=chained_method, route=path)
                registrations.append(_Registration(entry, f"{chained_method} {path}", call, handlers))
            return registrations

        if len(args) < 2:
            return None
        route = self._plain_string(src, args[0])
        if route is None:
            return None
        chain = self._chain(src, target)
        if not chain:
            return None
        receiver = chain[-1]
        if receiver not in src.names.get("routers", ()) and not _ROUTER_NAME_RE.search(receiver):
            return None
        return [
            _Registration(EntryPoint("http", method=method, route=route), f"{method} {route}", node, args[1:])
        ]

    def _registration(self, state: _State, registration: _Registration) -> None:
        src = state.src
        call = registration.call
        count = state.labels.get(registration.label, 0) + 1
        state.labels[registration.label] = count
        label = registration.label if count == 1 else f"{registration.label} #{count}"
        callee = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        head = src.slice(
            callee.start_byte if callee is not None else call.start_byte, registration.handlers[0].start_byte
        )
        prop = callee.child_by_field_name("property") if callee is not None else None
        fn = FunctionDef(
            name=label,
            qualified_name=label,
            kind=FunctionKind.ROUTE,
            start_line=line_of(prop or call),
            end_line=end_line_of(arguments or call),
            signature=_registration_signature(head),
            entry_point=registration.entry,
            name_line=line_of(prop or call),
        )
        roots: list[Node] = []
        for handler in registration.handlers:
            if handler.type in ("identifier", "member_expression"):
                chain = self._chain(src, handler)
                if chain:
                    fn.calls.append(
                        CallSite(chain[-1], line_of(handler), receiver=_receiver_of(chain), kind="reference")
                    )
                continue
            roots.append(handler)
            if handler.type in _FUNCTION_EXPRS:
                fn.params = self._params(src, handler)
        nested = self.visit_body(src, fn, roots, state.scope)
        fn.local_types = {key: value for key, value in fn.local_types.items() if not value.startswith("@")}
        self._emit_nested(state, nested, fn)
        state.module.functions.append(fn)

    # ------------------------------------------------------- body visitor hooks

    def bound_names(self, src: Source, node: Node) -> Iterable[str]:
        kind = node.type
        if kind == "variable_declarator":
            holder = node.child_by_field_name("name")
        elif kind == "catch_clause":
            holder = node.child_by_field_name("parameter")
        elif kind == "for_in_statement":
            holder = node.child_by_field_name("left")
        else:
            holder = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
        if holder is None:
            return
        if holder.type in _PATTERN_NAMES:
            yield src.text(holder)
            return
        # Destructuring patterns and parameter lists: every identifier they bind.
        stack = [holder]
        budget = 200
        while stack and budget:
            budget -= 1
            current = stack.pop()
            for child in current.named_children:
                if child.type in _PATTERN_NAMES:
                    yield src.text(child)
                elif child.type in ("pair_pattern", "assignment_pattern", "object_assignment_pattern"):
                    target = child.child_by_field_name("value" if child.type == "pair_pattern" else "left")
                    if target is not None:
                        if target.type in _PATTERN_NAMES:
                            yield src.text(target)
                        else:
                            stack.append(target)
                elif child.type not in ("type_annotation", "decorator"):
                    stack.append(child)

    def children_to_visit(self, node: Node) -> list[Node]:
        kind = node.type
        if kind == "member_expression":
            target = node.child_by_field_name("object")
            return [target] if target is not None else []
        if kind == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None and callee.type == "identifier" and src_is_require(callee):
                return []
            return node.named_children
        if kind == "variable_declarator":
            value = node.child_by_field_name("value")
            return [value] if value is not None else []
        if kind in _FUNCTION_TYPES:
            body = node.child_by_field_name("body")
            return [body] if body is not None else []
        if kind in ("jsx_opening_element", "jsx_self_closing_element"):
            return [
                child for child in node.named_children if child.type in ("jsx_attribute", "jsx_expression")
            ]
        return node.named_children

    def call_site(self, src: Source, node: Node) -> CallSite | None:
        kind = node.type
        if kind == "call_expression":
            callee = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if callee is None:
                return None
            arg_count = (
                arguments.named_child_count
                if arguments is not None and arguments.type == "arguments"
                else None
            )
            while callee.type in _TRANSPARENT and callee.named_child_count:
                callee = callee.named_children[-1 if callee.type == "type_assertion" else 0]
            if callee.type == "identifier":
                name = src.text(callee)
                return None if name == "require" else CallSite(name, line_of(callee), arg_count=arg_count)
            if callee.type == "super":
                return CallSite("super", line_of(callee), receiver="super", arg_count=arg_count, kind="super")
            if callee.type != "member_expression":
                return None
            prop = callee.child_by_field_name("property")
            target = callee.child_by_field_name("object")
            if prop is None or target is None:
                return None
            name = src.text(prop)
            if name in ("call", "apply", "bind"):
                # ``Foo.prototype.find.call(this, id)`` invokes ``find``; ``.bind`` only references it.
                chain = self._chain(src, target)
                if chain and len(chain) >= 1:
                    chain = [part for part in chain if part != "prototype"]
                    if chain:
                        return CallSite(
                            chain[-1],
                            line_of(prop),
                            receiver=".".join(chain[:-1]) or None,
                            kind="reference" if name == "bind" else "call",
                        )
            receiver: str | None = self._receiver(src, target)
            if receiver in ("exports", "module.exports"):
                receiver = None  # a function of this module
            return CallSite(name, line_of(prop), receiver=receiver, arg_count=arg_count)
        if kind == "new_expression":
            constructor = node.child_by_field_name("constructor")
            arguments = node.child_by_field_name("arguments")
            type_name = simple_type(src.text(constructor)) if constructor is not None else None
            if type_name is None:
                return None
            return CallSite(
                type_name,
                line_of(node),
                arg_count=arguments.named_child_count if arguments is not None else 0,
                kind="new",
            )
        # JSX: rendering <Widget /> invokes the component.
        tag = node.child_by_field_name("name")
        if tag is None:
            return None
        chain = self._chain(src, tag)
        if not chain or not chain[-1][:1].isupper():
            return None
        return CallSite(chain[-1], line_of(tag), receiver=".".join(chain[:-1]) or None)

    def _receiver(self, src: Source, node: Node) -> str:
        parts: list[str] = []
        current: Node | None = node
        for _ in range(8):
            if current is None:
                break
            kind = current.type
            if kind in ("identifier", "this", "super"):
                parts.append(src.text(current))
                return ".".join(reversed(parts))
            if kind == "member_expression":
                prop = current.child_by_field_name("property")
                if prop is None:
                    break
                parts.append(src.text(prop))
                current = current.child_by_field_name("object")
            elif kind in _TRANSPARENT and current.named_child_count:
                current = current.named_children[-1 if kind == "type_assertion" else 0]
            elif kind == "new_expression" and not parts:
                constructor = current.child_by_field_name("constructor")
                type_name = simple_type(src.text(constructor)) if constructor is not None else None
                return f"type:{type_name}" if type_name else "<expr>"
            else:
                break
        return "<expr>"

    def extra_calls(self, src: Source, node: Node) -> Iterable[CallSite]:
        if node.type != "call_expression":
            return
        arguments = node.child_by_field_name("arguments")
        callee = node.child_by_field_name("function")
        if arguments is None or callee is None or arguments.type != "arguments":
            return
        prop = callee.child_by_field_name("property") if callee.type == "member_expression" else callee
        takes_callbacks = prop is not None and src.text(prop) in _CALLBACK_APIS
        for argument in arguments.named_children:
            if argument.type == "identifier":
                name = src.text(argument)
                if name not in _NOT_CALLBACKS:
                    yield CallSite(name, line_of(argument), kind="reference")
            elif takes_callbacks and argument.type == "member_expression":
                chain = self._chain(src, argument)
                if chain and len(chain) == 2:
                    yield CallSite(
                        chain[-1], line_of(argument), receiver=_receiver_of(chain), kind="reference"
                    )

    def literal_value(self, src: Source, node: Node, lookup: Lookup) -> StrVal | None:
        kind = node.type
        if kind == "string":
            parts = [
                src.text(child) if child.type == "string_fragment" else unescape(src.text(child))
                for child in node.named_children
                if child.type in ("string_fragment", "escape_sequence")
            ]
            return StrVal("".join(parts))
        if kind != "template_string":
            return None
        pieces: list[str] = []
        dynamic = False
        for child in node.named_children:
            if child.type == "string_fragment":
                pieces.append(src.text(child))
            elif child.type == "escape_sequence":
                pieces.append(unescape(src.text(child)))
            elif child.type == "template_substitution":
                expression = child.named_children[0] if child.named_child_count else None
                value = self.eval_string(src, expression, lookup) if expression is not None else None
                if value is not None and value.literal:
                    pieces.append(value.text)
                    dynamic = dynamic or value.dynamic
                else:
                    pieces.append(self.placeholder(src, expression) if expression is not None else "${?}")
                    dynamic = True
        return StrVal("".join(pieces), dynamic=dynamic)

    def _plain_string(self, src: Source, node: Node) -> str | None:
        if node.type not in ("string", "template_string"):
            return None
        value = self.literal_value(src, node, lambda _name: None)
        return value.text if value is not None and not value.dynamic else None

    def concat_operands(self, node: Node) -> list[Node] | None:
        return flatten_binary(node, "+")

    def unwrap(self, node: Node) -> Node:
        current = node
        for _ in range(8):
            if current.type in _TRANSPARENT and current.named_child_count:
                current = current.named_children[-1 if current.type == "type_assertion" else 0]
            else:
                break
        return current

    def special_string(self, src: Source, node: Node, lookup: Lookup) -> StrVal | None:
        if node.type != "call_expression":
            return None
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or arguments is None:
            return None
        if arguments.type == "template_string":  # tagged template: sql`SELECT ...`
            return self.literal_value(src, arguments, lookup)
        args = arguments.named_children
        if callee.type == "identifier":
            if src.text(callee) in _FORMAT_CALLS and args:
                value = self.eval_string(src, args[0], lookup)
                return StrVal(value.text, dynamic=True) if value is not None and value.literal else None
            return None
        if callee.type != "member_expression":
            return None
        prop = callee.child_by_field_name("property")
        target = callee.child_by_field_name("object")
        if prop is None or target is None:
            return None
        name = src.text(prop)
        if name in _STRING_PASSTHROUGH and not args:
            return self.eval_string(src, target, lookup)
        if name in _FORMAT_CALLS and args and target.type == "identifier":
            value = self.eval_string(src, args[0], lookup)
            return StrVal(value.text, dynamic=True) if value is not None and value.literal else None
        if name == "join" and target.type == "array" and len(args) <= 1:
            separator = ","
            if args:
                folded = self.eval_string(src, args[0], lookup)
                if folded is None or not folded.literal:
                    return None
                separator = folded.text
            pieces: list[str] = []
            dynamic = False
            literal = False
            for item in target.named_children:
                value = self.eval_string(src, item, lookup)
                if value is None:
                    pieces.append(self.placeholder(src, item))
                    dynamic = True
                else:
                    pieces.append(value.text)
                    dynamic = dynamic or value.dynamic
                    literal = literal or value.literal
            return StrVal(separator.join(pieces), dynamic=dynamic) if literal else None
        return None

    def assignment(self, src: Source, node: Node) -> Assignment | None:
        kind = node.type
        if kind == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name is None or name.type != "identifier":
                return None
            if value is not None and value.type == "array":
                items = value.named_children
                if all(item.type in ("string", "template_string") for item in items):
                    # const parts = ["SELECT ...", "FROM t"]; parts.push("WHERE ..."); parts.join(" ")
                    return Assignment(src.text(name), None, builder=True, parts=list(items), separator=" ")
            return Assignment(src.text(name), value)
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier":
            return None
        if kind == "augmented_assignment_expression":
            operator = node.child_by_field_name("operator")
            if operator is None or operator.type != "+=":
                return None
            return Assignment(src.text(left), right, augmented=True)
        return Assignment(src.text(left), right)

    def append(self, src: Source, node: Node) -> Append | None:
        if node.type != "call_expression":
            return None
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            callee is None
            or arguments is None
            or callee.type != "member_expression"
            or arguments.type != "arguments"
        ):
            return None
        prop = callee.child_by_field_name("property")
        target = callee.child_by_field_name("object")
        if prop is None or target is None or target.type != "identifier" or src.text(prop) != "push":
            return None
        return Append(src.text(target), list(arguments.named_children), [node.id])

    def _constructed_type(self, src: Source, value: Node | None) -> str | None:
        if value is None:
            return None
        value = self.unwrap(value)
        if value.type != "new_expression":
            return None
        constructor = value.child_by_field_name("constructor")
        return simple_type(src.text(constructor)) if constructor is not None else None

    def local_types(self, src: Source, node: Node) -> Iterable[tuple[str, str]]:
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is None or name.type != "identifier":
                return
            annotation = node.child_by_field_name("type")
            type_name = (
                simple_type(src.text(annotation.named_children[0]))
                if annotation is not None and annotation.named_child_count
                else None
            ) or self._constructed_type(src, node.child_by_field_name("value"))
            if type_name:
                yield src.text(name), type_name
            return
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or left.type != "member_expression":
            return
        target = left.child_by_field_name("object")
        prop = left.child_by_field_name("property")
        if target is None or prop is None or target.type != "this":
            return
        type_name = self._constructed_type(src, right)
        if type_name:
            yield f"this.{src.text(prop)}", type_name
        elif right.type == "identifier":
            # ``this.repo = repo``: resolved against the parameter's type afterwards.
            yield f"this.{src.text(prop)}", "@" + src.text(right)

    def constant_ref(self, src: Source, node: Node) -> tuple[str | None, str] | None:
        if node.type == "identifier":
            return None, src.text(node)
        prop = node.child_by_field_name("property")
        target = node.child_by_field_name("object")
        if prop is None or target is None or prop.type != "property_identifier":
            return None
        if target.type == "this":
            return None, src.text(prop)
        if target.type == "identifier":
            return src.text(target), src.text(prop)
        return None


class TypeScriptExtractor(JavaScriptExtractor):
    language: ClassVar[Language] = Language.TYPESCRIPT

    def grammar(self, path: str) -> TsLanguage:
        return _TSX if path.lower().endswith("x") else _TS


def _receiver_of(chain: list[str]) -> str | None:
    """Receiver text for the last element of a property path; exports of this module have none."""
    prefix = chain[:-1]
    if prefix in (["exports"], ["module", "exports"]):
        return None
    return ".".join(prefix) or None


def src_is_require(callee: Node) -> bool:
    return callee.text == b"require"


def _registration_signature(head: str) -> str:
    text = collapse_ws(head, 200).rstrip(", ")
    return f"{text}…)" if text.endswith("(") else f"{text}, …)"


def _stem_name(path: str) -> str:
    """Name for an anonymous default export: the file name, or the directory for ``index`` files."""
    pure = PurePosixPath(path)
    stem = pure.name.split(".", 1)[0]
    if stem in ("index", "") and pure.parent.name:
        stem = pure.parent.name
    return re.sub(r"\W", "_", stem) or "default"


def _join_route(prefix: str | None, path: str | None) -> str:
    joined = "/".join(part.strip("/") for part in (prefix or "", path or "") if part.strip("/"))
    return "/" + joined
