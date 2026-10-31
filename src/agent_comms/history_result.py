"""History result declarations own their wire keys and target context."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .cli_result import CliResult
from .field_codec import FieldCodec
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .page_traversal import PageCursor


class HistoryEntry(ABC):
    """What a history page reads from each entry it reports."""

    __slots__ = ()

    @property
    @abstractmethod
    def view_cursor(self) -> PageCursor: ...

    @property
    @abstractmethod
    def display_metadata(self) -> dict: ...

    @abstractmethod
    def to_wire(self) -> dict: ...


@dataclass(frozen=True)
class HistoryPageResult(CliResult, ToolResult):
    messages_key: ClassVar[str] = "messages"
    messages: tuple[HistoryEntry, ...]
    has_older: bool

    @classmethod
    def for_target(cls, name: str, messages: tuple[HistoryEntry, ...], has_older: bool) -> Self:
        return cls(messages, has_older)

    @property
    def before(self) -> PageCursor | None:
        return self.messages[-1].view_cursor if self.messages else None

    def to_wire(self) -> dict[str, object]:
        return {self.messages_key: [{**m.to_wire(), **m.display_metadata} for m in self.messages],
                "before": FieldCodec.encode(self.before), "has_older": self.has_older}


class DmHistoryPageResult(HistoryPageResult):
    messages_key = "dm"


class EverythingHistoryPageResult(HistoryPageResult):
    messages_key = "everything"


@dataclass(frozen=True)
class ChannelHistoryPageResult(HistoryPageResult):
    channel: str

    @classmethod
    def for_target(cls, name: str, messages: tuple[HistoryEntry, ...], has_older: bool) -> Self:
        return cls(messages, has_older, name)

    def to_wire(self) -> dict[str, object]:
        return {**super().to_wire(), "channel": self.channel}
