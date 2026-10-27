"""After a start or compaction deals a bound session its sources, refuse every tool but Read
until each dealt part has been read in full.

The deal's order to read the parts is text, and text loses to a compaction summary: the model
holds its own paraphrase of the rules and treats the order as optional. This gate makes the
order the only path forward. It reads two files beside the transcript and nothing else, so it
costs no registry or bus read on any tool call. A subagent runs on its parent's transcript and
is never gated: the parent reads the parts.
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms_hooks import ClaudeHookProgram, ClaudeToolHook


class DefaultGate(ABC):
    """The gated call every tool the family does not name decodes as."""


@dataclass(frozen=True)
class GatedCall(ClaudeToolHook, DeclaredFamily):
    """One tool call the read gate judges; a member names its tool in its declaration and
    says only what that tool means."""

    @classmethod
    def of(cls, record: Mapping[str, object]) -> GatedCall:
        """The boundary decode: a subagent's call, a tool the family names, or any other."""
        if "agent_id" in record:
            return SubagentCall.decode(record)
        return cls.member_or_default(record["tool_name"], DefaultGate).decode(record)

    @abstractmethod
    def judge(self) -> None:
        """Record or refuse this call."""

    def unread(self) -> tuple[Path, ...]:
        """The dealt parts not yet read in full since the last deal. A deal writes the read
        record, so a session with no record was dealt nothing to read."""
        if not self.parts_read.exists():
            return ()
        read = set(self.parts_read.read_text(encoding="utf-8").split())
        return tuple(part for part in sorted(self.parts.iterdir()) if part.name not in read)


@dataclass(frozen=True)
class ReadCall(GatedCall, declared_name="Read"):
    """A Read is always allowed; a full Read of a dealt part counts it as read."""

    partial: ClassVar[tuple[str, ...]] = ("offset", "limit")
    """Read arguments that return less than the whole file."""

    def judge(self) -> None:
        path = Path(str(self.tool_input["file_path"]))
        if path.parent.resolve() != self.parts.resolve() or any(
                key in self.tool_input for key in self.partial):
            return
        with self.parts_read.open("a", encoding="utf-8") as read:
            read.write(path.name + "\n")


@dataclass(frozen=True)
class OtherCall(GatedCall, DefaultGate):
    """Any other tool waits for every dealt part to be read."""

    def judge(self) -> None:
        if unread := self.unread():
            self.respond(
                permissionDecision="deny",
                permissionDecisionReason=(
                    "Read the rules you were dealt first, each in full with the Read tool (no"
                    " offset or limit): " + ", ".join(str(part) for part in unread)
                    + ". Your summary is a paraphrase of them, not a reading."),
            )


@dataclass(frozen=True)
class SubagentCall(GatedCall):
    """A subagent's call: its parent reads the parts, so it is never refused."""

    agent_id: str

    def judge(self) -> None:
        return


class ReadGateProgram(ClaudeHookProgram):
    events = ("PreToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    GatedCall.of(json.load(sys.stdin)).judge()


if __name__ == "__main__":
    main()
