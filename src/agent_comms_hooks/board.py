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
from typing import ClassVar, Self


class UnexpectedBoardReply(ValueError):
    """The session bridge refused or could not answer the board read."""


class AbsentSessionSocket(UnexpectedBoardReply):
    """Nothing listens at the session socket: its file is gone, or no bridge accepts on it."""


class SessionWire:
    """The session socket's records: one JSON line each, its family member named by the key."""

    family_discriminator: ClassVar[str] = "type"
    request: ClassVar[str]
    # Past this the bridge is held behind an install's maintenance turn; a tool call never waits it out.
    timeout: ClassVar[float] = 2

    @classmethod
    def exchanged(cls, endpoint: Path, **fields: str) -> dict[str, Sequence[str]]:
        """Ask the bridge for this member and return its one reply line, undecoded."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(cls.timeout)
                peer.connect(str(endpoint))
                asked = {cls.family_discriminator: cls.request, **fields}
                peer.sendall(json.dumps(asked).encode() + b"\n")
                with peer.makefile("rb") as stream:
                    reply: dict[str, Sequence[str]] = json.loads(stream.readline())
                    return reply
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise AbsentSessionSocket(f"No session bridge is listening: {error}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise UnexpectedBoardReply(f"The session bridge did not answer: {error}") from error

    @classmethod
    def replied(cls, **reply: Sequence[str]) -> Self:
        """Decode the declared member first; a refusal is not a constructor error."""
        member = reply.pop(cls.family_discriminator)
        if member != cls.request:
            raise UnexpectedBoardReply(f"{member}: {reply!r}")
        return cls.decoded(**reply)

    @classmethod
    def decoded(cls, **fields: Sequence[str]) -> Self:
        """Decode this member's fields only after its discriminator matched."""
        return cls(**{name: tuple(value) for name, value in fields.items()})


@dataclass(frozen=True)
class BoardEntry:
    """A current Core observation, never a separately stored board."""

    content: str
    text: str
    since: float
    alarm_at: float
    owner_flagged: bool = False

    def minutes(self, now: float) -> int:
        return int((now - self.since) // 60)

    def aged(self, now: float) -> str:
        return f"{self.text} (seen for {self.minutes(now)} min)"

    def line(self, now: float) -> str:
        return f"BOARD: {self.aged(now)}"


@dataclass(frozen=True)
class BoardSummary:
    """One presentation of the current lines; no caller supplies a prose discriminator."""

    entries: Sequence[BoardEntry]
    now: float

    shown_chars: ClassVar[int] = 160

    @property
    def flagged(self) -> tuple[BoardEntry, ...]:
        return tuple(entry for entry in self.entries if entry.owner_flagged)

    @property
    def shown(self) -> BoardEntry:
        """Tristan's lines lead; each minute names the next, so no row becomes wallpaper."""
        rotation = self.flagged or tuple(self.entries)
        return rotation[int(self.now // 60) % len(rotation)]

    def line(self) -> str:
        shown = self.shown
        return (
            f"BOARD: {len(self.entries)} unchanged, {len(self.flagged)} from Tristan: "
            f"{shown.text[:self.shown_chars]} (seen {shown.minutes(self.now)} min)"
        )


@dataclass(frozen=True)
class SessionBoard(SessionWire):
    """The board lines the bridge derived from Core at the moment of asking."""

    request: ClassVar[str] = "board"

    lines: tuple[str, ...]

    @classmethod
    def presented(
        cls, due: Sequence[BoardEntry], unchanged: Sequence[BoardEntry], now: float
    ) -> SessionBoard:
        """Each change once, then exactly one summary of the current unchanged lines."""
        lines = tuple(entry.line(now) for entry in due)
        if unchanged:
            lines += (BoardSummary(unchanged, now).line(),)
        return cls(lines)

    @classmethod
    def asked(cls, endpoint: Path) -> SessionBoard:
        return cls.replied(**cls.exchanged(endpoint))


@dataclass(frozen=True)
class SessionOwnerSaid(SessionWire):
    """What the person last typed in this session, read by the bridge at the moment of asking."""

    request: ClassVar[str] = "owner_said"

    words: tuple[str, ...]

    @classmethod
    def asked(cls, endpoint: Path, transcript: Path) -> SessionOwnerSaid:
        return cls.replied(**cls.exchanged(endpoint, transcript=str(transcript)))
