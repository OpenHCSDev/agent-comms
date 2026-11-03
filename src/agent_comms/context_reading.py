"""Native context observations own their presentation without nullable readings."""

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Annotated

from .nominal.declared_family import DeclaredFamily
from .nominal.field_codec import WireOmitDefault
from .reading_attribution import CurrentReadingAttribution, ReadingAttribution


@dataclass(frozen=True, kw_only=True)
class ContextReading(DeclaredFamily):
    attribution: Annotated[ReadingAttribution, WireOmitDefault()] = field(
        default_factory=CurrentReadingAttribution
    )

    def label_parts(self) -> tuple[str, ...]:
        return self.attribution.label_parts(self.reading_parts())

    def status_text(self) -> str:
        return self.attribution.status_text(self.reading_status())

    @abstractmethod
    def reading_parts(self) -> tuple[str, ...]: ...

    @abstractmethod
    def reading_status(self) -> str: ...


@dataclass(frozen=True)
class ContextUsage(ContextReading):
    used: int
    size: int

    @property
    def context_percent(self) -> float:
        return self.used / self.size * 100

    @property
    def label(self) -> str:
        return f"{self.used:,}/{self.size:,} tokens ({self.context_percent:.1f}%)"

    def reading_parts(self) -> tuple[str, ...]:
        return (self.label,)

    def reading_status(self) -> str:
        return self.label


class UnsizedContext(ContextReading):
    """No share of the context window is known, so no usage update can carry it."""


@dataclass(frozen=True)
class UnmeasuredContext(UnsizedContext):
    def reading_parts(self) -> tuple[str, ...]:
        return ()

    def reading_status(self) -> str:
        return "Native owner has not reported context usage"


@dataclass(frozen=True)
class UnsizedContextUsage(UnsizedContext):
    """Usage a runtime reports without its context window, so no share of it is known."""

    used: int

    def reading_parts(self) -> tuple[str, ...]:
        return (f"{self.used:,} tokens",)

    def reading_status(self) -> str:
        return f"{self.used:,} tokens"
