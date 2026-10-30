"""The Claude Code leaf of the rule gate (agent_comms.rule_deal): its deal lives beside the
transcript, and a refusal is a PreToolUse denial.

PreToolUse only judges: it refuses any tool but reading a dealt file and answering it while one is
unread or unanswered, or the audit does not answer the deal. PostToolUse, which Claude Code sends only after a tool completes,
records a Read that covered a whole dealt file; a failed or partial Read leaves the gate closed.
A subagent is gated as its own session: its deal, reads and audit sit beside its own transcript,
it may not write the main agent's audit, and it reports its tool activity through its seat.
"""

from __future__ import annotations

import json
import sys
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


class ToolMeaning(DeclaredFamily):
    """What one Claude tool means to the gate; a member names its tool in its declaration."""

    @classmethod
    def of(cls, tool: str) -> ToolMeaning:
        return cls.member_or_default(tool, OtherTool)()

    def judge(self, call: GatedCall) -> None:
        """Any tool waits for every dealt file to be read and the audit to answer the deal."""
        call.judge_tool(call.tool_name)

    def completed(self, call: GatedCall) -> None:
        """What a completed call records; most record nothing."""
        return


class OtherTool(ToolMeaning):
    """Every tool the family does not name."""


class ReadTool(ToolMeaning, declared_name="Read"):
    """Reading a dealt file always runs, and a read covering all of it counts once complete;
    any other read waits like the rest."""

    def judge(self, call: GatedCall) -> None:
        call.judge_read(call.tool_name, ReadInput.decode(call.tool_input).file_path.resolve())

    def completed(self, call: GatedCall) -> None:
        read = ReadInput.decode(call.tool_input)
        call.record_read(read.file_path.resolve(), read.span())


class WriteTool(ToolMeaning, declared_name="Write"):
    """Writing the audit itself is the way through the gate; any other write waits."""

    def judge(self, call: GatedCall) -> None:
        call.judge_write(call.tool_name, WriteInput.decode(call.tool_input).file_path.resolve())


class EditTool(WriteTool, declared_name="Edit"):
    """Answering one rule edits the audit in place; an edit is judged like a write."""


@dataclass(frozen=True)
class GatedCall(ClaudeToolHook, DealtRuleGate):
    """One tool call of the session's own agent, judged before it runs and seen after it
    completes. Its deal lives beside the transcript, and a refusal is a PreToolUse denial."""

    @classmethod
    def of(cls, record: Mapping[str, object]) -> GatedCall:
        """The boundary decode: Claude Code names a subagent's call by its agent_id."""
        if "agent_id" in record:
            return SubagentCall.decode(record)
        return cls.decode(record)

    def judge(self) -> None:
        """PreToolUse: refuse this call, or let it run."""
        ToolMeaning.of(self.tool_name).judge(self)

    def completed(self) -> None:
        """PostToolUse: what this completed call records."""
        ToolMeaning.of(self.tool_name).completed(self)

    def refuse(self, reason: str) -> None:
        self.respond(permissionDecision="deny", permissionDecisionReason=reason)


@dataclass(frozen=True)
class SubagentCall(GatedCall, SeatedSubagent):
    """A subagent shares its session's id and transcript path, but its deal is its own: it
    lives beside the subagent's own transcript, so its reads, its audit and its calls never
    touch the main agent's, and it reports its own tool activity."""

    @property
    def stem(self) -> Path:
        return self.subagent_transcripts / f"agent-{self.agent_id}.jsonl"

    def judge(self) -> None:
        self.enter(ToolRunningPhase(self.tool_name))
        super().judge()

    def judge_write(self, tool: str, path: Path) -> None:
        """The main agent's audit is never a subagent's to write, whatever its context says."""
        if path == self.audit_beside(super().stem).resolve():
            self.refuse(f"{path} is the main agent's audit; yours is {self.audit}.")
            return
        super().judge_write(tool, path)


class ReadGateProgram(ClaudeHookProgram):
    events = ("PreToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    GatedCall.of(json.load(sys.stdin)).judge()


if __name__ == "__main__":
    main()
