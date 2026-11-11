"""C / C++ extractor: classes, free functions, out-of-line methods, embedded SQL.

C++ splits a class across files: the header declares methods, a source file
defines them as ``Class::method``. Declarations are not emitted as scopes (the
definition is the scope), except pure-virtual methods, which exist only as
declarations. An out-of-line definition records its owner by name; the class may
live in another file and is matched during resolution.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import ClassVar, Final

import tree_sitter_cpp
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
    collapse_ws,
    end_line_of,
    flatten_binary,
    leading_comment,
    line_of,
    make_sql_statement,
    simple_type,
    unescape,
)
from parsers.ir import (
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

_GRAMMAR: Final = TsLanguage(tree_sitter_cpp.language())

#: Nodes that only group declarations; their children are processed as if top-level.
_CONTAINERS: Final = frozenset(
    {
        "translation_unit", "declaration_list", "linkage_specification", "preproc_ifdef", "preproc_if",
        "preproc_else", "preproc_elif", "preproc_elifdef",
    }
)  # fmt: skip
_CLASS_KINDS: Final = {
    "class_specifier": ClassKind.CLASS,
    "struct_specifier": ClassKind.STRUCT,
    "union_specifier": ClassKind.STRUCT,
    "enum_specifier": ClassKind.ENUM,
}
_DECLARATOR_WRAPPERS: Final = frozenset(
    {
        "pointer_declarator",
        "reference_declarator",
        "parenthesized_declarator",
        "array_declarator",
        "init_declarator",
    }
)
_STRING_NODES: Final = frozenset({"string_literal", "raw_string_literal", "concatenated_string"})
_BUILDER_TYPE_RE: Final = re.compile(
    r"(?:^|[^A-Za-z_])(?:[a-z]*string(?:stream)?|ostringstream|CString|QString)$", re.I
)
_SMART_FACTORIES: Final = frozenset({"make_unique", "make_shared", "allocate_shared"})
_LIBRARY_NAMESPACES: Final = frozenset({"std", "boost", "absl", "fmt"})
_FORMAT_CALLS: Final = frozenset(
    {
        "sprintf",
        "snprintf",
        "swprintf",
        "asprintf",
        "vsnprintf",
        "format",
        "Format",
        "sprintf_s",
        "_snprintf",
        "arg",
    }
)
_LOG_STREAMS: Final = re.compile(
    r"^(?:std::)?(?:cout|cerr|clog|wcout|wcerr)$|^(?:LOG|DLOG|VLOG|SYSLOG|PLOG|qDebug|qWarning)\b"
)
_MAIN_NAMES: Final = frozenset({"main", "wmain", "WinMain", "wWinMain", "_tmain"})
_MACRO_STRING_RE: Final = re.compile(r'"((?:[^"\\]|\\.)*)"')
_EXEC_SQL_RE: Final = re.compile(r"\bEXEC\s+SQL\b(.*?);", re.IGNORECASE | re.DOTALL)
_EXEC_SQL_SKIP_RE: Final = re.compile(
    r"^\s*(?:INCLUDE|BEGIN\s+DECLARE|END\s+DECLARE|WHENEVER|CONNECT|COMMIT|ROLLBACK|OPEN|CLOSE|FETCH|VAR|TYPE|"
    r"CONTEXT|ALLOCATE|FREE|SAVEPOINT|SET\s+TRANSACTION)\b",
    re.IGNORECASE,
)
_CURSOR_PREFIX_RE: Final = re.compile(
    r"^\s*DECLARE\s+\w+\s+(?:\w+\s+)*?CURSOR\s+(?:WITH\s+HOLD\s+)?FOR\s+", re.IGNORECASE
)


class CppExtractor(TreeSitterExtractor):
    language: ClassVar[Language] = Language.CPP

    CALL_TYPES = frozenset({"call_expression", "new_expression"})
    STRING_TYPES = frozenset(
        {
            "string_literal",
            "raw_string_literal",
            "concatenated_string",
            "binary_expression",
            "call_expression",
        }
    )
    ASSIGN_TYPES = frozenset({"declaration", "assignment_expression"})
    APPEND_TYPES = frozenset({"call_expression", "binary_expression"})
    DECL_TYPES = frozenset({"declaration", "for_range_loop"})
    BRANCH_TYPES = frozenset(
        {
            "if_statement", "for_statement", "for_range_loop",
            "while_statement", "do_statement", "case_statement",
            "catch_clause", "conditional_expression",
        }
    )  # fmt: skip
    REF_TYPES = frozenset({"identifier", "qualified_identifier"})
    SKIP_TYPES = frozenset(
        {"comment", "type_descriptor", "parameter_list", "lambda_capture_specifier", "template_argument_list"}
    )

    def grammar(self, path: str) -> TsLanguage:
        return _GRAMMAR

    # ------------------------------------------------------------------ module

    def extract_module(self, src: Source, root: Node, module: ModuleDef) -> None:
        scope = Scope()
        first = root.named_children[0] if root.named_child_count else None
        if first is not None and first.type == "comment" and src.text(first).startswith("/*"):
            module.docstring = (
                leading_comment(src, first.next_sibling) if first.next_sibling is not None else None
            )
        items = list(self._declarations(src, root, module))
        # Pass 1: constants, so function bodies can see constants declared below them.
        for node, _anchor in items:
            if node.type == "declaration":
                self._constant(src, node, scope, module)
            elif node.type == "preproc_def":
                self._macro_constant(src, node, scope, module)
        for node, anchor in items:
            kind = node.type
            if kind == "function_definition":
                self._function(src, node, anchor, module, scope, None)
            elif kind in _CLASS_KINDS:
                self._class(src, node, anchor, module, scope, None, None)
            elif kind == "type_definition":
                inner = node.child_by_field_name("type")
                alias = node.child_by_field_name("declarator")
                if inner is not None and inner.type in _CLASS_KINDS and alias is not None:
                    self._class(src, inner, anchor, module, scope, None, src.text(alias))
            elif kind == "declaration":
                inner = node.child_by_field_name("type")
                if inner is not None and inner.type in _CLASS_KINDS:  # struct X { ... } instance;
                    self._class(src, inner, anchor, module, scope, None, None)

    def _declarations(self, src: Source, root: Node, module: ModuleDef) -> Iterable[tuple[Node, Node]]:
        """Top-level declarations ``(node, anchor)``, looking through namespaces, templates and ``#if``."""
        stack: list[Node] = [root]
        while stack:
            node = stack.pop()
            kind = node.type
            if kind in _CONTAINERS:
                stack.extend(reversed(node.named_children))
            elif kind == "namespace_definition":
                name = node.child_by_field_name("name")
                body = node.child_by_field_name("body")
                if name is not None and module.package is None and node.parent is not None:
                    module.package = src.text(name)
                elif name is not None and module.package and node.parent is not None:
                    outer = node.parent.parent
                    if (
                        outer is not None
                        and outer.type == "namespace_definition"
                        and "::" not in module.package
                    ):
                        outer_name = outer.child_by_field_name("name")
                        if outer_name is not None and src.text(outer_name) == module.package:
                            module.package = f"{module.package}::{src.text(name)}"
                if body is not None:
                    stack.append(body)
            elif kind == "template_declaration":
                for child in node.named_children:
                    if (
                        child.type in ("function_definition", "declaration", "template_declaration")
                        or child.type in _CLASS_KINDS
                    ):
                        yield (child, node) if child.type != "template_declaration" else (child, child)
            elif kind == "preproc_include":
                path = node.child_by_field_name("path")
                if path is not None and path.type == "string_literal":
                    module.imports.append(ImportDef(src.text(path).strip('"'), None, None, line_of(node)))
            else:
                yield node, node

    def _constant(self, src: Source, node: Node, scope: Scope, module: ModuleDef) -> None:
        for declarator in node.children_by_field_name("declarator"):
            if declarator.type != "init_declarator":
                continue
            value = declarator.child_by_field_name("value")
            name = _innermost_name(declarator)
            if value is None or name is None:
                continue
            if value.type == "initializer_list" and value.named_child_count == 1:
                value = value.named_children[0]
            folded = self.eval_string(src, value, scope.lookup)
            if folded is None or not folded.literal:
                continue
            definition = scope.define(src.text(name), folded, line_of(node), end_line_of(node))
            if definition is not None:
                module.constants[definition.name] = definition

    def _macro_constant(self, src: Source, node: Node, scope: Scope, module: ModuleDef) -> None:
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None or value is None:
            return
        text = src.text(value).replace("\\\n", " ").strip()
        literals = _MACRO_STRING_RE.findall(text)
        if not literals or _MACRO_STRING_RE.sub("", text).strip():
            return  # not purely one or more adjacent string literals
        folded = StrVal("".join(unescape(literal) for literal in literals))
        definition = scope.define(src.text(name), folded, line_of(node), end_line_of(node))
        if definition is not None:
            module.constants[definition.name] = definition

    # ------------------------------------------------------------------- class

    def _class(
        self,
        src: Source,
        node: Node,
        anchor: Node,
        module: ModuleDef,
        parent_scope: Scope,
        owner: ClassDef | None,
        alias: str | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        name = simple_type(src.text(name_node)) if name_node is not None else alias
        if body is None or not name:
            return  # forward declaration or anonymous type
        cls = ClassDef(
            name=name,
            qualified_name=f"{owner.qualified_name}::{name}" if owner else name,
            kind=_CLASS_KINDS[node.type],
            start_line=line_of(anchor),
            end_line=end_line_of(anchor),
            docstring=leading_comment(src, anchor),
            owner=owner.qualified_name if owner else None,
            name_line=line_of(name_node or node),
        )
        for child in node.named_children:
            if child.type == "base_class_clause":
                for base in child.named_children:
                    if base.type in ("type_identifier", "qualified_identifier", "template_type"):
                        base_name = simple_type(src.text(base).split("<", 1)[0])
                        if base_name:
                            cls.bases.append(base_name)
        module.classes.append(cls)
        if cls.kind is ClassKind.ENUM:
            return

        scope = Scope(parent_scope, cls.name)
        members: list[tuple[Node, Node]] = []
        stack = list(reversed(body.named_children))
        while stack:
            member = stack.pop()
            if member.type in _CONTAINERS:
                stack.extend(reversed(member.named_children))
            elif member.type == "template_declaration":
                members.extend(
                    (child, member)
                    for child in member.named_children
                    if child.type != "template_parameter_list"
                )
            else:
                members.append((member, member))

        for member, _member_anchor in members:
            if member.type == "field_declaration":
                self._field(src, member, cls, scope)
        for member, member_anchor in members:
            kind = member.type
            if kind == "function_definition":
                self._function(src, member, member_anchor, module, scope, cls)
            elif kind in ("field_declaration", "declaration"):
                self._pure_virtual(src, member, member_anchor, cls)
                inner = member.child_by_field_name("type")
                if (
                    inner is not None
                    and inner.type in _CLASS_KINDS
                    and inner.child_by_field_name("body") is not None
                ):
                    self._class(src, inner, member_anchor, module, scope, cls, None)
            elif kind in _CLASS_KINDS:
                self._class(src, member, member_anchor, module, scope, cls, None)

    def _field(self, src: Source, node: Node, cls: ClassDef, scope: Scope) -> None:
        type_node = node.child_by_field_name("type")
        type_name = simple_type(src.text(type_node)) if type_node is not None else None
        for declarator in node.children_by_field_name("declarator"):
            if _function_declarator(declarator) is not None:
                continue
            name = _innermost_name(declarator)
            if name is None:
                continue
            field_name = src.text(name)
            if type_name:
                cls.fields[field_name] = type_name
            default = node.child_by_field_name("default_value")
            if default is not None:
                folded = self.eval_string(src, default, scope.lookup)
                if folded is not None and folded.literal:
                    definition = scope.define(field_name, folded, line_of(node), end_line_of(node))
                    if definition is not None:
                        cls.constants[field_name] = definition

    def _pure_virtual(self, src: Source, node: Node, anchor: Node, cls: ClassDef) -> None:
        """``virtual int run() = 0;`` exists only as a declaration: emit it as an abstract method."""
        declarator = next(iter(node.children_by_field_name("declarator")), None)
        function = _function_declarator(declarator) if declarator is not None else None
        if function is None:
            return
        default = node.child_by_field_name("default_value")
        if default is None or src.text(default).strip() != "0":
            return
        name_node = function.child_by_field_name("declarator")
        if name_node is None:
            return
        name = src.text(name_node)
        return_node = node.child_by_field_name("type")
        cls.methods.append(
            FunctionDef(
                name=name,
                qualified_name=f"{cls.qualified_name}::{name}",
                kind=FunctionKind.METHOD,
                start_line=line_of(anchor),
                end_line=end_line_of(anchor),
                signature=collapse_ws(src.text(node)).rstrip(";"),
                params=self._params(src, function),
                return_type=collapse_ws(src.text(return_node), 80) if return_node is not None else None,
                docstring=leading_comment(src, anchor),
                modifiers=["virtual"],
                is_abstract=True,
                owner=cls.qualified_name,
                name_line=line_of(name_node),
            )
        )

    # ---------------------------------------------------------------- function

    def _function(
        self, src: Source, node: Node, anchor: Node, module: ModuleDef, scope: Scope, cls: ClassDef | None
    ) -> None:
        declarator = node.child_by_field_name("declarator")
        function = _function_declarator(declarator) if declarator is not None else None
        if function is None:
            return
        name_node = function.child_by_field_name("declarator")
        if name_node is None:
            return
        scopes, name = self._declared_name(src, name_node)
        if not name:
            return
        body = node.child_by_field_name("body")
        initializers = next(
            (child for child in node.named_children if child.type == "field_initializer_list"), None
        )
        owner_name = cls.qualified_name if cls is not None else ("::".join(scopes[-1:]) if scopes else None)
        owner_simple = cls.name if cls is not None else (scopes[-1] if scopes else None)
        return_node = node.child_by_field_name("type")
        modifiers = [
            src.text(child)
            for child in node.children
            if child.type
            in ("storage_class_specifier", "virtual", "type_qualifier", "virtual_function_specifier")
        ]
        signature_end = (
            (initializers or body or node).start_byte if (initializers or body) is not None else node.end_byte
        )
        fn = FunctionDef(
            name=name,
            qualified_name=f"{owner_name}::{name}" if owner_name else name,
            kind=(
                FunctionKind.CONSTRUCTOR
                if owner_simple is not None and name == owner_simple
                else FunctionKind.METHOD
                if owner_name
                else FunctionKind.FUNCTION
            ),
            start_line=line_of(anchor),
            end_line=end_line_of(anchor),
            signature=collapse_ws(src.slice(node.start_byte, signature_end)),
            params=self._params(src, function),
            return_type=collapse_ws(src.text(return_node), 80) if return_node is not None else None,
            docstring=leading_comment(src, anchor),
            modifiers=modifiers,
            owner=owner_name,
            name_line=line_of(name_node),
        )
        for param in fn.params:
            if param.type:
                fn.local_types[param.name] = param.type
        if owner_name is None and name in _MAIN_NAMES:
            fn.entry_point = EntryPoint("main")
        roots = [root for root in (initializers, body) if root is not None]
        self.visit_body(src, fn, roots, scope)
        if body is not None:
            self._embedded_sql(src, fn, body)
        (cls.methods if cls is not None else module.functions).append(fn)

    def _embedded_sql(self, src: Source, fn: FunctionDef, body: Node) -> None:
        """Pro*C / ESQL: ``EXEC SQL <statement>;`` written directly in the function body."""
        text = src.text(body)
        if "EXEC" not in text.upper():
            return
        base_line = line_of(body)
        for match in _EXEC_SQL_RE.finditer(text):
            statement = match.group(1)
            if _EXEC_SQL_SKIP_RE.match(statement):
                continue
            statement = _CURSOR_PREFIX_RE.sub("", statement).strip()
            line = base_line + text.count("\n", 0, match.start())
            end_line = base_line + text.count("\n", 0, match.end())
            sql = make_sql_statement(statement, line, end_line)
            if sql is not None:
                fn.sql.append(sql)

    def _declared_name(self, src: Source, node: Node) -> tuple[list[str], str]:
        """``(enclosing scopes, name)`` of a function declarator name such as ``ns::Class<T>::method``."""
        scopes: list[str] = []
        current: Node | None = node
        for _ in range(8):
            if current is None:
                return scopes, ""
            kind = current.type
            if kind == "qualified_identifier":
                scope_node = current.child_by_field_name("scope")
                if scope_node is not None:
                    scope_name = simple_type(src.text(scope_node).split("<", 1)[0])
                    if scope_name:
                        scopes.append(scope_name)
                current = current.child_by_field_name("name")
            elif kind == "template_function":
                current = current.child_by_field_name("name")
            else:
                return scopes, collapse_ws(src.text(current), 60).replace(" ", "")
        return scopes, ""

    def _params(self, src: Source, function: Node) -> list[Param]:
        holder = function.child_by_field_name("parameters")
        params: list[Param] = []
        if holder is None:
            return params
        for child in holder.named_children:
            if child.type not in ("parameter_declaration", "optional_parameter_declaration"):
                continue
            declarator = child.child_by_field_name("declarator")
            type_node = child.child_by_field_name("type")
            name = _innermost_name(declarator) if declarator is not None else None
            if name is None:
                continue  # unnamed parameter, e.g. ``(void)``
            params.append(
                Param(src.text(name), simple_type(src.text(type_node)) if type_node is not None else None)
            )
        return params

    # ------------------------------------------------------- body visitor hooks

    def children_to_visit(self, node: Node) -> list[Node]:
        kind = node.type
        if kind == "field_expression":
            target = node.child_by_field_name("argument")
            return [target] if target is not None else []
        if kind == "qualified_identifier":
            return []
        if kind == "declaration":
            return [child for child in node.named_children if child.type == "init_declarator"]
        if kind == "init_declarator":
            value = node.child_by_field_name("value")
            return [value] if value is not None else []
        if kind == "lambda_expression":
            body = node.child_by_field_name("body")
            return [body] if body is not None else []
        return node.named_children

    def call_site(self, src: Source, node: Node) -> CallSite | None:
        arguments = node.child_by_field_name("arguments")
        arg_count = arguments.named_child_count if arguments is not None else None
        if node.type == "new_expression":
            type_node = node.child_by_field_name("type")
            type_name = simple_type(src.text(type_node)) if type_node is not None else None
            return (
                CallSite(type_name, line_of(node), arg_count=arg_count or 0, kind="new")
                if type_name
                else None
            )
        callee = node.child_by_field_name("function")
        if callee is None:
            return None
        kind = callee.type
        if kind == "identifier":
            return CallSite(src.text(callee), line_of(callee), arg_count=arg_count)
        if kind == "field_expression":
            member = callee.child_by_field_name("field")
            target = callee.child_by_field_name("argument")
            if member is None or target is None:
                return None
            name = src.text(member).split("<", 1)[0]
            return CallSite(name, line_of(member), receiver=self._receiver(src, target), arg_count=arg_count)
        if kind in ("qualified_identifier", "template_function"):
            scopes, name = self._declared_name(src, callee)
            if (scopes and scopes[0] in _LIBRARY_NAMESPACES) or (not scopes and name in _SMART_FACTORIES):
                if name in _SMART_FACTORIES:
                    made = re.search(r"<\s*([\w:]+)", src.text(callee))
                    type_name = simple_type(made.group(1)) if made else None
                    if type_name:
                        return CallSite(type_name, line_of(callee), arg_count=arg_count, kind="new")
                return None
            if not name:
                return None
            return CallSite(
                name, line_of(callee), receiver=scopes[-1] if scopes else None, arg_count=arg_count
            )
        return None

    def _receiver(self, src: Source, node: Node) -> str:
        parts: list[str] = []
        current: Node | None = node
        for _ in range(6):
            if current is None:
                break
            kind = current.type
            if kind in ("identifier", "this"):
                parts.append(src.text(current))
                return ".".join(reversed(parts))
            if kind == "field_expression":
                member = current.child_by_field_name("field")
                if member is None:
                    break
                parts.append(src.text(member))
                current = current.child_by_field_name("argument")
            elif kind in ("parenthesized_expression", "pointer_expression") and current.named_child_count:
                current = current.named_children[-1]
            elif kind == "qualified_identifier" and not parts:
                return simple_type(src.text(current)) or "<expr>"
            else:
                break
        return "<expr>"

    def literal_value(self, src: Source, node: Node, lookup: Lookup) -> StrVal | None:
        kind = node.type
        if kind == "string_literal":
            parts = [
                src.text(child) if child.type == "string_content" else unescape(src.text(child))
                for child in node.named_children
            ]
            return StrVal("".join(parts))
        if kind == "raw_string_literal":
            content = next(
                (child for child in node.named_children if child.type == "raw_string_content"), None
            )
            return StrVal(src.text(content) if content is not None else "")
        if kind == "concatenated_string":
            pieces: list[str] = []
            dynamic = False
            for child in node.named_children:
                value = self.literal_value(src, child, lookup)
                if value is None and child.type == "identifier":
                    value = lookup(src.text(child))  # a string macro between literals
                if value is None:
                    pieces.append(self.placeholder(src, child))
                    dynamic = True
                else:
                    pieces.append(value.text)
                    dynamic = dynamic or value.dynamic
            return StrVal("".join(pieces), dynamic=dynamic)
        return None

    def concat_operands(self, node: Node) -> list[Node] | None:
        return flatten_binary(node, "+")

    def unwrap(self, node: Node) -> Node:
        current = node
        for _ in range(8):
            if current.type == "parenthesized_expression" and current.named_child_count == 1:
                current = current.named_children[0]
            elif current.type == "cast_expression":
                value = current.child_by_field_name("value")
                if value is None:
                    break
                current = value
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
        text = src.text(callee) if callee.end_byte - callee.start_byte < 80 else ""
        name = re.split(r"::|\.|->", text)[-1].split("<", 1)[0]
        args = arguments.named_children
        if name in (
            "string",
            "wstring",
            "c_str",
            "str",
            "QString",
            "CString",
            "_T",
            "TEXT",
            "QStringLiteral",
            "tr",
        ):
            # std::string("..."), sql.c_str(), _T("...")
            if args:
                return self.eval_string(src, args[0], lookup)
            if callee.type == "field_expression":
                target = callee.child_by_field_name("argument")
                return self.eval_string(src, target, lookup) if target is not None else None
            return None
        if name in _FORMAT_CALLS:
            for argument in args:
                value = self.eval_string(src, argument, lookup) if argument.type in _STRING_NODES else None
                if value is not None and value.literal:
                    return StrVal(value.text, dynamic=True)
        return None

    def assignment(self, src: Source, node: Node) -> Assignment | None:
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            operator = node.child_by_field_name("operator")
            if left is None or right is None or operator is None or left.type != "identifier":
                return None
            if operator.type not in ("=", "+="):
                return None
            return Assignment(src.text(left), right, augmented=operator.type == "+=")
        type_node = node.child_by_field_name("type")
        declarator = next(iter(node.children_by_field_name("declarator")), None)
        if type_node is None or declarator is None:
            return None
        is_builder = bool(_BUILDER_TYPE_RE.search(src.text(type_node)))
        if declarator.type == "identifier":
            return Assignment(src.text(declarator), None, builder=True) if is_builder else None
        if declarator.type != "init_declarator":
            return None
        name = _innermost_name(declarator)
        value = declarator.child_by_field_name("value")
        if name is None or value is None:
            return None
        if value.type in ("argument_list", "initializer_list"):  # std::string sql("SELECT ...");
            value = value.named_children[0] if value.named_child_count == 1 else None
        return (
            Assignment(src.text(name), value, builder=is_builder) if value is not None or is_builder else None
        )

    def append(self, src: Source, node: Node) -> Append | None:
        if node.type == "call_expression":  # sql.append("...")
            callee = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if callee is None or arguments is None or callee.type != "field_expression":
                return None
            member = callee.child_by_field_name("field")
            target = callee.child_by_field_name("argument")
            if member is None or target is None or target.type != "identifier":
                return None
            if (
                src.text(member) not in ("append", "push_back", "Append", "AppendFormat")
                or arguments.named_child_count < 1
            ):
                return None
            return Append(src.text(target), [arguments.named_children[0]], [node.id])
        # stream << "..." << value << "..."
        operator = node.child_by_field_name("operator")
        if operator is None or operator.type != "<<":
            return None
        operands: list[Node] = []
        chain: list[int] = []
        current = node
        while True:
            op = current.child_by_field_name("operator")
            left = current.child_by_field_name("left")
            right = current.child_by_field_name("right")
            if (
                current.type != "binary_expression"
                or op is None
                or op.type != "<<"
                or left is None
                or right is None
            ):
                break
            chain.append(current.id)
            operands.append(right)
            current = left
        operands.reverse()
        head = src.text(current) if current.end_byte - current.start_byte < 60 else ""
        if _LOG_STREAMS.search(head):
            return Append("", [], chain, quiet=True)
        if current.type != "identifier":
            return None
        return Append(head, operands, chain)

    def local_types(self, src: Source, node: Node) -> Iterable[tuple[str, str]]:
        type_node = node.child_by_field_name("type")
        if type_node is None:
            return
        declared = simple_type(src.text(type_node))
        if node.type == "for_range_loop":
            name = _innermost_name(node.child_by_field_name("declarator"))
            if name is not None and declared and declared != "auto":
                yield src.text(name), declared
            return
        for declarator in node.children_by_field_name("declarator"):
            name = _innermost_name(declarator)
            if name is None:
                continue
            type_name = declared
            if declared in (None, "auto"):
                type_name = None
                value = (
                    declarator.child_by_field_name("value") if declarator.type == "init_declarator" else None
                )
                if value is not None and value.type == "new_expression":
                    created = value.child_by_field_name("type")
                    type_name = simple_type(src.text(created)) if created is not None else None
                elif value is not None and value.type == "call_expression":
                    callee = value.child_by_field_name("function")
                    made = (
                        re.search(r"make_(?:unique|shared)\s*<\s*([\w:]+)", src.text(callee))
                        if callee is not None
                        else None
                    )
                    type_name = simple_type(made.group(1)) if made else None
            if type_name:
                yield src.text(name), type_name

    def constant_ref(self, src: Source, node: Node) -> tuple[str | None, str] | None:
        if node.type == "identifier":
            return None, src.text(node)
        scopes, name = self._declared_name(src, node)
        return (scopes[-1] if scopes else None, name) if name else None


def _function_declarator(node: Node | None) -> Node | None:
    """The ``function_declarator`` inside pointer / reference declarator wrappers, if any."""
    current = node
    for _ in range(6):
        if current is None:
            return None
        if current.type == "function_declarator":
            return current
        if current.type not in _DECLARATOR_WRAPPERS:
            return None
        current = current.child_by_field_name("declarator") or (
            current.named_children[-1] if current.named_child_count else None
        )
    return None


def _innermost_name(node: Node | None) -> Node | None:
    """The identifier a (possibly pointer / reference / array / function) declarator declares."""
    current = node
    for _ in range(8):
        if current is None:
            return None
        if current.type in ("identifier", "field_identifier"):
            return current
        if current.type not in _DECLARATOR_WRAPPERS and current.type != "function_declarator":
            return None
        inner = current.child_by_field_name("declarator")
        if inner is None:
            inner = next((child for child in current.named_children if child.type != "type_qualifier"), None)
        current = inner
    return None
