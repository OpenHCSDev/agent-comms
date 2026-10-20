"""History result declarations own their wire keys and target context."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Self

import agent_comms

from .cli_result import CliResult
from .field_codec import RecordMember, SubclassUnion, WireForm
from .message_provenance import HistoryProvenance, NoHistoryProvenance
from .page_traversal import NoPageCursor, PageCursor
from .tool_result import ToolResult

if TYPE_CHECKING:
    import agent_comms.messages


@dataclass(frozen=True)
class HistoryEntry(RecordMember):
    message: Annotated[agent_comms.messages.Message, WireForm] = field(
        metadata={"wire_inline": True})
    history: HistoryProvenance = field(default_factory=NoHistoryProvenance,
        metadata={"wire_name": HistoryProvenance.envelope_key, "wire_omit_default": True})

    @classmethod
    def capture(cls, message: agent_comms.messages.Message) -> Self:
        return cls(message, message.history_provenance)


@dataclass(frozen=True)
class HistoryPageResult(SubclassUnion, CliResult, ToolResult, affix="HistoryPageResult"):
    messages: tuple[HistoryEntry, ...]
    has_older: bool
    before: PageCursor = field(kw_only=True)

    @classmethod
    def target_fields(cls, name: str) -> dict[str, str]:
        return {}

    @classmethod
    def for_target(
        cls, name: str, messages: tuple[agent_comms.messages.Message, ...], has_older: bool,
    ) -> Self:
        return cls(
            tuple(HistoryEntry.capture(message) for message in messages), has_older,
            before=messages[-1].view_cursor if messages else NoPageCursor(),
            **cls.target_fields(name),
        )


@dataclass(frozen=True)
class DmHistoryPageResult(RecordMember, HistoryPageResult):
    messages: tuple[HistoryEntry, ...] = field(metadata={"wire_name": "dm"})


@dataclass(frozen=True)
class EverythingHistoryPageResult(RecordMember, HistoryPageResult):
    messages: tuple[HistoryEntry, ...] = field(metadata={"wire_name": "everything"})


@dataclass(frozen=True)
class ChannelHistoryPageResult(RecordMember, HistoryPageResult):
    channel: str

    @classmethod
    def target_fields(cls, name: str) -> dict[str, str]:
        return {"channel": name}
