"""Pi's reported counts and their wire domains, decoded once by FieldCodec."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, ClassVar

from .context_reading import ContextReading, ContextUsage, UnmeasuredContext
from .field_codec import FieldCodec, FieldRepresentation, WireValue

if TYPE_CHECKING:
    from .agent_events import AgentEvent
    from .backend import TurnSession
    from .turn_usage import UsageAccount


class PiCountWire(FieldRepresentation):
    """Positive native counts: zero and omission mean not reported."""

    accepts_null = True
    minimum: ClassVar[int] = 1

    @classmethod
    def decode(cls, value: object) -> PiCount:
        if value is None:
            return UnreportedCount()
        count = FieldCodec.decode(int, value)
        return ReportedCount(count) if count >= cls.minimum else UnreportedCount()

    @classmethod
    def encode(cls, value: object) -> object:
        return FieldCodec.encode(value)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(int | None)


class NonnegativeCountWire(PiCountWire):
    """Message and component counts may explicitly report zero."""

    minimum = 0


class PiCount(WireValue):
    """Whether Pi reported a count; wire omission is decided once at the field boundary."""

    accepts_null = True
    external_json_shape = True

    @classmethod
    def from_wire(cls, value: object) -> PiCount:
        return PiCountWire.decode(value)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return PiCountWire.schema()

    def matches(self, count: int) -> bool:
        return False

    def require_count(self) -> int:
        raise ValueError("Pi has not reported this count")

    def observe_size(self, account: UsageAccount) -> None:
        account.size = self

    def update_size(self, account: UsageAccount) -> None:
        pass

    def reading(self, used: PiCount) -> ContextReading:
        return UnmeasuredContext()

    def with_size(self, size: int) -> ContextReading:
        return UnmeasuredContext()

    def otherwise(self, other: PiCount) -> PiCount:
        return other

    def provisional(self, session: TurnSession) -> Iterator[AgentEvent]:
        return iter(())

    def confirm(self, account: UsageAccount) -> None:
        """Only a reported total confirms the context usage."""

    def settle(self, session: TurnSession) -> Iterator[AgentEvent]:
        if session.usage.provisional:
            session.usage.rollback()
            yield session.context_info()


@dataclass(frozen=True)
class UnreportedCount(PiCount):
    def to_wire(self) -> None:
        return None


@dataclass(frozen=True)
class ReportedCount(PiCount):
    count: int

    def to_wire(self) -> int:
        return self.count

    def matches(self, count: int) -> bool:
        return self.count == count

    def require_count(self) -> int:
        return self.count

    def update_size(self, account: UsageAccount) -> None:
        account.size = self

    def reading(self, used: PiCount) -> ContextReading:
        return used.with_size(self.count)

    def with_size(self, size: int) -> ContextReading:
        return ContextUsage(self.count, size)

    def otherwise(self, other: PiCount) -> PiCount:
        return self

    def confirm(self, account: UsageAccount) -> None:
        account.confirm(self)

    def provisional(self, session: TurnSession) -> Iterator[AgentEvent]:
        session.usage.observe(self)
        yield session.context_info()

    def settle(self, session: TurnSession) -> Iterator[AgentEvent]:
        if not session.accepts_output:
            yield from super().settle(session)
            return
        session.usage.confirm(self)
        yield session.context_info()


NonnegativePiCount = Annotated[PiCount, NonnegativeCountWire]
