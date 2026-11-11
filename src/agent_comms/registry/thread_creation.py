from __future__ import annotations

import math
import time
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import Set

from metaclass_registry.mro_dispatch import handles

from agent_comms.errors import RelationViolationError
from agent_comms.nominal.field_codec import FieldCodec, FieldRepresentation, WireValue


class CreationIdentityCollisionError(RelationViolationError):
    def __init__(self) -> None:
        super().__init__("Registry creation identities collide.")


class CreationTime(float, WireValue, ABC):
    @abstractmethod
    def distinct_from(self, used: Set[float]) -> CreationTime: ...

    def to_wire(self) -> float:
        return float(self)

    @classmethod
    def from_wire(cls, value: object) -> CreationTime:
        return RecordedCreationTime(FieldCodec.decode(float, value))

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(float)


class ThreadCreationSource(ABC):
    """The original creation value used to distinguish an owner-scoped input key."""

    __slots__ = ()
    created_at: CreationTime


class RecordedCreationTime(CreationTime):
    def distinct_from(self, used: Set[float]) -> CreationTime:
        if self in used:
            raise CreationIdentityCollisionError()
        return self


class GeneratedCreationTime(CreationTime):
    def distinct_from(self, used: Set[float]) -> CreationTime:
        candidate = float(self)
        while candidate in used:
            candidate = math.nextafter(candidate, math.inf)
        if not math.isfinite(candidate):
            raise CreationIdentityCollisionError()
        return RecordedCreationTime(candidate)

    @classmethod
    def now(cls) -> GeneratedCreationTime:
        return cls(time.time())


class CreationSeconds(FieldRepresentation):
    @handles(CreationTime)
    @classmethod
    def declared(cls, value: CreationTime, structural) -> CreationTime:
        return value

    @classmethod
    def decode(cls, value: object) -> CreationTime:
        return CreationTime.from_wire(value)

    @classmethod
    def encode(cls, value: CreationTime) -> float:
        return value.to_wire()

    @classmethod
    def schema(cls) -> dict[str, object]:
        return CreationTime.schema()
