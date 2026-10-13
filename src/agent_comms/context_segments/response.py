"""Turns recorded before the batch reply change carry this segment kind."""
from dataclasses import dataclass

from ..turn_context import InstructionFile, ReplyRouteSegment


@dataclass(frozen=True, kw_only=True)
class SelectedResponseSegment(ReplyRouteSegment):
    """Recorded kind only: nothing produces it now, saved turns still name it."""

    sender: str
    example: InstructionFile
