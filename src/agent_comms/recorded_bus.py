"""The bus identity recorded by read evidence, using its existing wire shape."""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path

from .field_codec import DeclaredWireType, FieldCodec, MalformedRecord, NullMember, SubclassUnion
from .private_path import AbsentPath, FileIdentity, ObservedPath, PresentPath


class RecordedBusIdentity(SubclassUnion, ObservedPath, affix=""):
    @staticmethod
    def capture(path: Path) -> RecordedBusIdentity:
        try:
            return RecordedBus(FileIdentity.from_stat(path.stat()))
        except FileNotFoundError:
            return NoRecordedBus()

    @abstractmethod
    def matches(self, source: ObservedPath) -> bool: ...


class RecordedBus(DeclaredWireType, PresentPath, RecordedBusIdentity):
    wire_type = list[int]  # device and inode

    def matches(self, source: ObservedPath) -> bool:
        return source.same_file(self.identity)

    @classmethod
    def from_wire(cls, data: object) -> RecordedBus:
        parts = FieldCodec.decode(cls.wire_type, data)
        if len(parts) != 2:
            raise MalformedRecord("A recorded bus requires device and inode.")
        return cls(FileIdentity(*parts))

    def to_wire(self) -> list[int]:
        return [self.identity.device, self.identity.inode]


class NoRecordedBus(NullMember, AbsentPath, RecordedBusIdentity):
    """A page painted while no bus existed stays current until a bus appears."""

    def matches(self, source: ObservedPath) -> bool:
        return source.is_absent()
