"""Source contracts for declarations sent between installed Core runtimes."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.declared_family import DeclaredFamily


@dataclass(frozen=True)
class ClientImport:
    """One source reference whose declaration another Core tree must supply."""

    client: str
    path: str
    line: int
    module: str
    names: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.client}:{self.path}:{self.line} imports {self.module}"

    def drift(self, root: Path) -> tuple[str, ...]:
        """Ordinary imports require names, not a wire field contract."""
        return ()


@dataclass(frozen=True)
class WireClass(ClientImport):
    """A wire declaration's ordered annotated fields, compared without importing old code."""

    fields: tuple[str, ...] = ()

    @staticmethod
    def declared(node: ast.ClassDef) -> tuple[str, ...]:
        return tuple(
            f"{ast.unparse(statement.target)}: {ast.unparse(statement.annotation)}"
            for statement in node.body
            if isinstance(statement, ast.AnnAssign)
        )

    @classmethod
    def of(cls, member: type[CrossesRuntimes]) -> WireClass:
        source = Path(inspect.getfile(member))
        lines, line = inspect.getsourcelines(member)
        node, = ast.parse(textwrap.dedent("".join(lines))).body
        match node:
            case ast.ClassDef():
                return cls("outgoing wire", str(Path("src", *member.__module__.split(".")).with_suffix(".py")),
                           line, member.__module__, (member.__name__,), cls.declared(node))
        raise ValueError(f"{source}:{line} does not declare {member.__qualname__}")

    def drift(self, root: Path) -> tuple[str, ...]:
        path = root / self.path
        if not path.is_file():
            return (f"{self.module}.{self.names[0]} is missing; sent fields ({', '.join(self.fields)})",)
        declarations = {
            node.name: self.declared(node)
            for node in ast.parse(path.read_text(), str(path)).body
            if isinstance(node, ast.ClassDef)
        }
        name, = self.names
        if name not in declarations:
            return (f"{name} is missing; sent fields ({', '.join(self.fields)})",)
        fields = declarations[name]
        if fields != self.fields:
            return (f"{name} declares ({', '.join(fields)}), sent as ({', '.join(self.fields)})",)
        return ()


class CrossesRuntimes:
    """A declaration sent to an outgoing runtime registers its own source contract."""

    _members: ClassVar[list[type[CrossesRuntimes]]] = []

    def __init_subclass__(cls, **options) -> None:
        super().__init_subclass__(**options)
        if not inspect.isabstract(cls):
            CrossesRuntimes._members.append(cls)

    @classmethod
    def outgoing_wire(cls) -> tuple[type[CrossesRuntimes], ...]:
        return (cls,)

    @classmethod
    def declarations(cls) -> tuple[WireClass, ...]:
        # Acquire declarations, never a second list of their names or capabilities.
        from agent_comms.cli_bridges import monitor
        from agent_comms.installer import probes

        DeclaredFamily.discover_package(probes)
        return tuple(WireClass.of(wire) for member in cls._members for wire in member.outgoing_wire())

    @classmethod
    def drift(cls, root: Path) -> tuple[str, ...]:
        return tuple(problem for declaration in cls.declarations() for problem in declaration.drift(root))
