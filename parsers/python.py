"""Python extractor: modules, classes, functions, framework entry points, embedded SQL."""

from __future__ import annotations

import textwrap
from collections.abc import Iterable
from typing import ClassVar, Final

import tree_sitter_python
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
    line_of,
    simple_type,
    unescape,
)
from parsers.ir import (
    MODULE_SCOPE,
    CallSite,
    ClassDef,
    ClassKind,
    ConstantDef,
    EntryPoint,
    FunctionDef,
    FunctionKind,
    ImportDef,
    Language,
    ModuleDef,
    Param,
)

_GRAMMAR: Final = TsLanguage(tree_sitter_python.language())

_DEFINITIONS: Final = frozenset({"function_definition", "class_definition", "decorated_definition"})
_IMPORTS: Final = frozenset({"import_statement", "import_from_statement", "future_import_statement"})
_HTTP_DECORATORS: Final = frozenset(
    {"route", "get", "post", "put", "delete", "patch", "head", "options", "api_route", "websocket"}
)
_TASK_DECORATORS: Final = frozenset({"task", "shared_task", "actor", "job", "periodic_task"})
_SCHEDULE_DECORATORS: Final = frozenset({"scheduled_job", "repeat_every", "cron", "crontab", "scheduled"})
_CLI_DECORATORS: Final = frozenset({"command", "group"})
_EVENT_DECORATORS: Final = frozenset(
    {"receiver", "listens_for", "on_event", "event_handler", "subscribe", "consumer"}
)
_VIEW_BASES: Final = frozenset(
    {
        "View", "APIView", "GenericAPIView",
        "ViewSet", "ModelViewSet", "ReadOnlyModelViewSet", "GenericViewSet",
        "TemplateView", "ListView", "DetailView", "FormView", "MethodView", "Resource", "RequestHandler",
        "HTTPEndpoint",
    }
)  # fmt: skip
_VIEW_METHODS: Final = {
    "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE", "head": "HEAD",
    "options": "OPTIONS", "list": "GET", "retrieve": "GET", "create": "POST", "update": "PUT",
    "partial_update": "PATCH", "destroy": "DELETE",
}  # fmt: skip
_STRING_PASSTHROUGH: Final = frozenset({"strip", "lstrip", "rstrip", "dedent"})
_NOT_CALLBACKS: Final = frozenset({"self", "cls", "None", "True", "False", "__name__", "__file__"})


class PythonExtractor(TreeSitterExtractor):
    language: ClassVar[Language] = Language.PYTHON

    CALL_TYPES = frozenset({"call"})
    STRING_TYPES = frozenset({"string", "concatenated_string", "binary_operator", "call"})
    ASSIGN_TYPES = frozenset({"assignment", "augmented_assignment"})
    APPEND_TYPES = frozenset({"call"})
    BINDING_TYPES = frozenset({"lambda", "for_statement", "for_in_clause", "as_pattern", "assignment"})
    DECL_TYPES = frozenset({"assignment", "as_pattern"})
    BRANCH_TYPES = frozenset(
        {
            "if_statement", "elif_clause", "for_statement", "while_statement", "except_clause",
            "conditional_expression", "boolean_operator", "for_in_clause",
            "if_clause", "case_clause", "lambda",
        }
    )  # fmt: skip
    NESTED_TYPES = _DEFINITIONS
    REF_TYPES = frozenset({"identifier", "attribute"})
    SKIP_TYPES = frozenset(
        {"comment", "type", "import_statement", "import_from_statement", "future_import_statement"}
    )

    def grammar(self, path: str) -> TsLanguage:
        return _GRAMMAR

    # ------------------------------------------------------------------ module

    def extract_module(self, src: Source, root: Node, module: ModuleDef) -> None:
        scope = Scope()
        statements = root.named_children
        module.docstring = self._docstring(src, root)
        script_roots: list[Node] = []
        is_main = False

        # Pass 1: imports and constants, so function bodies see constants defined below them.
        constant_statements: set[int] = set()
        for statement in statements:
            if statement.type in _IMPORTS:
                module.imports.extend(self._imports(src, statement))
            elif (
                statement.type == "expression_statement"
                and self._constant(src, statement, scope, module.constants) is not None
            ):
                # Defining a query is not running it: keep it out of the module scope.
                constant_statements.add(statement.id)

        for statement in statements:
            kind = statement.type
            if kind in _DEFINITIONS:
                self._definition(src, statement, module, scope, cls=None, parent=None)
            elif kind not in _IMPORTS and kind != "comment" and statement.id not in constant_statements:
                script_roots.append(statement)
                if kind == "if_statement" and _is_main_guard(src, statement):
                    is_main = True

        script = FunctionDef(
            name=MODULE_SCOPE,
            qualified_name=MODULE_SCOPE,
            kind=FunctionKind.SCRIPT,
            start_line=1,
            end_line=max(module.line_count, 1),
            signature="module-level code",
            entry_point=EntryPoint("main") if is_main else None,
        )
        for nested in self.visit_body(src, script, script_roots, scope):
            # Definitions inside module-level ``if`` / ``try`` blocks.
            self._definition(src, nested, module, scope, cls=None, parent=None)
        if script.calls or script.sql or is_main:
            script.local_types.clear()
            module.functions.append(script)

    def _imports(self, src: Source, node: Node) -> list[ImportDef]:
        imports: list[ImportDef] = []
        line = line_of(node)
        if node.type == "future_import_statement":
            return imports
        if node.type == "import_statement":
            for name in node.children_by_field_name("name"):
                if name.type == "aliased_import":
                    target = name.child_by_field_name("name")
                    alias = name.child_by_field_name("alias")
                    if target is not None and alias is not None:
                        imports.append(ImportDef(src.text(target), None, src.text(alias), line))
                else:
                    module = src.text(name)
                    # ``import a.b.c`` binds the top-level package name ``a``.
                    imports.append(ImportDef(module, None, module.split(".", 1)[0], line))
            return imports

        module_node = node.child_by_field_name("module_name")
        module_name = ""
        level = 0
        if module_node is not None:
            if module_node.type == "relative_import":
                for part in module_node.children:
                    if part.type == "import_prefix":
                        level = len(src.text(part))
                    elif part.type == "dotted_name":
                        module_name = src.text(part)
            else:
                module_name = src.text(module_node)
        if any(child.type == "wildcard_import" for child in node.children):
            imports.append(ImportDef(module_name, "*", None, line, level=level))
        for name in node.children_by_field_name("name"):
            if name.type == "aliased_import":
                target = name.child_by_field_name("name")
                alias = name.child_by_field_name("alias")
                if target is not None and alias is not None:
                    imports.append(
                        ImportDef(module_name, src.text(target), src.text(alias), line, level=level)
                    )
            else:
                imported = src.text(name)
                imports.append(ImportDef(module_name, imported, imported, line, level=level))
        return imports

    def _constant(
        self, src: Source, statement: Node, scope: Scope, sink: dict[str, ConstantDef]
    ) -> tuple[str, StrVal] | None:
        """Register ``NAME = "string"`` at module or class level."""
        assignment = statement.named_children[0] if statement.named_child_count else None
        if assignment is None or assignment.type != "assignment":
            return None
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier":
            return None
        name = src.text(left)
        folded = self.eval_string(src, right, scope.lookup)
        if folded is None or not folded.literal:
            return None
        definition = scope.define(name, folded, line_of(assignment), end_line_of(assignment))
        if definition is not None:
            sink[name] = definition
        return name, folded

    # -------------------------------------------------------------- definitions

    def _definition(
        self,
        src: Source,
        node: Node,
        module: ModuleDef,
        scope: Scope,
        *,
        cls: ClassDef | None,
        parent: FunctionDef | None,
    ) -> None:
        decorators: list[Node] = []
        outer = node
        if node.type == "decorated_definition":
            decorators = [child for child in node.children if child.type == "decorator"]
            inner = node.child_by_field_name("definition")
            if inner is None:
                return
            node = inner
        if node.type == "class_definition":
            self._class(src, node, outer, decorators, module, scope, owner=cls, parent=parent)
        elif node.type == "function_definition":
            self._function(src, node, outer, decorators, module, scope, cls=cls, parent=parent)

    def _class(
        self,
        src: Source,
        node: Node,
        outer: Node,
        decorators: list[Node],
        module: ModuleDef,
        parent_scope: Scope,
        *,
        owner: ClassDef | None,
        parent: FunctionDef | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:
            return
        name = src.text(name_node)
        prefix = owner.qualified_name if owner else (parent.qualified_name if parent else None)
        cls = ClassDef(
            name=name,
            qualified_name=f"{prefix}.{name}" if prefix else name,
            kind=ClassKind.CLASS,
            start_line=line_of(outer),
            end_line=end_line_of(outer),
            docstring=self._docstring(src, body),
            decorators=[self._decorator_name(src, decorator) for decorator in decorators],
            owner=owner.qualified_name if owner else None,
            name_line=line_of(name_node),
        )
        superclasses = node.child_by_field_name("superclasses")
        if superclasses is not None:
            for base in superclasses.named_children:
                if base.type == "keyword_argument":
                    continue
                base_name = simple_type(src.text(base))
                if base_name:
                    cls.bases.append(base_name)
        if cls.bases and all(base in ("Protocol", "ABC") for base in cls.bases):
            cls.kind = ClassKind.INTERFACE
        if any(base in ("Enum", "IntEnum", "StrEnum", "Flag") for base in cls.bases):
            cls.kind = ClassKind.ENUM
        module.classes.append(cls)

        scope = Scope(parent_scope, cls.name)
        members = body.named_children
        for member in members:
            if member.type != "expression_statement":
                continue
            defined = self._constant(src, member, scope, cls.constants)
            if defined is not None and defined[0] == "__tablename__":
                cls.table = defined[1].text
            self._class_attribute(src, member, cls)
        for member in members:
            if member.type in _DEFINITIONS:
                self._definition(src, member, module, scope, cls=cls, parent=None)
        cls.constants.pop("__tablename__", None)
        if owner is not None and name == "Meta":
            # Django: ``class Meta: db_table = "..."`` configures the enclosing model.
            db_table = scope.values.get("db_table")
            if db_table is not None:
                owner.table = db_table.text

    def _class_attribute(self, src: Source, statement: Node, cls: ClassDef) -> None:
        assignment = statement.named_children[0] if statement.named_child_count else None
        if assignment is None or assignment.type != "assignment":
            return
        left = assignment.child_by_field_name("left")
        if left is None or left.type != "identifier":
            return
        declared = assignment.child_by_field_name("type")
        type_name = simple_type(src.text(declared)) if declared is not None else None
        if type_name is None:
            type_name = self._constructed_type(src, assignment.child_by_field_name("right"))
        if type_name:
            cls.fields[src.text(left)] = type_name

    def _function(
        self,
        src: Source,
        node: Node,
        outer: Node,
        decorators: list[Node],
        module: ModuleDef,
        scope: Scope,
        *,
        cls: ClassDef | None,
        parent: FunctionDef | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:
            return
        name = src.text(name_node)
        decorator_names = [self._decorator_name(src, decorator) for decorator in decorators]
        params = self._params(src, node.child_by_field_name("parameters"))
        is_method = cls is not None and parent is None
        static = "staticmethod" in decorator_names
        receiver_name: str | None = None
        if is_method and not static and params:
            receiver_name = params[0].name  # ``self`` / ``cls``
            params = params[1:]
        return_node = node.child_by_field_name("return_type")
        prefix = parent.qualified_name if parent else (cls.qualified_name if cls else None)
        modifiers: list[str] = [
            flag for flag in ("staticmethod", "classmethod", "property") if flag in decorator_names
        ]
        if any(child.type == "async" for child in node.children):
            modifiers.append("async")

        fn = FunctionDef(
            name=name,
            qualified_name=f"{prefix}.{name}" if prefix else name,
            kind=(
                FunctionKind.FUNCTION
                if not is_method
                else FunctionKind.CONSTRUCTOR
                if name == "__init__"
                else FunctionKind.METHOD
            ),
            start_line=line_of(outer),
            end_line=end_line_of(outer),
            signature=collapse_ws(src.slice(node.start_byte, body.start_byte)).rstrip(": "),
            params=params,
            return_type=collapse_ws(src.text(return_node), 80) if return_node is not None else None,
            docstring=self._docstring(src, body),
            decorators=decorator_names,
            modifiers=modifiers,
            is_abstract="abstractmethod" in decorator_names,
            owner=cls.qualified_name if is_method and cls else None,
            parent_function=parent.qualified_name if parent else None,
            name_line=line_of(name_node),
        )
        for param in params:
            if param.type:
                fn.local_types[param.name] = param.type
        fn.entry_point = self._entry_point(src, fn, cls if is_method else None, decorators)

        nested = self.visit_body(src, fn, [body], scope)
        self._collect_fields(fn, cls if is_method else None, receiver_name)
        (cls.methods if is_method and cls is not None else module.functions).append(fn)
        for definition in nested:
            self._definition(src, definition, module, scope, cls=None, parent=fn)

    def _collect_fields(self, fn: FunctionDef, cls: ClassDef | None, receiver_name: str | None) -> None:
        """Turn ``self.x = Foo()`` / ``self.x = typed_param`` into class field types."""
        for key in [key for key in fn.local_types if "." in key]:
            value = fn.local_types.pop(key)
            if cls is None or receiver_name is None:
                continue
            receiver, _, attribute = key.partition(".")
            if receiver != receiver_name:
                continue
            type_name = fn.local_types.get(value[1:]) if value.startswith("@") else value
            if type_name:
                cls.fields.setdefault(attribute, type_name)
        for key in [key for key, value in fn.local_types.items() if value.startswith("@")]:
            source = fn.local_types.get(fn.local_types[key][1:])
            if source and not source.startswith("@"):
                fn.local_types[key] = source
            else:
                del fn.local_types[key]

    def _params(self, src: Source, node: Node | None) -> list[Param]:
        params: list[Param] = []
        if node is None:
            return params
        for child in node.named_children:
            kind = child.type
            if kind == "identifier":
                params.append(Param(src.text(child)))
            elif kind in ("typed_parameter", "default_parameter", "typed_default_parameter"):
                name_node = child.child_by_field_name("name") or next(
                    (c for c in child.named_children if c.type == "identifier"), None
                )
                type_node = child.child_by_field_name("type")
                if name_node is None:
                    continue
                type_name = simple_type(_unquote(src.text(type_node))) if type_node is not None else None
                params.append(Param(src.text(name_node), type_name))
            elif kind in ("list_splat_pattern", "dictionary_splat_pattern"):
                inner = next((c for c in child.named_children if c.type == "identifier"), None)
                if inner is not None:
                    params.append(Param(src.text(child)))
        return params

    def _docstring(self, src: Source, body: Node) -> str | None:
        first = body.named_children[0] if body.named_child_count else None
        if first is None or first.type != "expression_statement" or not first.named_child_count:
            return None
        literal = first.named_children[0]
        if literal.type not in ("string", "concatenated_string"):
            return None
        value = self.literal_value(src, literal, lambda _name: None)
        if value is None or value.dynamic:
            return None
        text = textwrap.dedent(value.text).strip()
        if not text:
            return None
        return text if len(text) <= 1200 else text[:1199].rstrip() + "…"

    def _decorator_name(self, src: Source, decorator: Node) -> str:
        expression = decorator.named_children[0] if decorator.named_child_count else None
        if expression is None:
            return ""
        if expression.type == "call":
            function = expression.child_by_field_name("function")
            return src.text(function) if function is not None else ""
        return src.text(expression)

    def _entry_point(
        self, src: Source, fn: FunctionDef, cls: ClassDef | None, decorators: list[Node]
    ) -> EntryPoint | None:
        for decorator in decorators:
            expression = decorator.named_children[0] if decorator.named_child_count else None
            if expression is None:
                continue
            call = expression if expression.type == "call" else None
            target = call.child_by_field_name("function") if call is not None else expression
            if target is None:
                continue
            full_name = src.text(target)
            last = full_name.rsplit(".", 1)[-1]
            arguments = call.child_by_field_name("arguments") if call is not None else None
            positional = (
                [a for a in arguments.named_children if a.type != "keyword_argument"] if arguments else []
            )
            keywords = {
                src.text(key): value
                for a in (arguments.named_children if arguments else [])
                if a.type == "keyword_argument"
                and (key := a.child_by_field_name("name")) is not None
                and (value := a.child_by_field_name("value")) is not None
            }
            first_string = self._plain_string(src, positional[0]) if positional else None

            if (
                last in _HTTP_DECORATORS
                and "." in full_name
                and (first_string is not None or call is not None)
            ):
                method = "ANY" if last in ("route", "api_route") else last.upper()
                methods = keywords.get("methods")
                if methods is not None:
                    listed = [self._plain_string(src, item) for item in methods.named_children]
                    method = ",".join(item for item in listed if item) or method
                return EntryPoint("http", method=method, route=first_string)
            if last in _TASK_DECORATORS:
                return EntryPoint("task", detail=full_name)
            if last in _SCHEDULE_DECORATORS:
                return EntryPoint("scheduled", detail=collapse_ws(src.text(expression), 100))
            if last in _CLI_DECORATORS and "." in full_name:
                return EntryPoint("cli", detail=first_string or fn.name)
            if last in _EVENT_DECORATORS:
                return EntryPoint("message", detail=collapse_ws(src.text(expression), 100))
        if cls is not None and fn.name in _VIEW_METHODS and set(cls.bases) & _VIEW_BASES:
            return EntryPoint("http", method=_VIEW_METHODS[fn.name])
        names = [param.name for param in fn.params]
        if (
            fn.owner is None
            and fn.parent_function is None
            and (fn.name == "lambda_handler" or names == ["event", "context"])
        ):
            return EntryPoint("message", detail="AWS Lambda handler")
        return None

    def _plain_string(self, src: Source, node: Node) -> str | None:
        if node.type != "string":
            return None
        value = self.literal_value(src, node, lambda _name: None)
        return value.text if value is not None and not value.dynamic else None

    def _constructed_type(self, src: Source, value: Node | None) -> str | None:
        """``Foo(...)`` / ``pkg.Foo(...)`` -> ``Foo`` when it looks like a class instantiation."""
        if value is None:
            return None
        if value.type == "await" and value.named_child_count:
            value = value.named_children[0]
        if value.type != "call":
            return None
        function = value.child_by_field_name("function")
        if function is None or function.type not in ("identifier", "attribute"):
            return None
        name = src.text(function).rsplit(".", 1)[-1]
        return name if name[:1].isupper() and not name.isupper() else None

    # ------------------------------------------------------- body visitor hooks

    def children_to_visit(self, node: Node) -> list[Node]:
        kind = node.type
        if kind == "attribute":
            target = node.child_by_field_name("object")
            return [target] if target is not None else []
        if kind == "keyword_argument":
            value = node.child_by_field_name("value")
            return [value] if value is not None else []
        if kind == "assignment":
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                right = node.child_by_field_name("right")
                return [right] if right is not None else []
        if kind == "lambda":
            body = node.child_by_field_name("body")
            return [body] if body is not None else []
        return node.named_children

    def call_site(self, src: Source, node: Node) -> CallSite | None:
        function = node.child_by_field_name("function")
        if function is None:
            return None
        arguments = node.child_by_field_name("arguments")
        arg_count = (
            arguments.named_child_count
            if arguments is not None and arguments.type == "argument_list"
            else None
        )
        if function.type == "identifier":
            name = src.text(function)
            if name == "super":
                return None
            return CallSite(name=name, line=line_of(function), arg_count=arg_count)
        if function.type == "attribute":
            member = function.child_by_field_name("attribute")
            target = function.child_by_field_name("object")
            if member is None or target is None:
                return None
            return CallSite(
                name=src.text(member),
                line=line_of(member),
                receiver=self._receiver(src, target),
                arg_count=arg_count,
            )
        return None

    def _receiver(self, src: Source, node: Node) -> str:
        parts: list[str] = []
        current: Node | None = node
        for _ in range(6):
            if current is None:
                break
            if current.type == "identifier":
                parts.append(src.text(current))
                return ".".join(reversed(parts))
            if current.type == "attribute":
                member = current.child_by_field_name("attribute")
                if member is None:
                    break
                parts.append(src.text(member))
                current = current.child_by_field_name("object")
                continue
            if current.type == "call" and not parts:
                function = current.child_by_field_name("function")
                if function is not None and function.type == "identifier":
                    name = src.text(function)
                    if name == "super":
                        return "super"
                    if name[:1].isupper():
                        return f"type:{name}"
            break
        return "<expr>"

    def literal_value(self, src: Source, node: Node, lookup: Lookup) -> StrVal | None:
        if node.type == "concatenated_string":
            parts = [
                self.literal_value(src, child, lookup)
                for child in node.named_children
                if child.type == "string"
            ]
            if not parts or any(part is None for part in parts):
                return None
            return StrVal("".join(p.text for p in parts if p), dynamic=any(p.dynamic for p in parts if p))
        if node.type != "string":
            return None
        raw = False
        pieces: list[str] = []
        dynamic = False
        for child in node.children:
            kind = child.type
            if kind == "string_start":
                raw = "r" in src.text(child).lower().rstrip("'\"")
            elif kind == "string_content":
                text = src.text(child)
                pieces.append(text if raw else unescape(text))
            elif kind == "interpolation":
                expression = child.child_by_field_name("expression")
                value = self.eval_string(src, expression, lookup) if expression is not None else None
                if value is not None and value.literal:
                    pieces.append(value.text)
                    dynamic = dynamic or value.dynamic
                else:
                    pieces.append(self.placeholder(src, expression) if expression is not None else "${?}")
                    dynamic = True
        text = "".join(pieces)
        if "\n" in text:
            text = textwrap.dedent(text)
        return StrVal(text, dynamic=dynamic)

    def concat_operands(self, node: Node) -> list[Node] | None:
        return flatten_binary(node, "+", "binary_operator")

    def unwrap(self, node: Node) -> Node:
        current = node
        for _ in range(8):
            if current.type in ("parenthesized_expression", "await") and current.named_child_count == 1:
                current = current.named_children[0]
            else:
                break
        return current

    def special_string(self, src: Source, node: Node, lookup: Lookup) -> StrVal | None:
        if node.type == "binary_operator":
            operator = node.child_by_field_name("operator")
            left = node.child_by_field_name("left")
            if operator is not None and operator.type == "%" and left is not None:
                value = self.eval_string(src, left, lookup)
                if value is not None and value.literal:
                    return StrVal(value.text, dynamic=True)
            return None
        if node.type != "call":
            return None
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None or arguments.type != "argument_list":
            return None
        args = arguments.named_children
        if function.type == "identifier":
            if src.text(function) == "dedent" and len(args) == 1:
                return self.eval_string(src, args[0], lookup)
            return None
        if function.type != "attribute":
            return None
        member = function.child_by_field_name("attribute")
        target = function.child_by_field_name("object")
        if member is None or target is None:
            return None
        name = src.text(member)
        if name == "format":
            value = self.eval_string(src, target, lookup)
            return StrVal(value.text, dynamic=True) if value is not None and value.literal else None
        if name in _STRING_PASSTHROUGH:
            if name == "dedent":
                return self.eval_string(src, args[0], lookup) if len(args) == 1 else None
            return self.eval_string(src, target, lookup) if not args else None
        if name == "join" and len(args) == 1 and args[0].type in ("list", "tuple"):
            separator = self.eval_string(src, target, lookup)
            if separator is None or not separator.literal:
                return None
            pieces: list[str] = []
            dynamic = False
            literal = False
            for item in args[0].named_children:
                value = self.eval_string(src, item, lookup)
                if value is None:
                    pieces.append(self.placeholder(src, item))
                    dynamic = True
                else:
                    pieces.append(value.text)
                    dynamic = dynamic or value.dynamic
                    literal = literal or value.literal
            return StrVal(separator.text.join(pieces), dynamic=dynamic) if literal else None
        return None

    def assignment(self, src: Source, node: Node) -> Assignment | None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier":
            return None
        if node.type == "augmented_assignment":
            operator = node.child_by_field_name("operator")
            if operator is None or operator.type != "+=":
                return None
            return Assignment(src.text(left), right, augmented=True)
        if right.type == "list":
            items = right.named_children
            if all(item.type in ("string", "concatenated_string") for item in items):
                # parts = ["SELECT ...", "FROM t"]; parts.append("WHERE ..."); " ".join(parts)
                return Assignment(src.text(left), None, builder=True, parts=list(items), separator=" ")
        return Assignment(src.text(left), right)

    def append(self, src: Source, node: Node) -> Append | None:
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            function is None
            or arguments is None
            or function.type != "attribute"
            or arguments.type != "argument_list"
        ):
            return None
        target = function.child_by_field_name("object")
        member = function.child_by_field_name("attribute")
        if (
            target is None
            or member is None
            or target.type != "identifier"
            or arguments.named_child_count != 1
        ):
            return None
        name = src.text(member)
        argument = arguments.named_children[0]
        if name == "append":
            return Append(src.text(target), [argument], [node.id])
        if name == "extend" and argument.type in ("list", "tuple"):
            return Append(src.text(target), list(argument.named_children), [node.id])
        return None

    def bound_names(self, src: Source, node: Node) -> Iterable[str]:
        kind = node.type
        if kind == "lambda":
            holder = node.child_by_field_name("parameters")
        elif kind == "as_pattern":
            holder = node.named_children[-1] if node.named_child_count == 2 else None
        else:
            holder = node.child_by_field_name("left")
        if holder is None:
            return
        if holder.type == "identifier":
            yield src.text(holder)
            return
        stack = [holder]
        budget = 100
        while stack and budget:
            budget -= 1
            current = stack.pop()
            for child in current.named_children:
                if child.type == "identifier":
                    yield src.text(child)
                elif child.type not in ("attribute", "subscript", "type", "call"):
                    stack.append(child)

    def extra_calls(self, src: Source, node: Node) -> Iterable[CallSite]:
        """Functions handed to another call: ``Thread(target=self.run)``, ``pool.map(process, xs)``."""
        arguments = node.child_by_field_name("arguments")
        if arguments is None or arguments.type != "argument_list":
            return
        for argument in arguments.named_children:
            value = argument.child_by_field_name("value") if argument.type == "keyword_argument" else argument
            if value is None:
                continue
            if value.type == "identifier":
                name = src.text(value)
                if name not in _NOT_CALLBACKS:
                    yield CallSite(name, line_of(value), kind="reference")
            elif value.type == "attribute":
                target = value.child_by_field_name("object")
                member = value.child_by_field_name("attribute")
                if target is not None and member is not None and target.type == "identifier":
                    receiver = src.text(target)
                    if receiver in ("self", "cls"):
                        yield CallSite(src.text(member), line_of(value), receiver=receiver, kind="reference")

    def local_types(self, src: Source, node: Node) -> Iterable[tuple[str, str]]:
        if node.type == "as_pattern":  # with Foo() as x
            children = node.named_children
            if len(children) == 2 and children[1].type == "as_pattern_target":
                type_name = self._constructed_type(src, children[0])
                if type_name:
                    yield src.text(children[1]), type_name
            return
        left = node.child_by_field_name("left")
        if left is None:
            return
        if left.type == "identifier":
            key = src.text(left)
        elif left.type == "attribute":
            target = left.child_by_field_name("object")
            member = left.child_by_field_name("attribute")
            if target is None or member is None or target.type != "identifier":
                return
            key = f"{src.text(target)}.{src.text(member)}"
        else:
            return
        declared = node.child_by_field_name("type")
        if declared is not None:
            type_name = simple_type(_unquote(src.text(declared)))
            if type_name:
                yield key, type_name
            return
        right = node.child_by_field_name("right")
        type_name = self._constructed_type(src, right)
        if type_name:
            yield key, type_name
        elif right is not None and right.type == "identifier":
            # ``self.dao = dao``: resolved against the parameter's annotation afterwards.
            yield key, "@" + src.text(right)

    def constant_ref(self, src: Source, node: Node) -> tuple[str | None, str] | None:
        if node.type == "identifier":
            return None, src.text(node)
        member = node.child_by_field_name("attribute")
        target = node.child_by_field_name("object")
        if member is None or target is None or target.type not in ("identifier", "attribute"):
            return None
        name = src.text(member)
        qualifier = src.text(target).rsplit(".", 1)[-1]
        if qualifier in ("self", "cls"):
            return None, name
        return qualifier, name


def _is_main_guard(src: Source, node: Node) -> bool:
    condition = node.child_by_field_name("condition")
    if condition is None:
        return False
    text = src.text(condition).replace(" ", "").replace("'", '"')
    return text in ('__name__=="__main__"', '"__main__"==__name__')


def _unquote(text: str) -> str:
    """Forward references are written as strings: ``"Foo"`` -> ``Foo``."""
    return text.strip().strip("\"'")
