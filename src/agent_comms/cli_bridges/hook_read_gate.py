"""The Claude Code leaf of the rule gate (agent_comms.rule_deal): its deal lives beside the
transcript, and a refusal is a PreToolUse denial.

PreToolUse only judges: it refuses any tool but reading a dealt file and answering it while one is
unread or unanswered, or the audit does not answer the deal. PostToolUse, which Claude Code sends
only after a tool completes, records a Read that covered a whole dealt file; a failed or partial
Read leaves the gate closed.
A subagent is gated as its own session: its deal, reads and audit sit beside its own transcript,
it may not write the main agent's audit, and it reports its tool activity through its seat.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from agent_comms_hooks.shell_commands import ConnectCommand, ShellCommand
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.rule_deal import Asked, DealtRuleGate, Mute, Owed, ReadSpan, RuleDeal, Speaking, Voice
from agent_comms.comms import Comms
from agent_comms.compaction_notices import CompactionNotice
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.session_receipts import SessionReceipts
from agent_comms.cli_bridges.shared import Binding, Watch
import time
from agent_comms.turn_phase import ToolRunningPhase
from agent_comms_hooks import ClaudeToolHook, ClaudeTranscriptHook, HookRecord
from agent_comms.channel_targets import ThreadTarget
from agent_comms.task_attachments.owner_notices import MeantNotice
from .claude_transcript import ClaudeJsonLines
from .hook_forks import SeatedSubagent


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


@dataclass(frozen=True)
class MonitorInput(HookRecord):
    """Only the bridge's watch may run inside an unfinished deal, not arbitrary Monitor code."""

    command: str
    options: ClassVar[frozenset[str]] = frozenset({"--state"})

    def watches_bus(self) -> bool:
        return any(command.invokes(ConnectCommand.program, Watch.command_name())
                   and command.value_options(self.options)
                   for command in ShellCommand.standalone(self.command))


class ToolMeaning(DeclaredFamily):
    """What one Claude tool means to the gate; a member names its tool in its declaration."""

    @classmethod
    def of(cls, tool: str) -> ToolMeaning:
        return cls.member_or_default(tool, OtherTool)()

    def judge(self, call: GatedCall) -> None:
        """Any tool waits for every dealt file to be read and the audit to answer the deal."""
        call.judge_tool(call.tool_name)

    def completed(self, call: GatedCall) -> bool:
        """Whether this completion starts reading the deal."""
        return False


class OtherTool(ToolMeaning):
    """Every tool the family does not name."""


class ReadTool(ToolMeaning, declared_name="Read"):
    """Reading a dealt file always runs, and a read covering all of it counts once complete;
    any other read waits like the rest."""

    def judge(self, call: GatedCall) -> None:
        call.judge_read(call.tool_name, ReadInput.decode(call.tool_input).file_path.resolve())

    def completed(self, call: GatedCall) -> bool:
        read = ReadInput.decode(call.tool_input)
        return call.record_read(read.file_path.resolve(), read.span())


class MonitorTool(ToolMeaning, declared_name="Monitor"):
    """Bus input must survive a deal; unrelated Monitor commands stay gated."""

    def judge(self, call: GatedCall) -> None:
        if not MonitorInput.decode(call.tool_input).watches_bus():
            call.judge_tool(call.tool_name)


class WriteTool(ToolMeaning, declared_name="Write"):
    """Writing the audit itself is the way through the gate; ask once its write completes."""

    def judge(self, call: GatedCall) -> None:
        call.judge_write(call.tool_name, WriteInput.decode(call.tool_input).file_path.resolve())

    def completed(self, call: GatedCall) -> bool:
        if WriteInput.decode(call.tool_input).file_path.resolve() == call.audit.resolve():
            call.audit_written()
        return False


class EditTool(WriteTool, declared_name="Edit"):
    """Answering one rule edits the audit in place; an edit is judged like a write."""


class TranscriptWords(ClaudeTranscriptHook, RuleDeal):
    """The session's transcript supplies bounded display receipts, never audit answers."""

    def receipts(self) -> SessionReceipts:
        """The transcript's recent past: a gate hook runs often, so it never reads more of it."""
        return ClaudeJsonLines().receipts(self.tail())


@dataclass(frozen=True)
class GatedCall(ClaudeToolHook, TranscriptWords, DealtRuleGate, Speaking):
    """One tool call of the session's own agent, judged before it runs and seen after it
    completes. Its deal lives beside the transcript, and a refusal is a PreToolUse denial."""

    comms: Comms = field(kw_only=True, compare=False, repr=False)
    binding: Binding = field(kw_only=True, compare=False, repr=False)

    @classmethod
    def of(cls, record: Mapping[str, object], **context: object) -> GatedCall:
        return (SubagentCall if cls.raised_by_subagent(record) else cls).decode(record, **context)

    def judge(self) -> None:
        """PreToolUse: refuse this call, or let it run."""
        ToolMeaning.of(self.tool_name).judge(self)

    def completed(self) -> int:
        """Decode/read outside the call lock, then claim one ordinal and its first notice."""
        first = ToolMeaning.of(self.tool_name).completed(self)
        if not self.record.exists():
            return 0
        with self.recorded_call() as call:
            self.announce(call.notices.first_read() if first else (), call.number)
            return call.number

    def announce(self, notices: tuple[type[CompactionNotice], ...], number: int) -> None:
        for declaration in notices:
            notice = declaration(self.binding.owner.require_recorded(), self.tool_name, number,
                                 RecordedEventTime(time.time()))
            self.comms.messaging.send(notice.owner.name, notice.channel, notice.body, role=notice.role)

    def blocked(self, cause: Owed, tool: str) -> None:
        template = self.template_once(cause.record.due)
        with self.recorded_call() as call:
            self.refuse(cause.explain_refusal(tool, call.number, template))
            self.announce(call.refused(), call.number)

    def refuse(self, reason: str) -> None:
        self.respond(permissionDecision="deny", permissionDecisionReason=reason)

    def voice(self) -> Voice:
        """The supplied resident bus and binding are already this call's speaking seat."""
        return self

    def say(self, line: str, deal: RuleDeal) -> Asked:
        owner = self.binding.owner.require_recorded()
        message = MeantNotice().publish(self.comms.messaging, ThreadTarget(owner.name), line)
        return Asked(message.message_id)


@dataclass(frozen=True)
class SubagentCall(GatedCall, SeatedSubagent):
    """A subagent shares its session's id and transcript path, but its deal is its own: it
    lives beside the subagent's own transcript, so its reads, its audit and its calls never
    touch the main agent's, and it reports its own tool activity."""

    def judge(self) -> None:
        self.enter(self.comms, self.binding, ToolRunningPhase(self.tool_name))
        super().judge()

    def voice(self) -> Voice:
        """The main session's bus seat cannot ask a question inside its subagent's turn."""
        return Mute()

    def judge_write(self, tool: str, path: Path) -> None:
        """The main agent's audit is never a subagent's to write, whatever its context says."""
        if path == self.audit_beside(self.session_stem).resolve():
            self.refuse(f"{path} is the main agent's audit; yours is {self.audit}.")
            return
        super().judge_write(tool, path)
