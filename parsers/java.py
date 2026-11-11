"""Java extractor: classes, methods, call sites, Spring/JAX-RS entry points, embedded SQL."""

from __future__ import annotations

import textwrap
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar, Final

import tree_sitter_java
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
    is_constant_name,
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
from parsers.sql_analysis import looks_like_sql

_GRAMMAR: Final = TsLanguage(tree_sitter_java.language())

_CLASS_KINDS: Final = {
    "class_declaration": ClassKind.CLASS,
    "interface_declaration": ClassKind.INTERFACE,
    "annotation_type_declaration": ClassKind.INTERFACE,
    "enum_declaration": ClassKind.ENUM,
    "record_declaration": ClassKind.RECORD,
}
_METHOD_TYPES: Final = frozenset(
    {"method_declaration", "constructor_declaration", "compact_constructor_declaration"}
)
_BUILDER_TYPES: Final = frozenset({"StringBuilder", "StringBuffer"})
_STRING_PASSTHROUGH: Final = frozenset(
    {
        "trim",
        "strip",
        "stripIndent",
        "stripLeading",
        "stripTrailing",
        "intern",
        "toString",
        "translateEscapes",
    }
)
_DOC_COMMENTS: Final = frozenset({"block_comment", "line_comment"})

_HTTP_MAPPINGS: Final = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": None,
}
_JAXRS_VERBS: Final = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
_MESSAGE_LISTENERS: Final = {
    "JmsListener": "destination",
    "KafkaListener": "topics",
    "RabbitListener": "queues",
    "SqsListener": "value",
    "StreamListener": "value",
    "EventListener": "value",
    "MessageMapping": "value",
}
_SCHEDULE_ANNOTATIONS: Final = frozenset({"Scheduled", "Schedule", "Schedules"})
_SERVLET_METHODS: Final = {
    "doGet": "GET",
    "doPost": "POST",
    "doPut": "PUT",
    "doDelete": "DELETE",
    "service": "ANY",
}


@dataclass(slots=True)
class _Annotation:
    name: str
    node: Node
    #: Argument nodes by element name; a single unnamed argument is stored as ``value``.
    arguments: dict[str, Node] = field(default_factory=dict)


class JavaExtractor(TreeSitterExtractor):
    language: ClassVar[Language] = Language.JAVA

    CALL_TYPES = frozenset(
        {
            "method_invocation",
            "object_creation_expression",
            "method_reference",
            "explicit_constructor_invocation",
        }
    )
    STRING_TYPES = frozenset({"string_literal", "binary_expression", "method_invocation"})
    ASSIGN_TYPES = frozenset({"variable_declarator", "assignment_expression"})
    APPEND_TYPES = frozenset({"method_invocation"})
    DECL_TYPES = frozenset({"local_variable_declaration", "enhanced_for_statement", "resource"})
    BRANCH_TYPES = frozenset(
        {
            "if_statement", "for_statement", "enhanced_for_statement", "while_statement", "do_statement",
            "catch_clause", "ternary_expression", "switch_label", "switch_rule", "lambda_expression",
        }
    )  # fmt: skip
    NESTED_TYPES = frozenset(_CLASS_KINDS)
    REF_TYPES = frozenset({"identifier", "field_access"})
    SKIP_TYPES = frozenset({"line_comment", "block_comment", "modifiers", "type_arguments", "annotation"})

    def grammar(self, path: str) -> TsLanguage:
        return _GRAMMAR

    # ------------------------------------------------------------------ module

    def extract_module(self, src: Source, root: Node, module: ModuleDef) -> None:
        scope = Scope()
        for child in root.named_children:
            kind = child.type
            if kind == "package_declaration":
                names = [c for c in child.named_children if c.type in ("scoped_identifier", "identifier")]
                if names:
                    module.package = src.text(names[0])
            elif kind == "import_declaration":
                imported = self._import(src, child)
                if imported is not None:
                    module.imports.append(imported)
            elif kind in _CLASS_KINDS:
                self._class(src, child, module, scope, owner=None)

    def _import(self, src: Source, node: Node) -> ImportDef | None:
        path: str | None = None
        wildcard = False
        static = False
        for child in node.children:
            if child.type in ("scoped_identifier", "identifier"):
                path = src.text(child)
            elif child.type == "asterisk":
                wildcard = True
            elif child.type == "static":
                static = True
        if path is None:
            return None
        if wildcard:
            return ImportDef(module=path, name="*", alias=None, line=line_of(node), is_static=static)
        package, _, name = path.rpartition(".")
        return ImportDef(module=package, name=name, alias=name, line=line_of(node), is_static=static)

    # ------------------------------------------------------------------- class

    def _class(
        self, src: Source, node: Node, module: ModuleDef, parent_scope: Scope, owner: ClassDef | None
    ) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None:
            return
        name = src.text(name_node)
        annotations, modifiers = self._modifiers(src, node)
        cls = ClassDef(
            name=name,
            qualified_name=f"{owner.qualified_name}.{name}" if owner else name,
            kind=_CLASS_KINDS[node.type],
            start_line=line_of(node),
            end_line=end_line_of(node),
            docstring=self._javadoc(src, node),
            decorators=[annotation.name for annotation in annotations],
            modifiers=modifiers,
            owner=owner.qualified_name if owner else None,
            name_line=line_of(name_node),
        )
        superclass = node.child_by_field_name("superclass")
        if superclass is not None:
            cls.bases.extend(self._type_names(src, superclass))
        interfaces = node.child_by_field_name("interfaces")
        if interfaces is not None:
            cls.interfaces.extend(self._type_names(src, interfaces))
        for child in node.children:
            if child.type == "extends_interfaces":  # interface A extends B, C
                cls.bases.extend(self._type_names(src, child))
        for annotation in annotations:
            if annotation.name == "Table":
                table = self._annotation_string(src, annotation, ("name", "value"), parent_scope.lookup)
                if table:
                    cls.table = table
        module.classes.append(cls)
        if node.type == "record_declaration":
            for param in self._params(src, node.child_by_field_name("parameters")):
                if param.type:
                    cls.fields[param.name] = param.type
        if body is None:
            return

        members: list[Node] = []
        for member in body.named_children:
            if member.type == "enum_body_declarations":
                members.extend(member.named_children)
            else:
                members.append(member)

        scope = Scope(parent_scope, cls.name)
        is_interface = cls.kind is ClassKind.INTERFACE
        # Pass 1: fields and constants, so method bodies can see constants declared below them.
        for member in members:
            if member.type in ("field_declaration", "constant_declaration"):
                self._field(src, member, cls, scope, constant_by_default=is_interface)
        # Pass 2: behaviour.
        initializer_count = 0
        for member in members:
            kind = member.type
            if kind in _METHOD_TYPES:
                self._method(src, member, module, cls, scope, annotations)
            elif kind in _CLASS_KINDS:
                self._class(src, member, module, scope, owner=cls)
            elif kind in ("static_initializer", "block"):
                initializer_count += 1
                self._initializer(src, member, module, cls, scope, initializer_count)

    def _field(
        self, src: Source, node: Node, cls: ClassDef, scope: Scope, *, constant_by_default: bool
    ) -> None:
        type_node = node.child_by_field_name("type")
        type_text = src.text(type_node) if type_node is not None else ""
        type_name = simple_type(type_text)
        _, modifiers = self._modifiers(src, node)
        is_final = constant_by_default or "final" in modifiers
        for declarator in node.children_by_field_name("declarator"):
            name_node = declarator.child_by_field_name("name")
            if name_node is None:
                continue
            name = src.text(name_node)
            if type_name:
                cls.fields[name] = type_name
            value = declarator.child_by_field_name("value")
            if value is None or type_name not in ("String", "CharSequence"):
                continue
            if not (is_final or is_constant_name(name)):
                continue
            folded = self.eval_string(src, value, scope.lookup)
            if folded is None or not folded.literal:
                continue
            definition = scope.define(name, folded, line_of(declarator), end_line_of(declarator))
            if definition is not None:
                cls.constants[name] = definition

    def _initializer(
        self, src: Source, node: Node, module: ModuleDef, cls: ClassDef, scope: Scope, index: int
    ) -> None:
        static = node.type == "static_initializer"
        name = "<clinit>" if static else "<init-block>"
        suffix = "" if index == 1 else f"#{index}"
        fn = FunctionDef(
            name=name,
            qualified_name=f"{cls.qualified_name}.{name}{suffix}",
            kind=FunctionKind.SCRIPT,
            start_line=line_of(node),
            end_line=end_line_of(node),
            signature="static { … }" if static else "{ … }",
            owner=cls.qualified_name,
            modifiers=["static"] if static else [],
        )
        self._finish_body(src, fn, [node], module, cls, scope)
        if fn.calls or fn.sql:
            cls.methods.append(fn)

    # ------------------------------------------------------------------ method

    def _method(
        self,
        src: Source,
        node: Node,
        module: ModuleDef,
        cls: ClassDef,
        scope: Scope,
        class_annotations: list[_Annotation],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = src.text(name_node)
        body = node.child_by_field_name("body")
        annotations, modifiers = self._modifiers(src, node)
        is_constructor = node.type != "method_declaration"
        params = self._params(src, node.child_by_field_name("parameters"))
        return_node = node.child_by_field_name("type")

        signature_start = max((a.node.end_byte for a in annotations), default=node.start_byte)
        signature_end = body.start_byte if body is not None else node.end_byte
        fn = FunctionDef(
            name=name,
            qualified_name=f"{cls.qualified_name}.{name}",
            kind=FunctionKind.CONSTRUCTOR if is_constructor else FunctionKind.METHOD,
            start_line=line_of(node),
            end_line=end_line_of(node),
            signature=collapse_ws(src.slice(signature_start, signature_end)).rstrip(";"),
            params=params,
            return_type=collapse_ws(src.text(return_node), 80) if return_node is not None else None,
            docstring=self._javadoc(src, node),
            decorators=[annotation.name for annotation in annotations],
            modifiers=modifiers,
            is_abstract=body is None and "native" not in modifiers,
            owner=cls.qualified_name,
            name_line=line_of(name_node),
        )
        for param in params:
            if param.type:
                fn.local_types[param.name] = param.type
        fn.entry_point = self._entry_point(src, fn, cls, annotations, class_annotations, scope.lookup)

        # SQL declared in annotations: @Query("..."), MyBatis @Select("..."), JDBI @SqlUpdate("...")
        for annotation in annotations:
            for argument in annotation.arguments.values():
                for candidate in self._annotation_values(argument):
                    folded = self.eval_string(src, candidate, scope.lookup)
                    if folded is not None and folded.literal and looks_like_sql(folded.text):
                        statement = make_sql_statement(
                            folded.text, line_of(candidate), end_line_of(candidate), dynamic=folded.dynamic
                        )
                        if statement is not None:
                            fn.sql.append(statement)

        if body is not None:
            self._finish_body(src, fn, [body], module, cls, scope)
        cls.methods.append(fn)

    def _finish_body(
        self, src: Source, fn: FunctionDef, roots: list[Node], module: ModuleDef, cls: ClassDef, scope: Scope
    ) -> None:
        for nested in self.visit_body(src, fn, roots, scope):
            # A named class declared inside a method body.
            self._class(src, nested, module, scope, owner=cls)

    def _params(self, src: Source, node: Node | None) -> list[Param]:
        params: list[Param] = []
        if node is None:
            return params
        for child in node.named_children:
            if child.type == "formal_parameter":
                name_node = child.child_by_field_name("name")
                type_node = child.child_by_field_name("type")
            elif child.type == "spread_parameter":
                type_node = next(
                    (
                        c
                        for c in child.named_children
                        if c.type.endswith("type") or c.type == "type_identifier"
                    ),
                    None,
                )
                declarator = next((c for c in child.named_children if c.type == "variable_declarator"), None)
                name_node = declarator.child_by_field_name("name") if declarator is not None else None
            else:
                continue
            if name_node is None:
                continue
            type_text = src.text(type_node) if type_node is not None else None
            params.append(Param(src.text(name_node), simple_type(type_text)))
        return params

    # ------------------------------------------------------------- annotations

    def _modifiers(self, src: Source, node: Node) -> tuple[list[_Annotation], list[str]]:
        annotations: list[_Annotation] = []
        modifiers: list[str] = []
        holder = next((child for child in node.children if child.type == "modifiers"), None)
        if holder is None:
            return annotations, modifiers
        for child in holder.children:
            if child.type in ("marker_annotation", "annotation"):
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                annotation = _Annotation(src.text(name_node).rsplit(".", 1)[-1], child)
                arguments = child.child_by_field_name("arguments")
                if arguments is not None:
                    for argument in arguments.named_children:
                        if argument.type == "element_value_pair":
                            key = argument.child_by_field_name("key")
                            value = argument.child_by_field_name("value")
                            if key is not None and value is not None:
                                annotation.arguments[src.text(key)] = value
                        else:
                            annotation.arguments["value"] = argument
                annotations.append(annotation)
            elif not child.is_named:
                modifiers.append(child.type)
        return annotations, modifiers

    @staticmethod
    def _annotation_values(node: Node) -> list[Node]:
        """An annotation argument is a single value or ``{a, b}``."""
        if node.type == "element_value_array_initializer":
            return list(node.named_children)
        return [node]

    def _annotation_string(
        self, src: Source, annotation: _Annotation, keys: tuple[str, ...], lookup: Lookup
    ) -> str | None:
        for key in keys:
            argument = annotation.arguments.get(key)
            if argument is None:
                continue
            for candidate in self._annotation_values(argument):
                folded = self.eval_string(src, candidate, lookup)
                if folded is not None and folded.literal:
                    return folded.text
                return collapse_ws(src.text(candidate), 80)
        return None

    def _entry_point(
        self,
        src: Source,
        fn: FunctionDef,
        cls: ClassDef,
        annotations: list[_Annotation],
        class_annotations: list[_Annotation],
        lookup: Lookup,
    ) -> EntryPoint | None:
        by_name = {annotation.name: annotation for annotation in annotations}
        class_by_name = {annotation.name: annotation for annotation in class_annotations}

        for name, verb in _HTTP_MAPPINGS.items():
            annotation = by_name.get(name)
            if annotation is None:
                continue
            prefix_annotation = class_by_name.get("RequestMapping")
            prefix = (
                self._annotation_string(src, prefix_annotation, ("value", "path"), lookup)
                if prefix_annotation
                else None
            )
            path = self._annotation_string(src, annotation, ("value", "path"), lookup)
            method = verb
            if method is None:
                declared = annotation.arguments.get("method")
                method = src.text(declared).rsplit(".", 1)[-1].strip("{} ") if declared is not None else "ANY"
            return EntryPoint("http", method=method, route=_join_route(prefix, path))

        jaxrs = next((verb for verb in _JAXRS_VERBS if verb in by_name), None)
        if jaxrs is not None:
            prefix_annotation = class_by_name.get("Path")
            prefix = (
                self._annotation_string(src, prefix_annotation, ("value",), lookup)
                if prefix_annotation
                else None
            )
            path_annotation = by_name.get("Path")
            path = (
                self._annotation_string(src, path_annotation, ("value",), lookup) if path_annotation else None
            )
            return EntryPoint("http", method=jaxrs, route=_join_route(prefix, path))

        for name in _SCHEDULE_ANNOTATIONS:
            annotation = by_name.get(name)
            if annotation is not None:
                detail = ", ".join(
                    f"{key}={collapse_ws(src.text(value), 60).strip(chr(34))}"
                    for key, value in annotation.arguments.items()
                )
                return EntryPoint("scheduled", detail=detail or None)

        for name, key in _MESSAGE_LISTENERS.items():
            annotation = by_name.get(name)
            if annotation is not None:
                return EntryPoint(
                    "message", detail=self._annotation_string(src, annotation, (key, "value"), lookup)
                )

        if (
            fn.name == "main"
            and "static" in fn.modifiers
            and "public" in fn.modifiers
            and len(fn.params) == 1
        ):
            return EntryPoint("main")
        ancestors = set(cls.bases) | set(cls.interfaces)
        if fn.name in _SERVLET_METHODS and "HttpServlet" in ancestors:
            servlet = class_by_name.get("WebServlet")
            route = (
                self._annotation_string(src, servlet, ("value", "urlPatterns"), lookup) if servlet else None
            )
            return EntryPoint("http", method=_SERVLET_METHODS[fn.name], route=route)
        if fn.name == "execute" and ancestors & {"Job", "QuartzJobBean", "InterruptableJob"}:
            return EntryPoint("scheduled", detail="Quartz job")
        if fn.name == "executeInternal" and "QuartzJobBean" in ancestors:
            return EntryPoint("scheduled", detail="Quartz job")
        if fn.name == "onMessage" and "MessageListener" in ancestors:
            return EntryPoint("message")
        return None

    def _javadoc(self, src: Source, node: Node) -> str | None:
        """Javadoc, or the plain comment lines a developer left directly above the declaration."""
        return leading_comment(src, node, _DOC_COMMENTS)

    def _type_names(self, src: Source, node: Node) -> list[str]:
        """Simple names of the types listed in an extends / implements clause."""
        names: list[str] = []
        stack = list(reversed(node.named_children))
        while stack:
            current = stack.pop()
            if current.type == "type_list":
                stack.extend(reversed(current.named_children))
                continue
            name = simple_type(src.text(current))
            if name:
                names.append(name)
        return names

    # ------------------------------------------------------- body visitor hooks

    def children_to_visit(self, node: Node) -> list[Node]:
        kind = node.type
        if kind == "method_invocation":
            # Skip the method name: it is not a variable or constant reference.
            return [
                c for c in (node.child_by_field_name("object"), node.child_by_field_name("arguments")) if c
            ]
        if kind == "field_access":
            target = node.child_by_field_name("object")
            return [target] if target is not None else []
        if kind == "variable_declarator":
            value = node.child_by_field_name("value")
            return [value] if value is not None else []
        if kind in ("method_reference", "labeled_statement", "break_statement", "continue_statement"):
            return []
        if kind in ("method_declaration", "constructor_declaration"):
            # A method of an anonymous class: only its body belongs to the enclosing method.
            body = node.child_by_field_name("body")
            return [body] if body is not None else []
        return node.named_children

    def call_site(self, src: Source, node: Node) -> CallSite | None:
        kind = node.type
        if kind == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return None
            target = node.child_by_field_name("object")
            return CallSite(
                name=src.text(name_node),
                line=line_of(name_node),
                receiver=self._receiver(src, target) if target is not None else None,
                arg_count=_arg_count(node.child_by_field_name("arguments")),
            )
        if kind == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            type_name = simple_type(src.text(type_node)) if type_node is not None else None
            if type_name is None:
                return None
            return CallSite(
                name=type_name,
                line=line_of(node),
                kind="new",
                arg_count=_arg_count(node.child_by_field_name("arguments")),
            )
        if kind == "method_reference":
            parts = node.children
            if len(parts) < 3:
                return None
            target, member = parts[0], parts[-1]
            if member.type == "new":
                type_name = simple_type(src.text(target))
                return CallSite(name=type_name, line=line_of(node), kind="new") if type_name else None
            return CallSite(
                name=src.text(member),
                line=line_of(node),
                receiver=self._receiver(src, target),
                kind="reference",
            )
        if kind == "explicit_constructor_invocation":
            constructor = node.child_by_field_name("constructor")
            which = src.text(constructor) if constructor is not None else "super"
            return CallSite(
                name=which,
                line=line_of(node),
                receiver=which,
                arg_count=_arg_count(node.child_by_field_name("arguments")),
                kind="super",
            )
        return None

    def _receiver(self, src: Source, node: Node) -> str:
        parts: list[str] = []
        current: Node | None = node
        for _ in range(6):
            if current is None:
                break
            kind = current.type
            if kind in ("identifier", "this", "super", "type_identifier"):
                parts.append(src.text(current))
                return ".".join(reversed(parts))
            if kind == "field_access":
                member = current.child_by_field_name("field")
                if member is None:
                    break
                parts.append(src.text(member))
                current = current.child_by_field_name("object")
                continue
            if kind == "scoped_type_identifier":
                return simple_type(src.text(current)) or "<expr>"
            if kind == "object_creation_expression" and not parts:
                type_node = current.child_by_field_name("type")
                type_name = simple_type(src.text(type_node)) if type_node is not None else None
                return f"type:{type_name}" if type_name else "<expr>"
            if kind == "parenthesized_expression" and not parts:
                inner = current.named_children[0] if current.named_children else None
                if inner is not None and inner.type == "cast_expression":
                    type_node = inner.child_by_field_name("type")
                    type_name = simple_type(src.text(type_node)) if type_node is not None else None
                    return f"type:{type_name}" if type_name else "<expr>"
                current = inner
                continue
            break
        return "<expr>"

    def literal_value(self, src: Source, node: Node, lookup: Lookup) -> StrVal | None:
        if node.type != "string_literal":
            return None
        parts: list[str] = []
        dynamic = False
        block = False
        for child in node.named_children:
            kind = child.type
            if kind == "string_fragment":
                parts.append(src.text(child))
            elif kind == "multiline_string_fragment":
                block = True
                parts.append(src.text(child))
            elif kind == "escape_sequence":
                parts.append(unescape(src.text(child)))
            else:  # string_interpolation (string templates)
                parts.append("${?}")
                dynamic = True
        text = "".join(parts)
        if block:
            text = textwrap.dedent(text.split("\n", 1)[1] if "\n" in text else text)
        return StrVal(text, dynamic=dynamic)

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
        if node.type != "method_invocation":
            return None
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = src.text(name_node)
        target = node.child_by_field_name("object")
        arguments = node.child_by_field_name("arguments")
        args = arguments.named_children if arguments is not None else []
        if name in _STRING_PASSTHROUGH and target is not None and not args:
            return self.eval_string(src, target, lookup)
        if name == "formatted" and target is not None:
            value = self.eval_string(src, target, lookup)
            return StrVal(value.text, dynamic=True, literal=value.literal) if value is not None else None
        if target is None or src.text(target) != "String":
            return None
        if name == "format":
            for candidate in args[:2]:  # an optional Locale may come first
                value = self.eval_string(src, candidate, lookup)
                if value is not None and value.literal:
                    return StrVal(value.text, dynamic=True)
            return None
        if name == "join" and len(args) >= 2:
            separator = self.eval_string(src, args[0], lookup)
            if separator is None or not separator.literal:
                return None
            pieces: list[str] = []
            dynamic = False
            literal = False
            for candidate in args[1:]:
                value = self.eval_string(src, candidate, lookup)
                if value is None:
                    pieces.append(self.placeholder(src, candidate))
                    dynamic = True
                else:
                    pieces.append(value.text)
                    dynamic = dynamic or value.dynamic
                    literal = literal or value.literal
            return StrVal(separator.text.join(pieces), dynamic=dynamic) if literal else None
        return None

    def assignment(self, src: Source, node: Node) -> Assignment | None:
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                return None
            return self._tracked_value(
                src, src.text(name_node), node.child_by_field_name("value"), augmented=False
            )
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        operator = node.child_by_field_name("operator")
        if left is None or right is None or operator is None or left.type != "identifier":
            return None
        if operator.type not in ("=", "+="):
            return None
        return self._tracked_value(src, src.text(left), right, augmented=operator.type == "+=")

    def _tracked_value(
        self, src: Source, target: str, value: Node | None, *, augmented: bool
    ) -> Assignment | None:
        if value is None:
            return None
        if value.type == "object_creation_expression":
            type_node = value.child_by_field_name("type")
            if type_node is None or simple_type(src.text(type_node)) not in _BUILDER_TYPES:
                return None
            arguments = value.child_by_field_name("arguments")
            initial = (
                arguments.named_children[0] if arguments is not None and arguments.named_child_count else None
            )
            return Assignment(target, initial, builder=True)
        return Assignment(target, value, augmented=augmented)

    def append(self, src: Source, node: Node) -> Append | None:
        if node.type != "method_invocation":
            return None
        arguments: list[Node] = []
        chain: list[int] = []
        current = node
        while True:
            name_node = current.child_by_field_name("name")
            if name_node is None or src.text(name_node) != "append":
                return None
            argument_list = current.child_by_field_name("arguments")
            if argument_list is None or argument_list.named_child_count != 1:
                return None
            arguments.append(argument_list.named_children[0])
            chain.append(current.id)
            target = current.child_by_field_name("object")
            if target is None:
                return None
            if target.type == "identifier":
                arguments.reverse()
                return Append(src.text(target), arguments, chain)
            if target.type != "method_invocation":
                return None
            current = target

    def local_types(self, src: Source, node: Node) -> Iterable[tuple[str, str]]:
        type_node = node.child_by_field_name("type")
        if type_node is None:
            return
        declared = simple_type(src.text(type_node))
        if node.type == "local_variable_declaration":
            for declarator in node.children_by_field_name("declarator"):
                name_node = declarator.child_by_field_name("name")
                if name_node is None:
                    continue
                type_name = declared
                if declared == "var":
                    value = declarator.child_by_field_name("value")
                    type_name = None
                    if value is not None and value.type == "object_creation_expression":
                        created = value.child_by_field_name("type")
                        type_name = simple_type(src.text(created)) if created is not None else None
                if type_name:
                    yield src.text(name_node), type_name
            return
        name_node = node.child_by_field_name("name")
        if name_node is not None and declared and declared != "var":
            yield src.text(name_node), declared

    def constant_ref(self, src: Source, node: Node) -> tuple[str | None, str] | None:
        if node.type == "identifier":
            return None, src.text(node)
        member = node.child_by_field_name("field")
        target = node.child_by_field_name("object")
        if member is None or target is None:
            return None
        name = src.text(member)
        if target.type == "this":
            return None, name
        if target.type not in ("identifier", "field_access"):
            return None
        qualifier = src.text(target).rsplit(".", 1)[-1]
        # ``Queries.FIND_ALL`` - a constant of another type; ``account.balance`` is not.
        return (qualifier, name) if qualifier[:1].isupper() else None


def _arg_count(arguments: Node | None) -> int | None:
    return arguments.named_child_count if arguments is not None else None


def _join_route(prefix: str | None, path: str | None) -> str | None:
    if not prefix and not path:
        return None
    joined = "/".join(part.strip("/") for part in (prefix or "", path or "") if part.strip("/"))
    return "/" + joined
