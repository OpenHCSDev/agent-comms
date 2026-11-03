"""Independent runtime fields retain the evidence that actually supplied each value."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .nominal.field_codec import FieldCodec, NullMember, RecordMember, SubclassUnion
from .reading_attribution import (
    CurrentReadingAttribution,
    LastKnownReadingAttribution,
    ReadingAttribution,
)
from .thread_identity import RecordedThreadIncarnation

if TYPE_CHECKING:
    from .runtime_info import RuntimeInfo


class RuntimeOrigin(SubclassUnion, affix="RuntimeOrigin"):
    """A field publication or an old stored row; their clocks attest different facts."""

    timestamp: float

    def capture[Value](self, value: Value) -> RuntimeField[Value]:
        return CapturedRuntimeField(value, self)

    @abstractmethod
    def attribution(self) -> ReadingAttribution: ...

    def current_value[Value](self, owner: RecordedThreadIncarnation, value: Value, default: Value) -> Value:
        return default


@dataclass(frozen=True)
class RuntimePublication(RecordMember, RuntimeOrigin):
    """This recorded birth actually published these fields at this clock."""

    owner: RecordedThreadIncarnation
    timestamp: float

    def __post_init__(self) -> None:
        self.owner.require_recorded()
        FieldCodec.decode(float, self.timestamp)

    def attribution(self) -> ReadingAttribution:
        return CurrentReadingAttribution()

    def current_value[Value](self, owner: RecordedThreadIncarnation, value: Value, default: Value) -> Value:
        return value if self.owner.lookup == owner.lookup else default


@dataclass(frozen=True)
class UnattributedRuntimeOrigin(RecordMember, RuntimeOrigin):
    """Original row spellings and update clock, never per-field publication or birth evidence."""

    root_name: str
    thread_name: str
    timestamp: float

    def __post_init__(self) -> None:
        FieldCodec.decode(float, self.timestamp)

    def attribution(self) -> ReadingAttribution:
        return LastKnownReadingAttribution(tuple(dict.fromkeys((self.root_name, self.thread_name))))


class RuntimeField[Value](SubclassUnion, affix="RuntimeField"):
    @abstractmethod
    def value_or(self, default: Value) -> Value: ...

    @abstractmethod
    def current_value(self, owner: RecordedThreadIncarnation, default: Value) -> Value: ...

    @abstractmethod
    def over(self, older: RuntimeField[Value]) -> RuntimeField[Value]: ...

    def attribution(self) -> ReadingAttribution:
        return CurrentReadingAttribution()

    def recorded_at(self) -> float:
        return 0.0

    def snapshot_or(self, current: RuntimeInfo, otherwise: RuntimeInfo) -> RuntimeInfo:
        return otherwise


@dataclass(frozen=True)
class UnobservedRuntimeField[Value](NullMember, RuntimeField[Value]):
    """No publication supplied this field; distinct from a published unknown value."""

    def value_or(self, default: Value) -> Value:
        return default

    def current_value(self, owner: RecordedThreadIncarnation, default: Value) -> Value:
        return default

    def over(self, older: RuntimeField[Value]) -> RuntimeField[Value]:
        return older


@dataclass(frozen=True)
class CapturedRuntimeField[Value](RecordMember, RuntimeField[Value]):
    value: Value
    origin: RuntimeOrigin

    def value_or(self, default: Value) -> Value:
        return self.value

    def current_value(self, owner: RecordedThreadIncarnation, default: Value) -> Value:
        return self.origin.current_value(owner, self.value, default)

    def over(self, older: RuntimeField[Value]) -> RuntimeField[Value]:
        return self

    def attribution(self) -> ReadingAttribution:
        return self.origin.attribution()

    def recorded_at(self) -> float:
        return self.origin.timestamp

    def snapshot_or(self, current: RuntimeInfo, otherwise: RuntimeInfo) -> RuntimeInfo:
        return current
