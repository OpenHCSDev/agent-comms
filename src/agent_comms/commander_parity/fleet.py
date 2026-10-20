"""Live board and work comparisons; neither side is a recorded answer."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from ..commander.judge import Edit
from ..commander.order import Commander
from ..field_codec import FieldCodec
from ..tool_caller import ThreadCaller
from ..tools import CommsUnjudgedTool
from . import ParityComparison


@dataclass(frozen=True)
class ReadComparison(ParityComparison):
    """Compare complete wire facts, not a count that can hide different records."""

    owner: str
    current: JsonValue
    native: JsonValue

    @property
    def passed(self) -> bool:
        return self.current == self.native

    def describe(self) -> str:
        return f"owner={self.owner}; complete_current_native_equal={self.passed}"


class UnjudgedComparison(ReadComparison):
    @classmethod
    def read(cls) -> ParityComparison:
        context = Commander.read(Commander.path())
        current = Edit.unjudged(context.comms, context.commander)
        native = CommsUnjudgedTool(thread=context.commander).apply(
            context.comms, ThreadCaller(context.commander)
        )
        return cls(
            "CommsUnjudgedTool / judge.Edit.unjudged",
            FieldCodec.encode(current),
            FieldCodec.encode(native.edits),
        )
