"""After a start or compaction a session is dealt the packaged sources it follows, and every tool
but reading them and writing the audit that answers them is refused until it has done both.

The order to read the rules is text, and text loses to a compaction summary: the model holds its
own paraphrase of the rules and treats the order as optional. The gate makes the order the only
path forward, whatever runs the agent. `RuleDeal` owns where one session's deal lives and what it
requires; the requirements are capability mixins composed by the MRO, each refusing in turn
before handing on to the next, so a backend that wants a different gate composes different
parents. A backend leaf supplies only where its deal lives and how a refusal reaches its model:
the Claude Code hooks deny a PreToolUse event, Core's Pi tool owner raises a tool denial.
"""

from __future__ import annotations

import inspect
import json
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .commander.package import CommanderPackage
from .commander.rule_file import CommanderAudience, FleetAudience, RuleAudience, RuleFile
from .field_codec import FieldCodec, MalformedRecord
from .pattern_sources import PostmortemFailure, ReadThenRepeatedFailure
from .slop import Slop

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .threads import Thread


@dataclass(frozen=True)
class ReadSpan:
    """The part of a file one read returned: from line `offset`, at most `limit` lines and
    `max_bytes` bytes. Each backend's read tool declares its own defaults."""

    offset: int
    limit: int
    max_bytes: int

    def covers(self, source: Path) -> bool:
        """Whether this read returned every line of the file."""
        text = source.read_bytes()
        return (self.offset <= 1 and self.limit >= len(text.splitlines())
                and self.max_bytes >= len(text))


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
        """Render the required answers from their declarations, not another JSON schema."""
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


class DealtSources(ABC):
    """Which packaged files a session is dealt, by the audiences its rules declare.

    Every bound session follows the fleet rules; the thread the registry names commander also
    follows the commander's. RuleFile's audience is the one owner of that split, and a member's
    audiences are its bases' plus its own declaration.
    """

    audiences: ClassVar[frozenset[type[RuleAudience]]] = frozenset()

    def __init_subclass__(cls, audience: tuple[type[RuleAudience], ...] = (),
                          **options: object) -> None:
        super().__init_subclass__(**options)
        cls.audiences = frozenset(audience).union(*(base.audiences for base in cls.__bases__))

    @classmethod
    def rules(cls) -> tuple[RuleFile, ...]:
        return tuple(rule for rule in RuleFile.packaged() if rule.audience in cls.audiences)

    @classmethod
    @abstractmethod
    def sources(cls) -> tuple[Path, ...]:
        """The packaged files this session is ordered to read in full after a start or
        compaction: the rules themselves, never a copy of them."""

    @staticmethod
    def for_thread(snapshot: RegistrySnapshot, thread: Thread) -> DealtSources:
        """The commander's sources for the thread the registry names commander, the fleet's for
        any other."""
        commanding = any(commander.incarnation == thread.incarnation
                         for commander in snapshot.commander_project.commanders(snapshot))
        return CommanderSources() if commanding else FleetSources()


class FleetSources(DealtSources, audience=(FleetAudience,)):
    """A bound session the registry does not name commander: the fleet rules, the voice they
    are said in and the failures already caught."""

    @classmethod
    def sources(cls) -> tuple[Path, ...]:
        return (
            CommanderPackage.voice(),
            Path(inspect.getfile(PostmortemFailure)),
            *(rule.path for rule in cls.rules()),
        )


class CommanderSources(FleetSources, audience=(CommanderAudience,)):
    """The commander: the fleet's sources plus the indictment, the commander skill, the
    commander's own rules and the slop catalog's index."""

    @classmethod
    def sources(cls) -> tuple[Path, ...]:
        return (
            CommanderPackage.voice(),
            CommanderPackage.indictment(),
            Path(inspect.getfile(PostmortemFailure)),
            CommanderPackage.skill(),
            *(rule.path for rule in cls.rules()),
            Slop.catalog() / "README.md",
        )


class RuleDeal(ABC):
    """One session's deal: the files beside its `stem` that record what it was dealt, what it
    has read in full, what its audit must answer, and the audit itself."""

    @property
    @abstractmethod
    def stem(self) -> Path:
        """Where this session's deal files live; each takes its own suffix."""

    @property
    def calls(self) -> Path:
        """One byte per gated call since this deal; atomic append counts parallel calls once
        without another counter or history scan."""
        return self.stem.with_suffix(".calls")

    @property
    def dealt_files(self) -> Path:
        """The packaged files dealt, one resolved path per line. It names the files themselves:
        a copy of a rule is a paraphrase the session reads instead of the rule."""
        return self.stem.with_suffix(".dealt")

    @property
    def dealt_read(self) -> Path:
        """The dealt files read in full since the last deal, one path per line."""
        return self.stem.with_suffix(".dealt-read")

    @property
    def audit(self) -> Path:
        """The session's audit of the dealt rules and the owner's words, written after reading."""
        return self.stem.with_suffix(".audit.json")

    @property
    def audit_due(self) -> Path:
        """What the audit must answer: every dealt rule and each owner prompt shown."""
        return self.stem.with_suffix(".audit-due.json")

    def count_call(self) -> int:
        with self.calls.open("ab", buffering=0) as calls:
            calls.write(b".")
            return calls.tell()

    def deal(self, dealt: DealtSources, prompts: tuple[str, ...]) -> None:
        """A start or compaction drops what was read: no file counts as read until it is read
        again, and the audit is owed again."""
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        self.calls.unlink(missing_ok=True)
        self.dealt_read.write_text("", encoding="utf-8")
        self.audit.unlink(missing_ok=True)
        self.audit_due.write_text(json.dumps({
            "rules": [rule.name for rule in dealt.rules()],
            "owner_words": list(prompts),
        }), encoding="utf-8")
        self.dealt_files.write_text(
            "".join(f"{source.resolve()}\n" for source in dealt.sources()), encoding="utf-8")

    def sources(self) -> tuple[str, ...]:
        """The resolved paths the last deal named; a session never dealt names none."""
        if not self.dealt_files.exists():
            return ()
        return tuple(self.dealt_files.read_text(encoding="utf-8").splitlines())

    def unread(self) -> tuple[Path, ...]:
        """The dealt files not yet read in full since the last deal. A deal writes the read
        record, so a session with no record was dealt nothing to read."""
        if not self.dealt_read.exists():
            return ()
        read = set(self.dealt_read.read_text(encoding="utf-8").splitlines())
        return tuple(Path(source) for source in self.sources() if source not in read)

    def record_read(self, path: Path, span: ReadSpan) -> None:
        """A completed read covering a whole dealt file counts it; any other read counts nothing."""
        if str(path) not in self.sources() or not span.covers(path):
            return
        with self.dealt_read.open("a", encoding="utf-8") as record:
            record.write(f"{path}\n")

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


@dataclass(frozen=True)
class GateRefusal:
    """One requirement the deal still holds open: what the session skipped, and why."""

    before: str
    reason: str

    def render(self, gate: ToolGate, tool: str) -> str:
        return "\n\n".join((
            f"You just tried {tool} before {self.before}. Call #{gate.count_call()}"
            " since this compaction, refused.", self.reason,
            *(due.reminder(gate.audit) for due in gate.required_audits()),
        ))


class ToolGate(RuleDeal):
    """A deal that judges each tool call before it runs. Each requirement is a parent that
    refuses or hands on to the next by the MRO; this base requires nothing."""

    def blocked(self) -> tuple[GateRefusal, ...]:
        """The first requirement still open; empty once every composed requirement is met."""
        return ()

    def judge_tool(self, tool: str) -> None:
        """Refuse this call while a requirement is open, or let it run."""
        for refusal in self.blocked():
            self.refuse(refusal.render(self, tool))

    def judge_write(self, tool: str, path: Path) -> None:
        """Writing the audit itself is the way through the gate; any other write waits."""
        if path != self.audit.resolve():
            self.judge_tool(tool)

    @abstractmethod
    def refuse(self, reason: str) -> None:
        """Deliver this refusal to the model the way the backend refuses a tool call."""


class ReadsDealtSources(ToolGate):
    """Every dealt file is read in full before anything else runs."""

    def blocked(self) -> tuple[GateRefusal, ...]:
        if unread := self.unread():
            return (GateRefusal(
                "reading",
                "Read the rules you were dealt first, each in full with your read tool (no"
                " offset or limit): " + ", ".join(str(source) for source in unread)
                + ". Your summary is a paraphrase of them, not a reading."),)
        return super().blocked()


class AuditsDealtRules(ToolGate):
    """After reading, the session writes an audit answering every dealt rule and prompt."""

    def blocked(self) -> tuple[GateRefusal, ...]:
        for due in self.required_audits():
            if refusal := self.unaudited(due):
                return (GateRefusal("writing the required audit",
                                    f"Reading is passive; {refusal}."),)
        return super().blocked()


class DealtRuleGate(ReadsDealtSources, AuditsDealtRules):
    """Read every dealt file, then write the audit: the gate every backend runs today."""
