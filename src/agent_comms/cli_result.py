"""A CLI command's result owns its exit code, reconnected targets and JSON."""

from __future__ import annotations

from .field_codec import FieldCodec
from .tool_result import DeclaredReply


class CliResult(DeclaredReply):
    """Every result a command returns; a result kind overrides what it decides."""

    __slots__ = ()

    def reconnect_targets(self) -> tuple[str, ...]:
        return ()

    def open_targets(self) -> tuple[str, ...]:
        return ()

    def exit_code(self) -> int:
        return 0

    def encoded(self) -> object:
        """The CLI's JSON; a record with its own wire form overrides it."""
        return FieldCodec.encode(self)
