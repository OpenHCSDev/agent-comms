"""One bus thread's harness on another machine: its commands reach that thread on a remote
Core through the thread's private profile, so no local Core root is needed."""

from __future__ import annotations

import sys
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from .agent_inbox import SecondsWait
from .command import Command
from .declared_verb import DeclaredVerb
from .nominal.field_codec import Argument, FieldCodec, PathText, boundary_decoder
from .connector_profile import ThreadCredential
from .messages import Message
from .remote_core import ConnectorCalls, RemoteThreadAccess
from .tools import CommsInboxTakenTool, CommsInboxTool


@dataclass(frozen=True, kw_only=True)
class RemoteThreadVerb(DeclaredVerb, Command[RemoteThreadAccess, int], affix="Verb"):
    """One command a remote harness runs as its own bus thread."""

    program: ClassVar[str] = "agent-comms-remote"
    profile: Annotated[
        Path, PathText,
        Argument(flags=("--profile",), options={"help": "This thread's private profile"}),
    ]

    @classmethod
    def tool(cls) -> str:
        return cls.program

    @abstractmethod
    def apply(self, ctx: RemoteThreadAccess) -> int: ...


@dataclass(frozen=True, kw_only=True)
class WatchInboxVerb(RemoteThreadVerb, declared_name="watch-inbox"):
    help = "Print each message this thread has not taken as one JSON line, waiting on Core"

    seconds: Annotated[
        int, Argument(flags=("--seconds",), options={"help": "Seconds each read waits on Core"}),
    ] = SecondsWait.longest

    def apply(self, ctx: RemoteThreadAccess) -> int:
        for message in self.messages(ctx):
            print(FieldCodec.compact(FieldCodec.encode(message)), flush=True)
        return 0

    def messages(self, ctx: ConnectorCalls[ThreadCredential]) -> Iterator[Message]:
        """Each untaken message once, in order, across restarts: once the caller has handled a
        message it is recorded as taken, so a restart replays at most the one in hand. A read
        that finds none waits on Core again."""
        wait = SecondsWait(self.seconds)
        while True:
            page = ctx.call(CommsInboxTool(thread=ctx.credential.thread, wait=wait))
            for message in page.messages:
                yield message
                ctx.call(CommsInboxTakenTool(reference=message.reference))


@boundary_decoder
def main(argv: Sequence[str] | None = None) -> int:
    verb = RemoteThreadVerb.parse(sys.argv[1:] if argv is None else argv)
    return verb.apply(RemoteThreadAccess.read(verb.profile))
