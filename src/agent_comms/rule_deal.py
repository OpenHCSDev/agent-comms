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

import json
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord
from .pattern_sources import ReadThenRepeatedFailure
from .store_files import AtomicFile

if TYPE_CHECKING:
    from .commander.rule_file import RuleFile
    from .context_reading import ContextUsage


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
        return (
            self.offset <= 1
            and self.limit >= len(text.splitlines())
            and self.max_bytes >= len(text)
        )


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
        return "\n\n".join(
            (
                *(
                    f"Tristan told you, verbatim:\n{words}\nYou did not."
                    for words in self.owner_words[-1:]
                ),
                f"{failure.title}\n{failure.cite()}\n"
                "This session is repeating it, not observing another session's mistake.",
                f"Write this SessionAudit to {audit_path}. Fill only broke and doing; keep every"
                " rule name and quoted words unchanged. Empty broke means the rule was kept;"
                " doing must name your current action:\n"
                + json.dumps(FieldCodec.encode(audit), ensure_ascii=False, indent=2),
            )
        )

    def refusal(self, audit: SessionAudit) -> str:
        """Why this audit does not answer the deal, or nothing when it does: every dealt rule
        is audited, and every shown prompt is answered by its own words with an action."""
        missing = sorted(self.rules - {entry.rule for entry in audit.rules})
        if missing:
            return "the audit skips the dealt rules " + ", ".join(missing)
        if idle := [answer.words for answer in audit.owner_words if not answer.acts()]:
            return "the audit names no action for: " + " | ".join(idle)
        unanswered = Counter(self.owner_words) - Counter(
            answer.words for answer in audit.owner_words
        )
        if unanswered:
            return "the audit does not answer the owner's prompts: " + " | ".join(
                unanswered.elements()
            )
        return ""


class DealtSources(ABC):
    """Which packaged files a session is dealt and which rules its audit answers. The deck
    readers (agent_comms_hooks.skill.DeckReader) are the one list, for every backend."""

    @classmethod
    @abstractmethod
    def rules(cls) -> tuple[RuleFile, ...]: ...

    @classmethod
    @abstractmethod
    def sources(cls) -> tuple[Path, ...]:
        """The packaged files ordered read in full: the rules themselves, never a copy."""


class DealState(ABC):
    """Whether a session holds an open deal, and what that deal still requires."""

    @abstractmethod
    def sources(self) -> tuple[Path, ...]:
        """The resolved paths of the files dealt."""

    @abstractmethod
    def unread(self, read: Path) -> tuple[Path, ...]:
        """The dealt files the read log does not list."""

    @abstractmethod
    def audit_refusal(self, audit: Path) -> str:
        """Why the audit does not answer the deal, or nothing when it does."""

    @abstractmethod
    def reminders(self, audit: Path) -> tuple[str, ...]:
        """The audit template a refusal carries."""

    @abstractmethod
    def started(self, deal: Redeals) -> None:
        """What a session's start does to its deal."""

    @abstractmethod
    def context_observed(self, deal: Redeals, usage: ContextUsage) -> None:
        """What a report of the context's use does to the deal."""

    def answer(self) -> GateAnswer:
        """What the deal itself answers once its reads and audit are done."""
        return GateOpen()


class NoDeal(DealState):
    """A session never dealt: nothing to read and no audit owed."""

    def sources(self) -> tuple[Path, ...]:
        return ()

    def unread(self, read: Path) -> tuple[Path, ...]:
        return ()

    def audit_refusal(self, audit: Path) -> str:
        return ""

    def reminders(self, audit: Path) -> tuple[str, ...]:
        return ()

    def started(self, deal: Redeals) -> None:
        """A session never dealt is dealt at its first start."""
        deal.redeal()

    def context_observed(self, deal: Redeals, usage: ContextUsage) -> None:
        """No deal, nothing to refresh."""
        return


class WrittenDeal(DealState, DeclaredFamily):
    """A deal record written whole and atomically; its member says what was dealt."""


@dataclass(frozen=True)
class RulelessDeal(WrittenDeal, declared_name="no_rules"):
    """A start or compaction that had no rules to deal: every tool is refused, loudly, until a
    later start finds the rules its role follows."""

    reason: str

    def sources(self) -> tuple[Path, ...]:
        return ()

    def unread(self, read: Path) -> tuple[Path, ...]:
        return ()

    def audit_refusal(self, audit: Path) -> str:
        return ""

    def reminders(self, audit: Path) -> tuple[str, ...]:
        return ()

    def started(self, deal: Redeals) -> None:
        """Every start tries again."""
        deal.redeal()

    def context_observed(self, deal: Redeals, usage: ContextUsage) -> None:
        return

    def answer(self) -> GateAnswer:
        return GateRefusal("being dealt any rules", f"{self.reason}.")


@dataclass(frozen=True)
class DealRecord(WrittenDeal, declared_name="rules"):
    """One deal: the files dealt and what the audit must answer. The read log and the audit
    hang off it, and a new deal resets both before it is written."""

    dealt: tuple[Path, ...]
    due: AuditDue

    def sources(self) -> tuple[Path, ...]:
        return self.dealt

    def unread(self, read: Path) -> tuple[Path, ...]:
        done = frozenset(Path(line) for line in read.read_text(encoding="utf-8").splitlines())
        return tuple(source for source in self.dealt if source not in done)

    def audit_refusal(self, audit: Path) -> str:
        if not audit.exists():
            return "no audit is written"
        try:
            written = FieldCodec.decode(SessionAudit, FieldCodec.parse(audit.read_bytes()))
        except MalformedRecord as error:
            return f"the audit does not decode: {error}"
        return self.due.refusal(written)

    def reminders(self, audit: Path) -> tuple[str, ...]:
        return (self.due.reminder(audit),)

    def started(self, deal: Redeals) -> None:
        """A restart keeps the context it had, and the deal it still answers."""
        return

    def context_observed(self, deal: Redeals, usage: ContextUsage) -> None:
        deal.refresh_due(usage)


class RuleDeal(ABC):
    """One session's deal: the files beside its `stem` that record what it was dealt, what it
    has read in full and the audit it wrote."""

    owner_prompts: ClassVar[int] = 6
    """How many of the person's own last prompts a deal puts back in front of the session."""

    @property
    @abstractmethod
    def stem(self) -> Path:
        """Where this session's deal files live; each takes its own suffix."""

    @property
    def record(self) -> Path:
        return self.stem.with_suffix(".deal.json")

    @property
    def calls(self) -> Path:
        """One byte per gated call since this deal; atomic append counts parallel calls once
        without another counter or history scan."""
        return self.stem.with_suffix(".calls")

    @property
    def dealt_read(self) -> Path:
        """The dealt files read in full since the last deal, one path per line."""
        return self.stem.with_suffix(".dealt-read")

    @property
    def audit(self) -> Path:
        """The session's audit of the dealt rules and the owner's words, written after reading."""
        return self.stem.with_suffix(".audit.json")

    def count_call(self) -> int:
        with self.calls.open("ab", buffering=0) as calls:
            calls.write(b".")
            return calls.tell()

    def current(self) -> DealState:
        """The deal this session holds; the record is its one declaration."""
        if not self.record.exists():
            return NoDeal()
        return FieldCodec.decode(WrittenDeal, FieldCodec.parse(self.record.read_bytes()))

    def deal(self, dealt: type[DealtSources], prompts: tuple[str, ...]) -> None:
        """A start or compaction drops what was read: the read log and the audit reset first,
        then the record is published whole, so no reader sees a half-written deal."""
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        self.calls.unlink(missing_ok=True)
        self.dealt_read.write_text("", encoding="utf-8")
        self.audit.unlink(missing_ok=True)
        self.publish(
            DealRecord(
                tuple(source.resolve() for source in dealt.sources()),
                AuditDue(frozenset(rule.name for rule in dealt.rules()), prompts),
            )
        )

    def deal_none(self, reason: str) -> None:
        """Nothing to deal is a deal too: it refuses every tool and says why."""
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        self.publish(RulelessDeal(reason))

    def publish(self, record: WrittenDeal) -> None:
        AtomicFile(self.record).write_bytes(
            json.dumps(FieldCodec.encode(record, WrittenDeal)).encode()
        )

    def record_read(self, path: Path, span: ReadSpan) -> None:
        """A completed read covering a whole dealt file counts it; any other read counts nothing."""
        if path not in self.current().sources() or not span.covers(path):
            return
        with self.dealt_read.open("a", encoding="utf-8") as record:
            record.write(f"{path}\n")


class GateAnswer(ABC):
    """What the gate answers one tool call."""

    @abstractmethod
    def enforce(self, gate: ToolGate, tool: str) -> None: ...


class GateOpen(GateAnswer):
    """Every composed requirement is met: the call runs."""

    def enforce(self, gate: ToolGate, tool: str) -> None:
        return


@dataclass(frozen=True)
class GateRefusal(GateAnswer):
    """One requirement the deal still holds open: what the session skipped, and why."""

    before: str
    reason: str

    def enforce(self, gate: ToolGate, tool: str) -> None:
        gate.refuse(
            "\n\n".join(
                (
                    f"You just tried {tool} before {self.before}. Call #{gate.count_call()}"
                    " since this compaction, refused.",
                    self.reason,
                    *gate.current().reminders(gate.audit),
                )
            )
        )


class ToolGate(RuleDeal):
    """A deal that judges each tool call before it runs. Each requirement is a parent that
    refuses or hands on to the next by the MRO; this base requires nothing."""

    def blocked(self, deal: DealState) -> GateAnswer:
        """The first requirement still open, or what the deal itself answers."""
        return deal.answer()

    def judge_tool(self, tool: str) -> None:
        """Refuse this call while a requirement is open, or let it run."""
        self.blocked(self.current()).enforce(self, tool)

    def judge_read(self, tool: str, path: Path) -> None:
        """Reading a dealt file is the way through the gate; any other read waits."""
        if path not in self.current().sources():
            self.judge_tool(tool)

    def judge_write(self, tool: str, path: Path) -> None:
        """Writing the audit itself is the way through the gate; any other write waits."""
        if path != self.audit.resolve():
            self.judge_tool(tool)

    @abstractmethod
    def refuse(self, reason: str) -> None:
        """Deliver this refusal to the model the way the backend refuses a tool call."""


class ReadsDealtSources(ToolGate):
    """Every dealt file is read in full before anything else runs."""

    def blocked(self, deal: DealState) -> GateAnswer:
        if unread := deal.unread(self.dealt_read):
            return GateRefusal(
                "reading",
                "Read the rules you were dealt first, each in full with your read tool (no"
                " offset or limit): "
                + ", ".join(str(source) for source in unread)
                + ". Your summary is a paraphrase of them, not a reading.",
            )
        return super().blocked(deal)


class AuditsDealtRules(ToolGate):
    """After reading, the session writes an audit answering every dealt rule and prompt."""

    def blocked(self, deal: DealState) -> GateAnswer:
        if refusal := deal.audit_refusal(self.audit):
            return GateRefusal("writing the required audit", f"Reading is passive; {refusal}.")
        return super().blocked(deal)


class DealtRuleGate(ReadsDealtSources, AuditsDealtRules):
    """Read every dealt file, then write the audit: the gate every backend runs today."""


@dataclass(frozen=True)
class RefreshAt:
    """Deal the rules again, once per compaction, when the context left falls to `remaining`
    of the window the backend itself reports."""

    remaining: float

    def due(self, usage: ContextUsage) -> bool:
        return usage.used >= (1 - self.remaining) * usage.size

    @property
    def key(self) -> str:
        return f"{self.remaining}"


class Redeals(RuleDeal):
    """A deal its backend renews itself: at the first start, after each compaction, and at
    each declared share of context left, read from the backend's own context-use report."""

    refreshes: ClassVar[tuple[RefreshAt, ...]] = (RefreshAt(0.66), RefreshAt(0.33))

    @property
    def refreshed(self) -> Path:
        """The refreshes already dealt since the last compaction, one key per line."""
        return self.stem.with_suffix(".refreshed")

    @abstractmethod
    def renew(self) -> None:
        """Deal this session's sources and its owner's latest words again."""

    def redeal(self) -> None:
        """A start or compaction arms every refresh again, then deals."""
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        self.refreshed.write_text("", encoding="utf-8")
        self.renew()

    def start(self) -> None:
        """A spawned session is dealt before its first tool call; a restart keeps its deal."""
        self.current().started(self)

    def observe_context(self, usage: ContextUsage) -> None:
        self.current().context_observed(self, usage)

    def refresh_due(self, usage: ContextUsage) -> None:
        """Every refresh the context passed that this compaction has not dealt yet deals once."""
        done = frozenset(self.refreshed.read_text(encoding="utf-8").split())
        if due := [
            refresh.key
            for refresh in self.refreshes
            if refresh.due(usage) and refresh.key not in done
        ]:
            with self.refreshed.open("a", encoding="utf-8") as record:
                record.write("".join(f"{key}\n" for key in due))
            self.renew()
