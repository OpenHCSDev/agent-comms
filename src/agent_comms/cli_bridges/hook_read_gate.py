"""Resident read permissions and first-event notices for the session's rule deal."""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_comms_hooks import ClaudeToolHook, HookRecord

from .hook_forks import SeatedSubagent
from .session_watch import WatchClaim
from ..comms import Comms
from ..declared_family import DeclaredFamily
from ..field_codec import FieldCodec, MalformedRecord
from ..instruction_deck import CompactionNotice, DealtCalls
from ..pattern_sources import ReadThenRepeatedFailure
from ..recorded_time import RecordedEventTime
from ..turn_phase import ToolRunningPhase


@dataclass(frozen=True)
class ReadInput(HookRecord):
    """The Read tool's input. Claude Code omits `offset` and `limit` for a read from the first
    line; the defaults are the tool's own: line 1, and at most 2,000 lines."""

    file_path: Path
    offset: int = 1
    limit: int = 2_000

    def covers(self, source: Path) -> bool:
        """Whether this read returned every line of the file."""
        return self.offset <= 1 and self.limit >= len(source.read_text(encoding="utf-8").splitlines())


@dataclass(frozen=True)
class WriteInput(HookRecord):
    """The Write tool's input: the gate only needs where it writes."""

    file_path: Path


@dataclass(frozen=True)
class RuleAudit:
    """One dealt rule, and each action since the last start or compaction that broke it."""

    rule: str
    broke: tuple[str, ...] = ()


@dataclass(frozen=True)
class OwnerWordsAnswer:
    """One of the owner's own prompts the deal showed, and what the session is doing about it."""

    words: str
    doing: str

    def acts(self) -> bool:
        """An answer that names no action is not an answer."""
        return bool(self.doing.strip())


@dataclass(frozen=True)
class SessionAudit:
    """The audit a session writes after reading its deal, decoded once."""

    rules: tuple[RuleAudit, ...]
    owner_words: tuple[OwnerWordsAnswer, ...]


@dataclass(frozen=True)
class AuditDue:
    """What the deal requires the audit to answer: the dealt rules and the prompts shown."""

    rules: frozenset[str]
    owner_words: tuple[str, ...]
    """The owner's prompts exactly as the deal showed them, each occurrence once."""

    def reminder(self, audit_path: Path) -> str:
        failure = ReadThenRepeatedFailure()
        audit = SessionAudit(
            rules=tuple(RuleAudit(rule) for rule in sorted(self.rules)),
            owner_words=tuple(OwnerWordsAnswer(words, "") for words in self.owner_words),
        )
        return "\n\n".join((
            *(f"Tristan told you, verbatim:\n{words}\nYou did not."
              for words in self.owner_words[-1:]),
            f"{failure.title}\n{failure.cite()}\n"
            "This session is repeating it, not observing another session's mistake.",
            f"Write this SessionAudit to {audit_path}. Fill only broke and doing; keep every"
            " rule name and quoted words unchanged. Empty broke means the rule was kept;"
            " doing must name your current action:\n"
            + json.dumps(FieldCodec.encode(audit), ensure_ascii=False, indent=2),
        ))

    def refusal(self, audit: SessionAudit) -> str:
        """Why this audit does not answer the deal, or nothing when it does: every dealt rule
        is audited, and every shown prompt is answered by its own words with an action."""
        missing = sorted(self.rules - {entry.rule for entry in audit.rules})
        if missing:
            return "the audit skips the dealt rules " + ", ".join(missing)
        if idle := [answer.words for answer in audit.owner_words if not answer.acts()]:
            return "the audit names no action for: " + " | ".join(idle)
        unanswered = Counter(self.owner_words) - Counter(
            answer.words for answer in audit.owner_words)
        if unanswered:
            return "the audit does not answer the owner's prompts: " + " | ".join(
                unanswered.elements())
        return ""


class DefaultGate(ABC):
    """The gated call every tool the family does not name decodes as."""


@dataclass(frozen=True)
class GatedCall(ClaudeToolHook, DealtCalls, DeclaredFamily):
    """One tool call the read gate judges before it runs and sees after it completes; a member
    names its tool in its declaration and says only what that tool means."""

    @classmethod
    def of(cls, record: Mapping[str, object], **context: object) -> GatedCall:
        """The boundary decode: a subagent's call, a tool the family names, or any other."""
        if "agent_id" in record:
            return SubagentCall.decode(record, **context)
        tool = ClaudeToolHook.decode(record).tool_name
        return cls.member_or_default(tool, DefaultGate).decode(record, **context)

    @abstractmethod
    def judge(self, comms: Comms) -> None:
        """PreToolUse: refuse this call, or let it run."""

    def completed(self, comms: Comms) -> int:
        """A successful call records once, before any reminder or read notice consumes it."""
        return self.count_call() if self.dealt_read.exists() else 0

    def announce(self, comms: Comms, notices: tuple[type[CompactionNotice], ...], number: int) -> None:
        for notice in notices:
            (binding,) = (record for record in WatchClaim(self.session_id).state().bindings()
                          if Path(record.root).resolve() == comms.root.resolve())
            owner = comms.registry.require(binding.bus).incarnation.require_recorded()
            comms.messaging.report_compaction(notice(
                owner, self.tool_name, number, RecordedEventTime(time.time())))

    def gate(self, comms: Comms) -> None:
        """Refuse this call while a dealt file is unread or the audit does not answer the deal."""
        due = self.required_audits()
        if unread := self.unread():
            self.refuse(comms, "reading", "Read the rules you were dealt first, each in full with the"
                        " Read tool (no offset or limit): "
                        + ", ".join(str(source) for source in unread)
                        + ". Your summary is a paraphrase of them, not a reading.", due)
            return
        for requirement in due:
            if refusal := self.unaudited(requirement):
                self.refuse(comms, "writing the required audit", f"Reading is passive; {refusal}.", due)

    def refuse(self, comms: Comms, before: str, reason: str, due: tuple[AuditDue, ...]) -> None:
        with self.recorded_call() as call:
            self.respond(
                permissionDecision="deny",
                permissionDecisionReason="\n\n".join((
                    f"You just tried {self.tool_name} before {before}. Call #{call.number}"
                    " since this compaction, refused.", reason,
                    *(requirement.reminder(self.audit) for requirement in due),
                )),
            )
            self.announce(comms, call.refused(), call.number)

    def required_audits(self) -> tuple[AuditDue, ...]:
        if not self.audit_due.exists():
            return ()
        return (FieldCodec.decode(AuditDue, FieldCodec.parse(self.audit_due.read_bytes())),)

    def unaudited(self, due: AuditDue) -> str:
        """Why no tool may run yet for want of the audit, or nothing once it answers the deal."""
        if not self.audit.exists():
            return "no audit is written"
        try:
            audit = FieldCodec.decode(SessionAudit, FieldCodec.parse(self.audit.read_bytes()))
        except MalformedRecord as error:
            return f"the audit does not decode: {error}"
        return due.refusal(audit)

    def unread(self) -> tuple[Path, ...]:
        """The dealt files not yet read in full since the last deal. A deal writes the read
        record, so a session with no record was dealt nothing to read."""
        if not self.dealt_read.exists():
            return ()
        read = set(self.dealt_read.read_text(encoding="utf-8").splitlines())
        return tuple(Path(source) for source in self.sources() if source not in read)

    def sources(self) -> tuple[str, ...]:
        """The resolved paths of the packaged files the last deal named; a session never dealt
        names none."""
        if not self.dealt_files.exists():
            return ()
        return tuple(self.dealt_files.read_text(encoding="utf-8").splitlines())


@dataclass(frozen=True)
class ReadCall(GatedCall, declared_name="Read"):
    """A Read always runs; once it completes, a read covering a whole dealt file counts it."""

    def judge(self, comms: Comms) -> None:
        pass

    def completed(self, comms: Comms) -> int:
        read = ReadInput.decode(self.tool_input)
        path = read.file_path
        if str(path.resolve()) not in self.sources() or not read.covers(path):
            return super().completed(comms)
        with self.recorded_call() as call:
            self.announce(comms, call.read(self.dealt_read, path), call.number)
            return call.number


@dataclass(frozen=True)
class OtherCall(GatedCall, DefaultGate):
    """Any other tool waits for every dealt file to be read and the audit to answer the deal."""

    def judge(self, comms: Comms) -> None:
        self.gate(comms)


@dataclass(frozen=True)
class WriteCall(GatedCall, declared_name="Write"):
    """Writing the audit itself is the way through the gate; any other write waits like the rest."""

    def judge(self, comms: Comms) -> None:
        if WriteInput.decode(self.tool_input).file_path.resolve() != self.audit.resolve():
            self.gate(comms)


@dataclass(frozen=True)
class SubagentCall(GatedCall, SeatedSubagent):
    """A subagent bypasses its parent's read gate but reports its own tool activity."""

    def judge(self, comms: Comms) -> None:
        self.enter(ToolRunningPhase(self.tool_name))

    def completed(self, comms: Comms) -> int:
        """A subagent's event never increments its parent's deal record."""
        return 0
