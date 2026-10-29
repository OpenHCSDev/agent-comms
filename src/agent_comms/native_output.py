"""A native output's original persisted location and presentation scope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from .coordination_errors import TranscriptChanged
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    ToolCallId,
    WireName,
)
from .native_session_reopen import NativeSessionIdentity
from .turn_item import NativeItemId

if TYPE_CHECKING:
    from .native_entries import NativeEntry


@dataclass(frozen=True)
class NativeOutputScope(DeclaredFamily, affix="Output"):
    """The presentation completed by one original persisted message."""


@dataclass(frozen=True)
class AssistantOutput(NativeOutputScope):
    """The current assistant text and thought, not its unfinished tool calls."""


@dataclass(frozen=True)
class ToolOutput(NativeOutputScope):
    tool_call_id: Annotated[ToolCallId, WireName("toolCallId")]


@dataclass(frozen=True)
class NativeRecordLocation(NativeSessionIdentity):
    session_entry_id: Annotated[NativeItemId, WireName("sessionEntryId")]
    session_entry_start: Annotated[int, WireName("sessionEntryStart")]
    session_entry_end: Annotated[int, WireName("sessionEntryEnd")]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.session_entry_id or not 0 < self.session_entry_start < self.session_entry_end:
            raise ValueError("Native commit requires a complete original record")

    def require_entry(self, entry: NativeEntry) -> None:
        if entry.id != self.session_entry_id:
            raise TranscriptChanged()


@dataclass(frozen=True)
class NativeOutputLocation(NativeRecordLocation):
    output: NativeOutputScope

    def require_entry(self, entry: NativeEntry) -> None:
        super().require_entry(entry)
        if self.output not in tuple(
            scope for message in entry.messages for scope in message.committed_outputs()
        ):
            raise TranscriptChanged()
