"""Source contracts for declarations sent between installed Core runtimes."""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import ClassVar

from metaclass_registry.discovery import discover_registry_classes_recursive

import agent_comms


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

    def drift_source(self, source: str) -> tuple[str, ...]:
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
                return cls("outgoing wire", str(Path("src") / source.relative_to(Path(__file__).parents[2])),
                           line, member.__module__, (member.__name__,), cls.declared(node))
        raise ValueError(f"{source}:{line} does not declare {member.__qualname__}")

    def drift_source(self, source: str) -> tuple[str, ...]:
        """Compare either a checkout file or the installed commit's file, identically."""
        declarations = {
            node.name: self.declared(node)
            for node in ast.parse(source, self.path).body
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

    def __init_subclass__(cls, **options: object) -> None:
        super().__init_subclass__(**options)
        if not inspect.isabstract(cls):
            CrossesRuntimes._members.append(cls)

    @classmethod
    def outgoing_wire(cls) -> tuple[type[CrossesRuntimes], ...]:
        return (cls,)

    @staticmethod
    @cache
    def _discover() -> None:
        discover_registry_classes_recursive(
            agent_comms.__path__, agent_comms.__name__ + ".", CrossesRuntimes,
        )

    @classmethod
    def declarations(cls) -> tuple[WireClass, ...]:
        cls._discover()
        return tuple(WireClass.of(wire) for member in cls._members for wire in member.outgoing_wire())

    @classmethod
    def drift_sources(cls, sources: Mapping[str, str]) -> tuple[str, ...]:
        """A missing file supplies no declarations, so each missing class is reported."""
        return tuple(problem for declaration in cls.declarations()
                     for problem in declaration.drift_source(sources.get(declaration.path, "")))
