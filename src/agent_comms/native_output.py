"""A native output's original persisted location and presentation scope."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from .declared_family import DeclaredFamily
from .field_codec import WireName
from .native_session_reopen import NativeSessionIdentity
from .native_tool_call import ToolCallId


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
class NativeOutputLocation(NativeSessionIdentity):
    session_entry_id: Annotated[str, WireName("sessionEntryId")]
    session_entry_start: Annotated[int, WireName("sessionEntryStart")]
    session_entry_end: Annotated[int, WireName("sessionEntryEnd")]
    output: NativeOutputScope

    def __post_init__(self):
        super().__post_init__()
        if not self.session_entry_id or not 0 < self.session_entry_start < self.session_entry_end:
            raise ValueError("Native output requires a complete original record")
