"""Native context observations own their presentation without nullable readings."""

from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily


class ContextReading(DeclaredFamily):
    @abstractmethod
    def label_parts(self) -> tuple[str, ...]: ...

    @abstractmethod
    def status_text(self) -> str: ...


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

    def label_parts(self) -> tuple[str, ...]:
        return (self.label,)

    def status_text(self) -> str:
        return self.label


@dataclass(frozen=True)
class UnmeasuredContext(ContextReading):
    def label_parts(self) -> tuple[str, ...]:
        return ()

    def status_text(self) -> str:
        return "Native owner has not reported context usage"
