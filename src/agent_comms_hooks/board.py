"""The teammate's own board rows: each new or changed row naming the session, once.

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


class AbsentSessionSocket(UnexpectedBoardReply):
    """Nothing listens at the session socket: its file is gone, or no bridge accepts on it."""


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
class SessionBoard(SessionWire):
    """The board lines the bridge derived from Core at the moment of asking."""

    request: ClassVar[str] = "board"
    # Past this the bridge is held behind an install's maintenance turn; a tool call never waits it out.
    timeout: ClassVar[float] = 2

    lines: tuple[str, ...]

    @classmethod
    def presented(cls, due: Sequence[BoardEntry], now: float) -> SessionBoard:
        """Each new or changed row once; an unchanged row says nothing again."""
        return cls(tuple(entry.line(now) for entry in due))

    @classmethod
    def asked(cls, endpoint: Path) -> SessionBoard:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(cls.timeout)
                peer.connect(str(endpoint))
                peer.sendall(json.dumps({cls.family_discriminator: cls.request}).encode() + b"\n")
                with peer.makefile("rb") as stream:
                    return cls.replied(**json.loads(stream.readline()))
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise AbsentSessionSocket(f"No session bridge is listening: {error}") from error
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
