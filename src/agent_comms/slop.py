"""Slop citations: the refactor-audit pattern a restated fact matches, where it is
restated, the mechanism that derives it and the declaration that owns it."""

from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import ClassVar

from pydantic import JsonValue

from agent_comms import skills
from agent_comms.change_report import FileLine
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import (
    FieldCodec,
    FieldRepresentation,
    MalformedRecord,
    StructuralRepresentation,
    WireValue,
)
from agent_comms.mro_dispatch import handles


class Derivation(DeclaredFamily, affix="Derivation"):
    sentence: ClassVar[str]

    @classmethod
    def declaration_elsewhere(cls, by: FileLine, where: FileLine, root: Path) -> bool:
        return False


class DeclarationDerivation(Derivation):
    sentence = "the declaration derives it"

    @classmethod
    def declaration_elsewhere(cls, by: FileLine, where: FileLine, root: Path) -> bool:
        return by.declaration(root) != where.declaration(root)


class NominalKeyDerivation(Derivation, declared_name="nominal-key"):
    sentence = "the nominal key identifies it"


class SubclassDerivation(DeclarationDerivation):
    sentence = "the subclass derives it"


class MixinDerivation(DeclarationDerivation):
    sentence = "the mixed-in capability derives it"


class BaseMethodDerivation(DeclarationDerivation, declared_name="base-method"):
    sentence = "the base method derives it"


class InitSubclassDerivation(DeclarationDerivation, declared_name="init-subclass"):
    sentence = "subclass initialization derives it"


class MetaclassDerivation(DeclarationDerivation):
    sentence = "the metaclass derives it"


class DeleteCopyDerivation(Derivation, declared_name="delete-copy"):
    sentence = "deleting the copy leaves its original declaration"


@dataclass(frozen=True)
class OwnerLine(WireValue):
    """The declaration a slop citation names as the owner of the restated fact.

    A ``file.py:line`` string names a line on main; ``{"created": "file.py:line"}``
    names a line the order itself creates."""

    @property
    @abstractmethod
    def line(self) -> FileLine: ...

    def cited(self) -> tuple[FileLine, ...]:
        """The lines that must exist when the order is sent."""
        return (self.line,)

    def owed(self) -> tuple[FileLine, ...]:
        """The lines that must exist on the delivered tree before the order is done."""
        return ()

    def spelled(self) -> str:
        return self.line.to_wire()

    @handles(str)
    @classmethod
    def decode_existing(cls, data: str, structural: object) -> ExistingLine:
        return ExistingLine(FileLine.from_wire(data))

    @handles(dict)
    @classmethod
    def decode_created(cls, data: dict, structural: object) -> CreatedLine:
        return FieldCodec.decode(CreatedLine, data)

    @classmethod
    def from_wire(cls, data: JsonValue) -> OwnerLine:
        raise MalformedRecord("Expected 'file.py:line' or {\"created\": \"file.py:line\"}.")

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        return FieldCodec.alternatives_schema(
            "anyOf", [FileLine.schema(), FieldCodec.record_schema(CreatedLine)])


@dataclass(frozen=True)
class ExistingLine(OwnerLine):
    existing: FileLine

    @property
    def line(self) -> FileLine:
        return self.existing

    def to_wire(self) -> JsonValue:
        return self.existing.to_wire()


@dataclass(frozen=True)
class CreatedLine(OwnerLine):
    created: FileLine

    @property
    def line(self) -> FileLine:
        return self.created

    def cited(self) -> tuple[FileLine, ...]:
        return ()

    def owed(self) -> tuple[FileLine, ...]:
        return (self.created,)

    def spelled(self) -> str:
        return f"{self.created.to_wire()} (this order creates it)"

    @classmethod
    def representation(cls) -> type[FieldRepresentation]:
        return StructuralRepresentation

    def to_wire(self) -> JsonValue:
        return FieldCodec.encode(self)


@dataclass(frozen=True)
class Slop:
    pattern: str
    fact: str
    at: FileLine
    derive: type[Derivation]
    by: OwnerLine

    PATTERN_ENTRY: ClassVar[re.Pattern[str]] = re.compile(r"^## ([A-Z]+-\d+)\s+\S", re.M)

    @staticmethod
    def catalog() -> Path:
        """The pinned refactor-audit pattern catalog that slop citations name."""
        return Path(str(files(skills) / "refactor-audit" / "references" / "patterns"))

    @classmethod
    @cache
    def patterns(cls) -> frozenset[str]:
        return frozenset(
            found[1]
            for source in cls.catalog().glob("*.md")
            for found in cls.PATTERN_ENTRY.finditer(source.read_text(encoding="utf-8"))
        )

    def __post_init__(self) -> None:
        problems = []
        if self.pattern not in self.patterns():
            problems.append(
                f"Unknown refactor-audit pattern ID {self.pattern!r}; "
                "fix: use an ID from the packaged pattern catalog."
            )
        if len(self.fact.split()) < 4:
            problems.append("fact: fix: describe the duplicated fact in at least four words")
        if problems:
            raise ValueError("\n".join(problems))

    def render(self) -> str:
        return (
            f"{self.at.to_wire()} restates {self.pattern}: {self.fact}. "
            f"{self.by.spelled()} owns it; {self.derive.sentence}, so the copy is deleted."
        )
