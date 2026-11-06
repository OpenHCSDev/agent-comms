"""The teammate's own board rows: each new or changed row naming the session, once.

The board is the newest heartbeat packet's lines that no recorded action has disposed of yet.
Only Core can answer that; the session's resident, which already answers its hooks with Core
open, reads them through ReaderBoardDelivery and prints exactly the rows it stamps delivered.
This module holds the row and its printed line, with the standard library only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar


class SessionWire:
    """The session socket's records: one JSON line each, its family member named by the key."""

    family_discriminator: ClassVar[str] = "type"


@dataclass(frozen=True)
class BoardEntry:
    """A current Core observation, never a separately stored board."""

    content: str
    text: str
    since: float
    alarm_at: float

    # One short line rides a tool call; the whole row stays in comms_hud and comms_board.
    shown_chars: ClassVar[int] = 160

    def minutes(self, now: float) -> int:
        return int((now - self.since) // 60)

    def line(self, now: float) -> str:
        cut = self.text[:self.shown_chars]
        shown = self.text if cut == self.text else f"{cut}…"
        return f"BOARD: {shown} ({self.minutes(now)} min)"


@dataclass(frozen=True)
class SessionBoard:
    """The board lines one tool call prints."""

    lines: tuple[str, ...]

    @classmethod
    def presented(cls, due: Sequence[BoardEntry], now: float) -> SessionBoard:
        """Each new or changed row once; an unchanged row says nothing again."""
        return cls(tuple(entry.line(now) for entry in due))
