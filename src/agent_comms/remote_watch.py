"""The live bridge over the connector: follow one thread's inbox on a remote bus and print
each new message as one line.

A Claude Code session in the cloud runs this as a Monitor, so every message to its thread
wakes it the way the local bridge wakes a session on the bus host. The wait happens in Core,
on the bus's own commit signal (CommsInboxTool's wait), through the same request a local
UI sends; nothing here polls on a timer.

    python -m agent_comms.remote_watch --profile PATH --thread NAME
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .agent_inbox import SecondsWait, TakenInputPosition, TakenInputStart
from .core_access import CoreAccess
from .messages import Message
from .remote_core import RemoteCoreAccess
from .tools import CommsInboxTool


@dataclass(frozen=True)
class RemoteInboxWatch:
    core: CoreAccess
    thread: str
    wait: SecondsWait = SecondsWait(SecondsWait.longest)
    reconnect_seconds: float = 5.0

    def caught_up(self) -> TakenInputPosition:
        """After every message already waiting: the session starts from now, and reads the
        backlog itself if it wants it, instead of being woken once per old message."""
        after: TakenInputPosition = TakenInputStart()
        while (page := self.core.call(CommsInboxTool(thread=self.thread, after=after))).messages:
            after = after.advanced(page.messages[-1].reference)
        return after

    def messages(self) -> Iterator[Message]:
        after = self.caught_up()
        while True:
            try:
                page = self.core.call(CommsInboxTool(thread=self.thread, after=after, wait=self.wait))
            except OSError as unreachable:
                print(f"remote_watch: connector unreachable, retrying: {unreachable}", flush=True)
                time.sleep(self.reconnect_seconds)
                continue
            for message in page.messages:
                yield message
                after = after.advanced(message.reference)

    @staticmethod
    def line(message: Message) -> str:
        """One Monitor event per message: who, where, which message, and its first line."""
        first, _, rest = message.body.strip().partition("\n")
        more = " …" if rest else ""
        return (f"{message.sender} -> {message.target} [{message.reference.message_id}]: "
                f"{first[:240]}{more}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", type=Path, required=True,
                        help="the owner's UI connection profile (endpoint and credential)")
    parser.add_argument("--thread", required=True, help="the thread whose inbox to follow")
    options = parser.parse_args(argv)
    watch = RemoteInboxWatch(RemoteCoreAccess.read(options.profile), options.thread)
    print(f"remote_watch: following {options.thread}", flush=True)
    for message in watch.messages():
        print(watch.line(message), flush=True)


if __name__ == "__main__":
    sys.exit(main())
