"""The Claude Code leaf of the rule gate (agent_comms.rule_deal): its deal lives beside the
transcript, and a refusal is a PreToolUse denial.

PreToolUse only judges: it refuses any tool but Read while a dealt file is unread or the audit
does not answer the deal. PostToolUse, which Claude Code sends only after a tool completes,
records a Read that covered a whole dealt file; a failed or partial Read leaves the gate closed.
A subagent runs on its parent's transcript and is never gated: the parent reads the rules, while
the subagent reports tool activity through its existing seat.
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_comms.declared_family import DeclaredFamily
from agent_comms.rule_deal import DealtRuleGate, ReadSpan
from agent_comms.turn_phase import ToolRunningPhase
from agent_comms_hooks import ClaudeHookProgram, ClaudeToolHook, HookRecord
from agent_comms_hooks.forks import SeatedSubagent


@dataclass(frozen=True)
class ReadInput(HookRecord):
    """The Read tool's input. Claude Code omits `offset` and `limit` for a read from the first
    line; the defaults are the tool's own: line 1, and at most 2,000 lines. It refuses a file
    over its size limit rather than truncating it, so a read that succeeded has no byte cap."""

    file_path: Path
    offset: int = 1
    limit: int = 2_000

    def span(self) -> ReadSpan:
        return ReadSpan(self.offset, self.limit, sys.maxsize)


@dataclass(frozen=True)
class WriteInput(HookRecord):
    """The Write tool's input: the gate only needs where it writes."""

    file_path: Path


class DefaultGate(ABC):
    """The gated call every tool the family does not name decodes as."""


@dataclass(frozen=True)
class GatedCall(ClaudeToolHook, DealtRuleGate, DeclaredFamily):
    """One tool call the read gate judges before it runs and sees after it completes; a member
    names its tool in its declaration and says only what that tool means. The deal lives beside
    the transcript, and a refusal is Claude Code's PreToolUse denial."""

    @classmethod
    def of(cls, record: Mapping[str, object]) -> GatedCall:
        """The boundary decode: a subagent's call, a tool the family names, or any other."""
        if "agent_id" in record:
            return SubagentCall.decode(record)
        tool = ClaudeToolHook.decode(record).tool_name
        return cls.member_or_default(tool, DefaultGate).decode(record)

    @abstractmethod
    def judge(self) -> None:
        """PreToolUse: refuse this call, or let it run."""

    def completed(self) -> None:
        """PostToolUse: what this completed call records; most record nothing."""

    def refuse(self, reason: str) -> None:
        self.respond(permissionDecision="deny", permissionDecisionReason=reason)


@dataclass(frozen=True)
class ReadCall(GatedCall, declared_name="Read"):
    """A Read always runs; once it completes, a read covering a whole dealt file counts it."""

    def judge(self) -> None:
        return

    def completed(self) -> None:
        read = ReadInput.decode(self.tool_input)
        self.record_read(read.file_path.resolve(), read.span())


@dataclass(frozen=True)
class OtherCall(GatedCall, DefaultGate):
    """Any other tool waits for every dealt file to be read and the audit to answer the deal."""

    def judge(self) -> None:
        self.judge_tool(self.tool_name)


@dataclass(frozen=True)
class WriteCall(GatedCall, declared_name="Write"):
    """Writing the audit itself is the way through the gate; any other write waits like the rest."""

    def judge(self) -> None:
        self.judge_write(self.tool_name, WriteInput.decode(self.tool_input).file_path.resolve())


@dataclass(frozen=True)
class SubagentCall(GatedCall, SeatedSubagent):
    """A subagent bypasses its parent's read gate but reports its own tool activity."""

    def judge(self) -> None:
        self.enter(ToolRunningPhase(self.tool_name))


class ReadGateProgram(ClaudeHookProgram):
    events = ("PreToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    GatedCall.of(json.load(sys.stdin)).judge()


if __name__ == "__main__":
    main()
