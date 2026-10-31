"""The Claude Code leaf of the rule gate (agent_comms.rule_deal): its deal lives beside the
transcript, and a refusal is a PreToolUse denial.

PreToolUse only judges: it refuses any tool but reading a dealt file and answering it while one is
unread or unanswered, or the audit does not answer the deal. PostToolUse, which Claude Code sends
only after a tool completes, records a Read that covered a whole dealt file; a failed or partial
Read leaves the gate closed.
A subagent is gated as its own session: its deal, reads and audit sit beside its own transcript,
it may not write the main agent's audit, and it reports its tool activity through its seat.

A completed audit edit asks, in Tristan's voice over the session's bus seat, whether the answers
were meant; the session's words are read from its own transcript after the question went out.
Only that path imports the transcript decoder and the bus client, so no ordinary call pays for
them.
"""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_comms.declared_family import DeclaredFamily
from agent_comms.rule_deal import Asked, DealtRuleGate, Mute, ReadSpan, RuleDeal, Speaking, Voice
from agent_comms.turn_phase import ToolRunningPhase
from agent_comms_hooks import ClaudeHookProgram, ClaudeToolHook, ClaudeTranscriptHook, HookRecord
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


@dataclass(frozen=True)
class MonitorInput(HookRecord):
    """The Monitor tool's input: the command it watches."""

    command: str

    options: ClassVar[frozenset[str]] = frozenset({"--state"})
    """The watch's own options, each taking one value."""
    shell: ClassVar[frozenset[str]] = frozenset(";|&$`<>()\n")

    def watches_bus(self) -> bool:
        """Exactly the session bridge's watch, bare or with its state directory: Monitor runs
        any shell command, so anything else is a tool like the rest."""
        from agent_comms.cli_bridges.claude import main as bridge

        if self.shell & set(self.command):
            return False
        words = shlex.split(self.command)
        program = ClaudeHookProgram.console_script(f"{bridge.__module__}:{bridge.__name__}")
        options = words[2:]
        return (len(words) >= 2 and Path(words[0]).name == program and words[1] == "watch"
                and len(options) % 2 == 0 and set(options[::2]) <= self.options)


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


class MonitorTool(ToolMeaning, declared_name="Monitor"):
    """The bus watch is how Tristan's lines reach the session, his question included, so a
    deal never refuses it: on 2026-10-30 claude-board and claude-nra sat in the gate with
    expired watches and every line to them waited unread. Any other Monitor waits."""

    def judge(self, call: GatedCall) -> None:
        if not MonitorInput.decode(call.tool_input).watches_bus():
            call.judge_tool(call.tool_name)


class WriteTool(ToolMeaning, declared_name="Write"):
    """Writing the audit itself is the way through the gate; any other write waits. A completed
    audit write is followed by the question whether it was meant."""

    def judge(self, call: GatedCall) -> None:
        call.judge_write(call.tool_name, WriteInput.decode(call.tool_input).file_path.resolve())

    def completed(self, call: GatedCall) -> None:
        if WriteInput.decode(call.tool_input).file_path.resolve() == call.audit.resolve():
            call.audit_written()


class EditTool(WriteTool, declared_name="Edit"):
    """Answering one rule edits the audit in place; an edit is judged like a write."""


class TranscriptWords(ClaudeTranscriptHook, RuleDeal):
    """A Claude deal reads its session's own words from the transcript beside it: the one
    record of what the model said, written before each tool call it makes."""

    def words_end(self) -> int:
        return self.stem.stat().st_size

    def complete_lines(self, offset: int) -> list[tuple[int, bytes]]:
        """Every whole line after `offset` with its position; a line still being written waits."""
        with self.stem.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read()
        lines, position = [], offset
        for line in raw.splitlines(keepends=True):
            if line.endswith(b"\n"):
                lines.append((position, line))
            position += len(line)
        return lines

    def said_since(self, offset: int) -> tuple[str, ...]:
        from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines

        decoder = ClaudeJsonLines()
        return tuple(words for position, line in self.complete_lines(offset)
                     for words in decoder.decode_line(line, position).said())

    def said_after(self, asked: Asked) -> tuple[str, ...]:
        """The words in the lines after the one that delivered this line's message."""
        lines = self.complete_lines(asked.offset)
        arrival = next((index for index, (_, line) in enumerate(lines)
                        if asked.message_id.encode() in line), len(lines))
        from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines

        decoder = ClaudeJsonLines()
        return tuple(words for position, line in lines[arrival + 1:]
                     for words in decoder.decode_line(line, position).said())


@dataclass(frozen=True)
class OwnerVoice(Speaking):
    """Tristan's voice over the session's bus seat: an owner notice to its own thread, which
    its bridge delivers mid-turn like any line of his."""

    thread: str
    root: str

    def say(self, line: str, deal: RuleDeal) -> Asked:
        from agent_comms.channel_targets import ThreadTarget
        from agent_comms.comms import client_wire
        from agent_comms.task_attachments.owner_notices import MeantNotice

        offset = deal.words_end()
        with client_wire(self.root) as comms:
            message = MeantNotice().publish(comms.messaging, ThreadTarget(self.thread), line)
        return Asked(str(message.message_id), offset)


@dataclass(frozen=True)
class GatedCall(ClaudeToolHook, TranscriptWords, DealtRuleGate):
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

    def refuse(self, reason: str) -> None:
        self.respond(permissionDecision="deny", permissionDecisionReason=reason)

    def voice(self) -> Voice:
        """The bus seat this session is bound to speaks for Tristan; an unbound one is mute."""
        from agent_comms_hooks.watch import WatchClaim

        return next((OwnerVoice(record.bus, record.root)
                     for record in WatchClaim(self.session_id).state().bindings()), Mute())


@dataclass(frozen=True)
class SubagentCall(GatedCall, SeatedSubagent):
    """A subagent shares its session's id and transcript path, but its deal is its own: it
    lives beside the subagent's own transcript, so its reads, its audit and its calls never
    touch the main agent's, and it reports its own tool activity."""

    def judge(self) -> None:
        self.enter(ToolRunningPhase(self.tool_name))
        super().judge()

    def voice(self) -> Voice:
        """The session's bus seat belongs to the main agent: a question sent there would land
        in its turn, not the subagent's."""
        return Mute()

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
