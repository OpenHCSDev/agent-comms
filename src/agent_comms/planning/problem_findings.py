from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Annotated,
    ClassVar,
)

from agent_comms.errors import TodoError
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import Description, NullMember, SingleFieldValue, SubclassUnion
from agent_comms.planning.goals import OrderCheck
from agent_comms.projects.change_report import Declaration, FileLine, SourceRevision, SourceRoots


class OrderDeletion(DeclaredFamily, affix="Deletion"):
    """The deletion an order declares, decoded as its own fields."""

    label: ClassVar[str] = "Deletes:"
    checks: ClassVar[tuple[OrderCheck, ...]] = ()

    @property
    def modules(self) -> tuple[str, ...]:
        return ()

    def instructions(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class NoOrderDeletion(OrderDeletion):
    pass


@dataclass(frozen=True)
class ModuleDeletion(OrderDeletion):
    modules: tuple[str, ...] = ()

    def instructions(self) -> tuple[str, ...]:
        return (f"{self.label} {', '.join(self.modules)}",)


@dataclass(frozen=True)
class FamilyDeletion(OrderDeletion):
    """Completion counts the whole family on the delivered commit, so a site renamed or wrapped
    instead of deleted still counts, and only zero passes."""

    family: str
    site: FileLine
    count: OrderCheck

    @property
    def checks(self) -> tuple[OrderCheck, ...]:
        return (self.count,)

    def instructions(self) -> tuple[str, ...]:
        return (f"{self.label} {self.family} at {self.site.to_wire()} :: {self.count.directory} :: "
                f"{self.count.command}",)


@dataclass(frozen=True)
class ProblemFinding(DeclaredFamily, affix="ProblemFinding"):
    def source_locations(self) -> tuple[FileLine, ...]:
        return ()

    def require_references(self, roots: SourceRoots, revision: SourceRevision) -> None:
        """An unlocated finding has no source reference to validate."""

    def in_declarations(self, declarations: tuple[Declaration, ...], root: Path) -> bool:
        return False

    def instructions(self) -> tuple[str, ...]:
        return ()

    def place(self) -> str:
        return "-"


@dataclass(frozen=True)
class UnlocatedProblemFinding(ProblemFinding):
    pass


@dataclass(frozen=True)
class LocatedProblemFinding(ProblemFinding):
    where: FileLine

    def source_locations(self) -> tuple[FileLine, ...]:
        return (self.where,)

    def require_references(self, roots: SourceRoots, revision: SourceRevision) -> None:
        self.where.require_exists(roots, revision)

    def in_declarations(self, declarations: tuple[Declaration, ...], root: Path) -> bool:
        return any(declaration.holds(self.where, root) for declaration in declarations)

    def instructions(self) -> tuple[str, ...]:
        return (f"Where: {self.place()}",)

    def place(self) -> str:
        return self.where.to_wire()


@dataclass(frozen=True, kw_only=True)
class Successor(SubclassUnion, affix="Successor"):
    """The problem that already owns a withdrawn order's fix, or none."""

    @abstractmethod
    def withdrawal_note(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SupersedingProblem(SingleFieldValue, Successor):
    problem: str

    def withdrawal_note(self) -> str:
        return f" (superseded by {self.problem})"


@dataclass(frozen=True, slots=True)
class NoSuccessor(NullMember, Successor):
    def withdrawal_note(self) -> str:
        return ""


@dataclass(frozen=True)
class FixShape:
    """The parent declarations owning a fix and the related member declarations."""

    parents: tuple[FileLine, ...]
    members: tuple[FileLine, ...]

    def __post_init__(self) -> None:
        if not self.parents:
            raise TodoError("scope.shape.parents must name at least one parent class declaration")

    def require_references(self, roots: SourceRoots, revision: SourceRevision) -> None:
        for location in (*self.parents, *self.members):
            location.require_class(roots, revision)


@dataclass(frozen=True)
class ExpectedProblem:
    """An open problem an order assigns, at the Todo revision its sender read."""
    problem: Annotated[str, Description("Open problem ID")]
    expected_revision: Annotated[int, Description("Its Todo revision the sender read")]
