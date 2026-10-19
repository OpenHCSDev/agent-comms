"""History result declarations own their wire keys and target context."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from .cli_result import CliResult
from .field_codec import FieldCodec

if TYPE_CHECKING:
    from .messages import Message
    from .page_traversal import PageCursor


@dataclass(frozen=True)
class HistoryPageResult(CliResult):
    messages_key: ClassVar[str] = "messages"
    messages: tuple[Message, ...]
    has_older: bool

    @classmethod
    def for_target(cls, name: str, messages: tuple[Message, ...], has_older: bool) -> Self:
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
    def for_target(cls, name: str, messages: tuple[Message, ...], has_older: bool) -> Self:
        return cls(messages, has_older, name)

    def to_wire(self) -> dict[str, object]:
        return {**super().to_wire(), "channel": self.channel}
