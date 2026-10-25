"""The commander's board, printed on every tool call of its Claude session.

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


class SessionWire:
    """The session socket's records: one JSON line each, its family member named by the key."""

    family_discriminator: ClassVar[str] = "type"


@dataclass(frozen=True)
class SessionBoard(SessionWire):
    """The board lines the bridge derived from Core at the moment of asking."""

    request: ClassVar[str] = "board"
    # Past this the bridge is held behind an install's maintenance turn; a tool call never waits it out.
    timeout: ClassVar[float] = 2

    lines: tuple[str, ...]

    @classmethod
    def asked(cls, endpoint: Path) -> SessionBoard:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(cls.timeout)
            peer.connect(str(endpoint))
            peer.sendall(json.dumps({cls.family_discriminator: cls.request}).encode() + b"\n")
            with peer.makefile("rb") as stream:
                return cls.replied(**json.loads(stream.readline()))

    @classmethod
    def replied(cls, lines: Sequence[str], **family: str) -> SessionBoard:
        """The reply's discriminator names this member; its one field is the lines. A reply of
        any other shape, an older bridge's refusal included, raises TypeError here."""
        return cls(tuple(lines))


@dataclass(frozen=True)
class UnreadableBoard:
    """A bridge that cannot answer says so on the board instead of printing it empty."""

    error: Exception

    @property
    def lines(self) -> tuple[str, ...]:
        return (f"BOARD UNREADABLE: the session bridge did not answer ({self.error!r})",)
