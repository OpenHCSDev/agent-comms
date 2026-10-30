"""The Claude Code leaf of the rule gate (agent_comms.rule_deal): its deal lives beside the
transcript, and a refusal is a PreToolUse denial.

PreToolUse only judges: it refuses any tool but reading a dealt file, answering it, hearing the
bus and arming the watch while one is unread or unanswered, or the audit does not answer the deal.
PostToolUse, which Claude Code sends only after a tool completes, records a Read that covered a
whole dealt file; a failed or partial Read leaves the gate closed.
A subagent is gated as its own session: its deal, reads and audit sit beside its own transcript,
it may not write the main agent's audit, and it reports its tool activity through its seat.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms.rule_deal import AuditDue, DealtRuleGate, ReadSpan
from agent_comms.turn_phase import ToolRunningPhase
from agent_comms_hooks import ClaudeHookProgram, ClaudeToolHook, HookRecord
from agent_comms_hooks.seats import SeatedSubagent


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


def typed_prompt(raw: bytes) -> str:
    """What the person typed in one transcript line: a user record or a prompt queued into a
    running turn, each marked with a human origin. Any other line carries none."""
    record = json.loads(raw)
    queued = record.get("attachment") or {}
    origin, content = (
        (queued.get("origin"), queued.get("prompt")) if queued.get("type") == "queued_command"
        else (record.get("origin"), (record.get("message") or {}).get("content"))
        if record.get("type") == "user" else (None, None))
    if not origin or origin.get("kind") != "human":
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content or () if block.get("type") == "text")


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


class HearingTool(ToolMeaning):
    """The bus stays open during a deal: on 2026-10-30 the commander's watch expired mid-deal and
    could not be re-armed, and Tristan's orders waited unread behind the files he had it reading."""

    def judge(self, call: GatedCall) -> None:
        return


class BusInbox(HearingTool, declared_name="mcp__comms__comms_inbox"):
    """What the bus delivered to this session."""


class BusHistory(HearingTool, declared_name="mcp__comms__comms_history"):
    """What was said on a thread or channel."""


class BusThreads(HearingTool, declared_name="mcp__comms__comms_threads"):
    """Who is on the bus."""


class BusSend(HearingTool, declared_name="mcp__comms__comms_send"):
    """Answering him. Orders and verdicts wait for the deal; a word to the bus does not."""


class WatchArming(ToolMeaning):
    """The one command that re-arms this session's watch runs; any other waits like the rest."""

    command: ClassVar[str] = "agent-comms-claude-bridge watch"

    def judge(self, call: GatedCall) -> None:
        if call.tool_input.get("command") != self.command:
            super().judge(call)


class BashTool(WatchArming, declared_name="Bash"):
    """The shell, which may arm the watch."""


class MonitorTool(WatchArming, declared_name="Monitor"):
    """The Monitor, which is how the watch is held."""


@dataclass(frozen=True)
class GatedCall(ClaudeToolHook, DealtRuleGate):
    """One tool call of the session's own agent, judged before it runs and seen after it
    completes. Its deal lives beside the transcript, and a refusal is a PreToolUse denial."""

    @classmethod
    def of(cls, record: Mapping[str, object]) -> GatedCall:
        return (SubagentCall if cls.raised_by_subagent(record) else cls).decode(record)

    def judge(self) -> None:
        """PreToolUse: refuse this call, or let it run."""
        ToolMeaning.of(self.tool_name).judge(self)

    def completed(self) -> None:
        """PostToolUse: what this completed call records."""
        ToolMeaning.of(self.tool_name).completed(self)

    def owner_said(self, due: AuditDue) -> tuple[str, ...]:
        """His last prompt in the transcript's tail as this call runs, not as the deal showed it.
        Core's transcript decoder costs a hook 3 s of imports, so this reads the two records that
        carry his typing with the standard library; past the window the deal's copy stands."""
        if not Path(self.transcript_path).exists():
            return super().owner_said(due)
        for raw in reversed(self.tail()):
            if words := typed_prompt(raw):
                return (words,)
        return super().owner_said(due)

    def refuse(self, reason: str) -> None:
        self.respond(permissionDecision="deny", permissionDecisionReason=reason)


@dataclass(frozen=True)
class SubagentCall(GatedCall, SeatedSubagent):
    """A subagent shares its session's id and transcript path, but its deal is its own: it
    lives beside the subagent's own transcript, so its reads, its audit and its calls never
    touch the main agent's, and it reports its own tool activity."""

    def judge(self) -> None:
        self.enter(ToolRunningPhase(self.tool_name))
        super().judge()

    def judge_write(self, tool: str, path: Path) -> None:
        """The main agent's audit is never a subagent's to write, whatever its context says."""
        if path == self.audit_beside(self.session_stem).resolve():
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
