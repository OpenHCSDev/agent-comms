"""Thread tag selection choices: explicit, inherited, or added/removed."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Self

from agent_comms.execution.launch_runtime import TagListText
from agent_comms.nominal.field_codec import (
    DeclaredWireType,
    FieldCodec,
    NullMember,
    RecordMember,
    SubclassUnion,
)


class TagChoice(SubclassUnion, affix="Tags"):
    """Select explicit, inherited or changed tags from an existing set."""

    def for_new(self) -> frozenset[str]:
        return self.for_child(frozenset())

    @abstractmethod
    def for_child(self, parent: frozenset[str]) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class GivenTags(DeclaredWireType, TagChoice):
    """Tags spelled as comma-separated text on the command line, in the editor and in tools."""

    wire_type = str
    tags: frozenset[str]

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls(TagListText.from_text(FieldCodec.decode(cls.wire_type, data)))

    def to_wire(self) -> str:
        return TagListText.encode(self.tags)

    def for_child(self, parent: frozenset[str]) -> frozenset[str]:
        return self.tags


@dataclass(frozen=True, slots=True)
class InheritedTags(NullMember, TagChoice):
    def for_child(self, parent: frozenset[str]) -> frozenset[str]:
        return parent


@dataclass(frozen=True, slots=True)
class ChangedTags(RecordMember, TagChoice):
    add: frozenset[str]
    remove: frozenset[str]

    def for_child(self, parent: frozenset[str]) -> frozenset[str]:
        return (parent | self.add) - self.remove
