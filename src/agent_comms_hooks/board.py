"""The commander's current board: changes once, then one unchanged count per tool call.

The board is the newest heartbeat packet's lines that no recorded action has disposed of yet.
Only Core can answer that, and importing Core costs the hook over a second, so the hook asks
the session's bridge, which holds Core open, over the socket it already serves. The bridge
derives the lines when asked, so a verdict drops its line on the next tool call. This module
is the hook's half of that exchange, with the standard library only.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


class UnexpectedBoardReply(ValueError):
    """The session bridge refused or could not answer the board read."""


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

    def minutes(self, now: float) -> int:
        return int((now - self.since) // 60)

    def aged(self, now: float) -> str:
        return f"{self.text} (seen for {self.minutes(now)} min)"

    def line(self, now: float) -> str:
        return f"BOARD: {self.aged(now)}"


@dataclass(frozen=True)
class SessionBoard(SessionWire):
    """The board lines the bridge derived from Core at the moment of asking."""

    request: ClassVar[str] = "board"
    # Past this the bridge is held behind an install's maintenance turn; a tool call never waits it out.
    timeout: ClassVar[float] = 2

    lines: tuple[str, ...]

    @classmethod
    def presented(cls, due: Sequence[BoardEntry], unchanged: Sequence[BoardEntry], now: float) -> SessionBoard:
        """Each change once, then exactly one summary of the current unchanged lines."""
        lines = tuple(entry.line(now) for entry in due)
        if unchanged:
            lines += (cls.summary(unchanged, now),)
        return cls(lines)

    @classmethod
    def summary(cls, entries: Sequence[BoardEntry], now: float, description: str = "unchanged") -> str:
        """One count/oldest presentation; callers supply their reason for showing it."""
        oldest = min(entries, key=lambda entry: entry.since)
        return (f"BOARD: {len(entries)} {description} (oldest: {oldest.text[:60]}, "
                f"seen {oldest.minutes(now)} min)")

    @classmethod
    def asked(cls, endpoint: Path) -> SessionBoard:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(cls.timeout)
                peer.connect(str(endpoint))
                peer.sendall(json.dumps({cls.family_discriminator: cls.request}).encode() + b"\n")
                with peer.makefile("rb") as stream:
                    return cls.replied(**json.loads(stream.readline()))
        except (OSError, json.JSONDecodeError) as error:
            raise UnexpectedBoardReply(f"The session bridge did not answer: {error}") from error

    @classmethod
    def replied(cls, **reply: Sequence[str]) -> SessionBoard:
        """Decode the declared member first; a refusal is not a constructor error."""
        member = reply.pop(cls.family_discriminator)
        if member != cls.request:
            raise UnexpectedBoardReply(f"{member}: {reply!r}")
        return cls.decoded(**reply)

    @classmethod
    def decoded(cls, lines: Sequence[str]) -> SessionBoard:
        """Decode this member's field only after its discriminator matched."""
        return cls(tuple(lines))
