"""A displayed reading is current, or explicitly last-known without a recorded owner."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .nominal.field_codec import RecordMember, SubclassUnion


class ReadingAttribution(SubclassUnion, affix="ReadingAttribution"):
    names: tuple[str, ...] = ()

    @abstractmethod
    def joined(self, other: ReadingAttribution) -> ReadingAttribution: ...

    def label_parts(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        return parts

    def status_text(self, text: str) -> str:
        return text


@dataclass(frozen=True)
class CurrentReadingAttribution(RecordMember, ReadingAttribution):
    """No birthless value contributes to this displayed reading."""

    def joined(self, other: ReadingAttribution) -> ReadingAttribution:
        return other


@dataclass(frozen=True)
class LastKnownReadingAttribution(RecordMember, ReadingAttribution):
    """Names are display labels, never identity or authorization evidence."""

    names: tuple[str, ...]

    def joined(self, other: ReadingAttribution) -> ReadingAttribution:
        return type(self)(tuple(dict.fromkeys((*self.names, *other.names))))

    @property
    def notice(self) -> str:
        return f"last known; owner unrecorded ({', '.join(self.names)})"

    def label_parts(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        return (*parts, self.notice)

    def status_text(self, text: str) -> str:
        return f"{text} · {self.notice}"
