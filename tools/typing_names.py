"""Which typing declaration a name or attribute in a module stands for, read from the
module's imports and the stdlib's own typing modules, never by running the module."""

from __future__ import annotations

import ast
import types
import typing

import typing_extensions

from agent_comms.mro_dispatch import MroDispatch, handles


class TypingImports(MroDispatch):
    """Resolve unshadowed module imports from the actual stdlib declarations.

    This bounded syntactic measure never executes inspected source. Nested
    bindings conservatively disqualify a name rather than inferring its flow.
    """

    typing_modules: typing.ClassVar[dict[str, types.ModuleType]] = {
        module.__name__: module for module in (typing, typing_extensions)
    }

    def __init__(self, declarations: set[ast.AST]) -> None:
        self.declarations = declarations
        self.values: dict[str, object] = {}
        self.shadowed: set[str] = set()

    def bind(self, node: ast.AST, name: str, value: object) -> None:
        if node not in self.declarations or name in self.values:
            self.shadowed.add(name)
        self.values[name] = value

    @handles(ast.Import)
    def modules(self, node: ast.Import) -> None:
        for item in node.names:
            self.bind(
                node, item.asname or item.name.split(".")[0], self.typing_modules.get(item.name)
            )

    @handles(ast.ImportFrom)
    def members(self, node: ast.ImportFrom) -> None:
        match node:
            case ast.ImportFrom(level=0, module=str(name)) if name in self.typing_modules:
                namespace = vars(self.typing_modules[name])
            case _:
                namespace = {}
        for item in node.names:
            self.bind(node, item.asname or item.name, namespace.get(item.name))

    @handles(ast.Name)
    def assignment(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.shadowed.add(node.id)

    @handles(ast.Attribute)
    def mutation(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            for descendant in ast.walk(node.value):
                if isinstance(descendant, ast.Name):
                    self.shadowed.add(descendant.id)

    @handles(ast.arg)
    def parameter(self, node: ast.arg) -> None:
        self.shadowed.add(node.arg)

    @handles(ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    def named_binding(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.shadowed.add(node.name)

    @handles(ast.ExceptHandler)
    def exception_binding(self, node: ast.ExceptHandler) -> None:
        match node:
            case ast.ExceptHandler(name=str(name)):
                self.shadowed.add(name)

    @handles(ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)
    def type_parameter(self, node: ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple) -> None:
        self.shadowed.add(node.name)

    @classmethod
    def of(cls, tree: ast.Module) -> dict[str, object]:
        imports = cls(set(tree.body))
        for node in ast.walk(tree):
            imports.dispatch_sync(node)
        if "*" in imports.values:
            return {}  # A star import can replace any otherwise known binding.
        return {
            name: value for name, value in imports.values.items() if name not in imports.shadowed
        }

    @classmethod
    def at_any_depth(cls, tree: ast.Module) -> dict[str, object]:
        """Every name an import binds to ``typing``, wherever the import is written.

        For a measure that counts uses of a typing name: skipping an import inside
        ``if TYPE_CHECKING:`` or a function body would hide the uses it enables.
        """
        imports = cls(set(ast.walk(tree)))
        for node in ast.walk(tree):
            imports.dispatch_sync(node)
        return {name: value for name, value in imports.values.items() if value is not None}


class TypingReference(MroDispatch):
    """One AST expression's resolved imported declaration, never evaluated code."""

    def __init__(self, imports: dict[str, object]) -> None:
        self.imports = imports
        self.value: object = None

    @property
    def is_any(self) -> bool:
        return self.value is typing.Any

    @property
    def is_type_checking(self) -> bool:
        """``typing.TYPE_CHECKING`` is the one typing declaration whose value is False."""
        return self.value is typing.TYPE_CHECKING

    @handles(ast.Name)
    def name(self, node: ast.Name) -> None:
        self.value = self.imports.get(node.id)

    @handles(ast.Attribute)
    def member(self, node: ast.Attribute) -> None:
        parent = TypingReference(self.imports)
        parent.dispatch_sync(node.value)
        for module in TypingImports.typing_modules.values():
            if parent.value is module:
                self.value = vars(module).get(node.attr)

    def raw_access_count(self) -> int:
        """Typing declarations contribute no raw-record operation; unknowns do."""
        return int(getattr(self.value, "__module__", None) != typing.__name__)
