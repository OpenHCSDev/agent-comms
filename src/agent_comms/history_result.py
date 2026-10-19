"""History result declarations own their wire keys and target context."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from pydantic import JsonValue

from .cli_result import CliResult
from .field_codec import FieldCodec
from .messages import Message

if TYPE_CHECKING:
    from .page_traversal import PageCursor


@dataclass(frozen=True)
class HistoryPageResult(CliResult):
    messages_key: ClassVar[str] = "messages"
    messages: tuple[Message, ...]
    has_older: bool

    @classmethod
    def for_target(cls, name: str, messages: tuple[Message, ...], has_older: bool) -> Self:
        return cls(messages, has_older)

    @classmethod
    def decode(cls, wire: Mapping[str, JsonValue]) -> Self:
        """The page ``encoded`` wrote, read back under this member's own keys."""
        return cls(cls.decoded_messages(wire), FieldCodec.decode(bool, wire["has_older"]))

    @classmethod
    def decoded_messages(cls, wire: Mapping[str, JsonValue]) -> tuple[Message, ...]:
        rows = FieldCodec.decode(tuple[dict[str, JsonValue], ...], wire[cls.messages_key])
        return tuple(map(Message.from_wire, rows))

    @property
    def before(self) -> PageCursor | None:
        return self.messages[-1].view_cursor if self.messages else None

    def encoded(self) -> dict[str, object]:
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

    def encoded(self) -> dict[str, object]:
        return {**super().encoded(), "channel": self.channel}

    @classmethod
    def decode(cls, wire: Mapping[str, JsonValue]) -> Self:
        return cls(cls.decoded_messages(wire), FieldCodec.decode(bool, wire["has_older"]),
                   FieldCodec.decode(str, wire["channel"]))
