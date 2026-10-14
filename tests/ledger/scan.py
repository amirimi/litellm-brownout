from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

ReadKind = Literal[
    "key",
    "env",
    "flag",
    "endpoint",
    "litellm",
    "router",
    "member",
    "parameter",
    "distribution",
]

LITELLM_PACKAGE: Final = "litellm"
DISTRIBUTION_MODULE: Final = "importlib.metadata"
LITELLM_VALUE_TYPES: Final = frozenset({"UserAPIKeyAuth"})
ROUTER_ACCESSORS: Final = frozenset({"_router", "live_router"})
HOOK_PREFIX: Final = "async_"
MAPPING_READER: Final = "get"
ENVIRONMENT_READER: Final = "getenv"
ENVIRONMENT_MAPPING: Final = "os.environ"
FLAG_DECLARERS: Final = frozenset({"add_argument", "add_parser"})
ENDPOINT_READER: Final = "_get"
ENDPOINT_PREFIX: Final = "/"
FROZENSET: Final = "frozenset"
SELF: Final = "self"
MODULE_SCOPE: Final = "<module>"
PYTHON_FILES: Final = "*.py"

Comprehension = ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, order=True)
class Read:
    module: str
    function: str
    kind: ReadKind
    name: str

    @property
    def site(self) -> str:
        return f"{self.module}.{self.function}"

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass(frozen=True)
class ModuleFacts:
    constants: Mapping[str, tuple[str, ...]]
    litellm_names: frozenset[str]
    litellm_modules: frozenset[str]
    distribution_names: frozenset[str]


@dataclass
class Scope:
    members: set[str] = field(default_factory=set)
    routers: set[str] = field(default_factory=set)
    parameters: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)
    bound: dict[str, tuple[str, ...]] = field(default_factory=dict)


def scan_package(package: Path) -> tuple[Read, ...]:
    reads: set[Read] = set()
    for path in sorted(package.glob(PYTHON_FILES)):
        reads.update(scan_module(path))
    return tuple(sorted(reads))


def scan_module(path: Path) -> list[Read]:
    tree: Final = ast.parse(path.read_text(encoding="utf-8"))
    scanner: Final = _Scanner(path.stem, _facts(tree))
    scanner.visit(tree)
    return scanner.reads


def package_functions(package: Path) -> frozenset[str]:
    functions: set[str] = set()
    for path in sorted(package.glob(PYTHON_FILES)):
        functions.update(f"{path.stem}.{name}" for name in qualified_functions(path))
    return frozenset(functions)


def qualified_functions(path: Path) -> frozenset[str]:
    tree: Final = ast.parse(path.read_text(encoding="utf-8"))
    return frozenset(_qualified(tree.body, ""))


def _qualified(statements: Sequence[ast.stmt], prefix: str) -> list[str]:
    names: list[str] = []
    for statement in statements:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            names.append(f"{prefix}{statement.name}")
        if isinstance(statement, ast.ClassDef):
            names.append(f"{prefix}{statement.name}")
            names.extend(_qualified(statement.body, f"{prefix}{statement.name}."))
    return names


def _facts(tree: ast.Module) -> ModuleFacts:
    return ModuleFacts(
        constants=_constants(tree),
        litellm_names=_imported_names(tree.body, LITELLM_PACKAGE),
        litellm_modules=_imported_modules(tree, LITELLM_PACKAGE),
        distribution_names=_imported_names(tree.body, DISTRIBUTION_MODULE),
    )


def _within(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _imported_names(statements: Sequence[ast.stmt], package: str) -> frozenset[str]:
    names: set[str] = set()
    for node in statements:
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if _within(node.module, package):
            names.update(alias.asname or alias.name for alias in node.names)
    return frozenset(names)


def _imported_modules(tree: ast.Module, package: str) -> frozenset[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        names.update(
            alias.asname or alias.name.split(".")[0]
            for alias in node.names
            if _within(alias.name, package)
        )
    return frozenset(names)


def _constants(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    constants: dict[str, tuple[str, ...]] = {}
    for statement in tree.body:
        assigned = _assignment(statement)
        if assigned is None:
            continue
        strings = _strings(assigned[1])
        if strings:
            constants[assigned[0]] = strings
    return constants


def _assignment(statement: ast.stmt) -> tuple[str, ast.expr] | None:
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        if statement.value is None:
            return None
        return (statement.target.id, statement.value)
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        if isinstance(target, ast.Name):
            return (target.id, statement.value)
    return None


def _strings(value: ast.expr) -> tuple[str, ...]:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return (value.value,)
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == FROZENSET
        and len(value.args) == 1
    ):
        return _strings(value.args[0])
    if isinstance(value, ast.Tuple | ast.List | ast.Set):
        elements = [_strings(element) for element in value.elts]
        if all(len(element) == 1 for element in elements):
            return tuple(element[0] for element in elements)
    return ()


class _Scanner(ast.NodeVisitor):
    def __init__(self, module: str, facts: ModuleFacts) -> None:
        self.module: Final = module
        self.facts: Final = facts
        self.names: list[str] = []
        self.scopes: list[Scope] = [Scope()]
        self.reads: list[Read] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.append(node.name)
        for base in node.bases:
            self.visit(base)
        for statement in node.body:
            self.visit(statement)
        self.names.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        self._bind(node.targets, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            return
        self.visit(node.value)
        self._bind([node.target], node.value)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._bind_loop(node.target, node.iter)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node)

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        if isinstance(function, ast.Attribute):
            self._method_call(function, node.args)
        if isinstance(function, ast.Name):
            self._function_call(function.id, node.args)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load):
            self._keyed(node.value, node.slice)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if self._is_membership(node):
            self._record("key", self._resolve(node.left))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name):
            self._attribute(node.value.id, node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        if node.id in self.facts.litellm_names | self.scopes[-1].imports:
            self._record("litellm", (node.id,))
        if node.id in self.scopes[-1].parameters:
            self._record("parameter", (node.id,))

    def _function(self, node: Function) -> None:
        hook: Final = bool(self.names) and node.name.startswith(HOOK_PREFIX)
        self.names.append(node.name)
        self.scopes.append(_scope_for(node, hook))
        self.scopes[-1].imports.update(_imported_names(node.body, LITELLM_PACKAGE))
        for default in node.args.defaults:
            self.visit(default)
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()
        self.names.pop()

    def _comprehension(self, node: Comprehension) -> None:
        for generator in node.generators:
            self.visit(generator.iter)
            self._bind_loop(generator.target, generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
            return
        self.visit(node.elt)

    def _bind(self, targets: Sequence[ast.expr], value: ast.expr) -> None:
        names: Final = [target.id for target in targets if isinstance(target, ast.Name)]
        if self._is_router_access(value):
            self.scopes[-1].routers.update(names)
        elif self._is_litellm_value(value):
            self.scopes[-1].members.update(names)

    def _bind_loop(self, target: ast.expr, iterable: ast.expr) -> None:
        if not isinstance(target, ast.Name):
            return
        values: Final = self._resolve(iterable)
        if values:
            self.scopes[-1].bound[target.id] = values

    def _method_call(
        self, function: ast.Attribute, arguments: Sequence[ast.expr]
    ) -> None:
        owner: Final = function.value
        if isinstance(owner, ast.Name) and owner.id in self.scopes[-1].routers:
            self._record("router", (function.attr,))
        if not arguments:
            return
        if function.attr == MAPPING_READER:
            self._record("key", self._resolve(arguments[0]))
        if function.attr == ENVIRONMENT_READER:
            self._record("env", self._resolve(arguments[0]))
        if function.attr in FLAG_DECLARERS:
            self._record("flag", self._resolve(arguments[0]))

    def _function_call(self, name: str, arguments: Sequence[ast.expr]) -> None:
        if name == ENDPOINT_READER:
            self._record("endpoint", tuple(_endpoints(arguments)))
        if name in self.facts.distribution_names and arguments:
            self._record("distribution", self._resolve(arguments[0]))

    def _keyed(self, owner: ast.expr, key: ast.expr) -> None:
        if ast.unparse(owner) == ENVIRONMENT_MAPPING:
            self._record("env", self._resolve(key))
            return
        self._record("key", self._resolve(key))

    def _attribute(self, owner: str, attribute: str) -> None:
        if owner in self.scopes[-1].members and attribute != MAPPING_READER:
            self._record("member", (attribute,))
        if owner in self.scopes[-1].routers:
            self._record("router", (attribute,))
        if owner in self.facts.litellm_modules:
            self._record("litellm", (attribute,))

    def _is_router_access(self, value: ast.expr) -> bool:
        if not isinstance(value, ast.Call):
            return False
        function: Final = value.func
        if isinstance(function, ast.Name):
            return function.id in ROUTER_ACCESSORS
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            return function.value.id == SELF and function.attr in ROUTER_ACCESSORS
        return False

    def _is_litellm_value(self, value: ast.expr) -> bool:
        scope: Final = self.scopes[-1]
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            return value.value.id in scope.members
        if not isinstance(value, ast.Call):
            return False
        function: Final = value.func
        if isinstance(function, ast.Name):
            return function.id in self.facts.litellm_names
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            return function.value.id in scope.routers
        return False

    def _is_membership(self, node: ast.Compare) -> bool:
        if len(node.ops) != 1 or not isinstance(node.ops[0], ast.In | ast.NotIn):
            return False
        container: Final = node.comparators[0]
        return not (
            isinstance(container, ast.Name) and container.id in self.facts.constants
        )

    def _resolve(self, node: ast.expr) -> tuple[str, ...]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return (node.value,)
        if not isinstance(node, ast.Name):
            return ()
        bound: Final = self.scopes[-1].bound.get(node.id)
        if bound is not None:
            return bound
        return self.facts.constants.get(node.id, ())

    def _record(self, kind: ReadKind, names: tuple[str, ...]) -> None:
        function: Final = ".".join(self.names) or MODULE_SCOPE
        self.reads.extend(Read(self.module, function, kind, name) for name in names)


def _scope_for(node: Function, hook: bool) -> Scope:
    scope: Final = Scope()
    arguments: Final = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    for argument in arguments:
        if argument.arg == SELF:
            continue
        annotation = argument.annotation
        if annotation is not None and ast.unparse(annotation) in LITELLM_VALUE_TYPES:
            scope.members.add(argument.arg)
        if hook:
            scope.parameters.add(argument.arg)
    return scope


def _endpoints(arguments: Sequence[ast.expr]) -> list[str]:
    return [
        argument.value
        for argument in arguments
        if isinstance(argument, ast.Constant)
        and isinstance(argument.value, str)
        and argument.value.startswith(ENDPOINT_PREFIX)
    ]
