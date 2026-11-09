from __future__ import annotations

from collections.abc import Callable
from dataclasses import (
    dataclass,
    fields,
)
from typing import (
    TypedDict,
    Unpack,
    cast,
)

from metaclass_registry.core import REPLACES_REGISTERED_ATTRIBUTE
from metaclass_registry.mro_dispatch import (
    MroDispatch,
    handles,
)

from agent_comms.nominal.declared_family import DeclaredFamily, FamilyMeta


class FamilyOptions(TypedDict, total=False):
    """The class-header options FamilyMeta reads, each present only when a header gives it."""

    affix: str
    discovery_package: str
    declared_name: str


class ServesDeclaration(FamilyMeta):
    """A served class built on a wire declaration takes that declaration's name and its slot in
    the family, so a wire name decodes into the served class wherever the served module is
    imported, and into the declaration everywhere else. Nothing is restated per member."""

    def __new__(
        mcs: type[ServesDeclaration], name: str, bases: tuple[type, ...],
        namespace: dict[str, object], **options: Unpack[FamilyOptions],
    ) -> ServesDeclaration:
        declarations = DeclarationBases.of(bases)
        if len(declarations) > 1:
            raise TypeError(f"{name} serves two declarations: {declarations!r}")
        for declaration in declarations:
            # FamilyMeta reads a member's name from its namespace when the header gives none.
            namespace.setdefault(REPLACES_REGISTERED_ATTRIBUTE, declaration)
            namespace.setdefault("declared_name", declaration.selection_key())
            namespace.setdefault("__eq__", DeclaredValue(declaration).equal)
            namespace.setdefault("__hash__", declaration.__hash__)
        return cast(ServesDeclaration, super().__new__(mcs, name, bases, namespace, **options))


@dataclass(frozen=True)
class DeclaredValue:
    """A served request is the value its declaration carries: once a process has served a
    request, the requests it decodes are served members while the ones it builds are
    declarations, and a reply must still find the request that asked for it."""

    declaration: type

    @property
    def equal(self) -> Callable[[object, object], bool]:
        declaration = self.declaration
        compared = tuple(field.name for field in fields(declaration) if field.compare)

        def equal(served: object, other: object) -> bool:
            return isinstance(other, declaration) and all(
                bool(getattr(served, name) == getattr(other, name)) for name in compared
            )

        return equal


class DeclarationBases(MroDispatch):
    """Which bases are wire declarations a served class takes over, by the base's metaclass."""

    @handles(ServesDeclaration)
    @staticmethod
    def served(base: type[DeclaredFamily]) -> tuple[type[DeclaredFamily], ...]:
        return ()

    @handles(FamilyMeta)
    @staticmethod
    def declared(base: type[DeclaredFamily]) -> tuple[type[DeclaredFamily], ...]:
        """A family member registered under its own name; an intermediate has no name."""
        return (base,) if base in dict.values(base.__registry__) else ()

    @handles(type)
    @staticmethod
    def plain(base: type) -> tuple[type[DeclaredFamily], ...]:
        return ()

    @classmethod
    def of(cls, bases: tuple[type, ...]) -> tuple[type[DeclaredFamily], ...]:
        return tuple(found for base in bases for found in cls.handler_for(type(base))(base))
