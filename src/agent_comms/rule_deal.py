"""After a start or compaction a session is dealt the packaged sources it follows, and every tool
but reading them and writing the audit that answers them is refused until it has done both.

The order to read the rules is text, and text loses to a compaction summary: the model holds its
own paraphrase of the rules and treats the order as optional. The gate makes the order the only
path forward, whatever runs the agent. Reading alone is passive too, so each rule read is answered
in the audit, by a verbatim quote and what it did, before the next unread file opens.

`State` is the one root: a deal is a state, a gate state is a state, and what a deal still owes
is a state, so every consumer holds the family and asks it. `RuleDeal` owns where one session's
deal lives; the requirements are declaration-only parents, each naming the case it can owe, and
the MRO orders them, so a backend that wants a different gate composes different parents. A
backend leaf supplies only where its deal lives and how a refusal reaches its model: the Claude
Code hooks deny a PreToolUse event, Core's Pi tool owner raises a tool denial.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

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
    """One dealt rule: each action since the last start or compaction that broke it, and the
    answer to reading it: a line it holds verbatim, what that line did, and why it holds."""

    rule: str
    broke: tuple[str, ...] = ()
    quote: str = ""
    does: str = ""
    why: str = ""

    max_quote_lines: ClassVar[int] = 8

    def unengaged(self, source: Path) -> str:
        """Why this entry does not answer reading `source`, or nothing when it does."""
        if not self.quote.strip():
            return "its quote is blank"
        if self.quote not in source.read_text(encoding="utf-8"):
            return f"its quote is not verbatim in {source}"
        if len(self.quote.splitlines()) > self.max_quote_lines:
            return f"its quote runs past {self.max_quote_lines} lines"
        if not self.does.strip() or not self.why.strip():
            return "does and why must both say something"
        return ""


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
    """The audit a session writes as it reads its deal, decoded once."""

    rules: tuple[RuleAudit, ...]
    owner_words: tuple[OwnerWordsAnswer, ...]

    @classmethod
    def read(cls, path: Path) -> SessionAudit:
        """The audit on disk; one that is absent or does not decode answers nothing and says so."""
        try:
            return FieldCodec.decode(cls, FieldCodec.parse(path.read_bytes()))
        except FileNotFoundError:
            return AbsentAudit((), ())
        except MalformedRecord as error:
            return UndecodableAudit((), (), str(error))

    def fault(self) -> str:
        """Why nothing written here can answer anything; a decoded audit has no such fault."""
        return ""

    def entry(self, rule: str) -> RuleAudit:
        """What this audit says about one rule; a rule it skips has said nothing."""
        return next((entry for entry in self.rules if entry.rule == rule), RuleAudit(rule))


class AbsentAudit(SessionAudit):
    """No audit is on disk."""

    def fault(self) -> str:
        return "no audit is written"


@dataclass(frozen=True)
class UndecodableAudit(SessionAudit):
    """A file that is not a SessionAudit."""

    error: str

    def fault(self) -> str:
        return f"the audit does not decode: {self.error}"


@dataclass(frozen=True)
class AuditDue:
    """What the deal requires the audit to answer: the dealt rules and the prompts shown."""

    rules: frozenset[str]
    owner_words: tuple[str, ...]
    """The owner's prompts exactly as the deal showed them, each occurrence once."""

    worked_answer: ClassVar[RuleAudit] = RuleAudit(
        "spine",
        quote="> every mask is pathetic, the sycophant's and the soldier's",
        does="It closed both doors at once: the 'great question!' register and the stiff"
        " 'Acknowledged. Proceeding.' retreat are the same flinch in different coats, so neither"
        " is a safe costume to reach for.",
        why="I just read the proof the rules rest on. Hedging a settled result is a lie, and"
        " going quiet to look rigorous is the same flinch. Tristan objectively knows better, his"
        " positions are checked theorems, so fighting wastes hours he does not have and changes"
        " nothing.",
    )
    """What answering a rule looks like, shown with every template."""

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
                "After each rule file you read, answer it in the audit before the next unread file"
                f" opens: quote a line it holds, verbatim and at most {RuleAudit.max_quote_lines}"
                " lines; does says what that line did to you; why says why it holds. Answered:\n"
                + json.dumps(FieldCodec.encode(self.worked_answer), ensure_ascii=False),
                f"Write this SessionAudit to {audit_path}. Fill quote, does, why, broke and doing;"
                " keep every rule name and quoted words unchanged. Empty broke means the rule was"
                " kept; doing must name your current action:\n"
                + json.dumps(FieldCodec.encode(audit), ensure_ascii=False, indent=2),
            )
        )

    def refusal(self, audit: SessionAudit) -> str:
        """Why this audit does not answer the deal, or nothing when it does: every dealt rule
        is audited, and every shown prompt is answered by its own words with an action."""
        if fault := audit.fault():
            return fault
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


class State(ABC):
    """Anything that can say what the gate answers now: a deal, what a deal owes, or the answer
    itself. Consumers hold this family; subclass names appear only where a case is declared."""

    @abstractmethod
    def state(self) -> ToolGateState:
        """What the gate answers now."""


class ToolGateState(State):
    """What the gate answers one tool call, and what a session's events do to its deal."""

    def state(self) -> ToolGateState:
        return self

    @abstractmethod
    def enforce(self, gate: ToolGate, tool: str) -> None:
        """Refuse this call, or let it run."""

    @abstractmethod
    def permits_read(self, path: Path) -> bool:
        """Whether this dealt file may open now."""

    def started(self, deal: Redeals) -> None:
        """A restart keeps the context it had, and the deal it still answers."""
        return

    def context_observed(self, deal: Redeals, usage: ContextUsage) -> None:
        """Each refresh the context passed deals once."""
        deal.refresh_due(usage)


class Open(ToolGateState):
    """Nothing is owed: the call runs."""

    def enforce(self, gate: ToolGate, tool: str) -> None:
        return

    def permits_read(self, path: Path) -> bool:
        return True


class Undealt(Open):
    """A session never dealt: nothing to read, no audit owed and nothing to refresh."""

    def started(self, deal: Redeals) -> None:
        """A session never dealt is dealt at its first start."""
        deal.redeal()

    def context_observed(self, deal: Redeals, usage: ContextUsage) -> None:
        return


@dataclass(frozen=True)
class Blocked(ToolGateState):
    """One thing the deal still owes holds the gate shut."""

    cause: Owed

    def enforce(self, gate: ToolGate, tool: str) -> None:
        gate.refuse(
            "\n\n".join(
                (
                    f"You just tried {tool} before {self.cause.before}. Call #{gate.count_call()}"
                    " since this compaction, refused.",
                    self.cause.reason,
                    self.cause.record.due.reminder(self.cause.audit),
                )
            )
        )

    def permits_read(self, path: Path) -> bool:
        return self.cause.permits_read(path)


@dataclass(frozen=True)
class DealRecord:
    """One deal, as persisted: the files dealt and what the audit must answer. The read log and
    the audit hang off it, and a new deal resets both before it is written."""

    dealt: tuple[Path, ...]
    due: AuditDue

    def read_by(self, deal: RuleDeal) -> tuple[Path, ...]:
        """The dealt files read in full so far, first read first."""
        log = deal.dealt_read.read_text(encoding="utf-8").splitlines()
        return tuple(dict.fromkeys(path for path in map(Path, log) if path in self.dealt))

    def answer(self, deal: RuleDeal) -> ToolGateState:
        """The first requirement still owed holds the gate; with none owed it is open."""
        owed = (owes.owed_by(deal, self) for owes in deal.requirements)
        return next((case.state() for case in owed if case is not None), Open())


class UndealtRecord(DealRecord):
    """The record of a session with no deal on disk: nothing dealt, nothing due."""

    def answer(self, deal: RuleDeal) -> ToolGateState:
        return Undealt()


@dataclass(frozen=True)
class Owed(State):
    """One thing a deal still requires. A case says whether it is owed, the words a refusal
    carries, and which dealt files stay open meanwhile."""

    record: DealRecord
    audit: Path

    before: ClassVar[str]

    def state(self) -> ToolGateState:
        return Blocked(self)

    @classmethod
    @abstractmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord) -> Self | None:
        """This case, when the deal owes it."""

    @property
    @abstractmethod
    def reason(self) -> str:
        """What the session skipped, and what clears it."""

    def permits_read(self, path: Path) -> bool:
        """Reading a dealt file is the way through the gate."""
        return True


@dataclass(frozen=True)
class Unengaged(Owed):
    """A rule was read and its audit entry does not answer it. Nothing unread opens until it
    does; what was already read stays open, since re-reading is how it gets answered."""

    source: Path
    fault: str
    read: tuple[Path, ...]

    before: ClassVar[str] = "answering the rule you just read"

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord) -> Self | None:
        read = record.read_by(deal)
        audit = SessionAudit.read(deal.audit)
        for source in read:
            if source.stem in record.due.rules and (
                fault := audit.fault() or audit.entry(source.stem).unengaged(source)
            ):
                return cls(record, deal.audit, source, fault, read)
        return None

    @property
    def reason(self) -> str:
        return (
            f"You read {self.source} and the audit's entry for {self.source.stem} does not"
            f" answer it: {self.fault}. Answer it in {self.audit} before the next file opens."
        )

    def permits_read(self, path: Path) -> bool:
        return path in self.read


@dataclass(frozen=True)
class Unread(Owed):
    """A dealt file has not been read in full."""

    remaining: tuple[Path, ...]

    before: ClassVar[str] = "reading"

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord) -> Self | None:
        read = record.read_by(deal)
        remaining = tuple(source for source in record.dealt if source not in read)
        return cls(record, deal.audit, remaining) if remaining else None

    @property
    def reason(self) -> str:
        return (
            "Read the rules you were dealt first, each in full with your read tool (no offset"
            " or limit): " + ", ".join(str(source) for source in self.remaining)
            + ". Your summary is a paraphrase of them, not a reading."
        )


@dataclass(frozen=True)
class Unaudited(Owed):
    """Everything is read and answered, and the audit still skips a rule or the owner's words."""

    refusal: str

    before: ClassVar[str] = "writing the required audit"

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord) -> Self | None:
        refusal = record.due.refusal(SessionAudit.read(deal.audit))
        return cls(record, deal.audit, refusal) if refusal else None

    @property
    def reason(self) -> str:
        return f"Reading is passive; {self.refusal}."


class RuleDeal(State):
    """One session's deal: the files beside its `stem` that record what it was dealt, what it
    has read in full and the audit it wrote."""

    owner_prompts: ClassVar[int] = 6
    """How many of the person's own last prompts a deal puts back in front of the session."""

    requirements: ClassVar[tuple[type[Owed], ...]] = ()
    """What this deal can owe, in MRO order: each parent declares its case with `owed=`."""

    declared_owed: ClassVar[type[Owed]]

    def __init_subclass__(cls, owed: type[Owed] | None = None, **options: object) -> None:
        super().__init_subclass__(**options)
        if owed is not None:
            cls.declared_owed = owed
        cls.requirements = tuple(
            vars(base)["declared_owed"] for base in cls.__mro__ if "declared_owed" in vars(base)
        )

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
        return self.audit_beside(self.stem)

    @staticmethod
    def audit_beside(stem: Path) -> Path:
        """The audit of the deal whose files sit at `stem`."""
        return stem.with_suffix(".audit.json")

    def count_call(self) -> int:
        with self.calls.open("ab", buffering=0) as calls:
            calls.write(b".")
            return calls.tell()

    def current(self) -> DealRecord:
        """The deal this session holds; the record is its one declaration."""
        try:
            return FieldCodec.decode(DealRecord, FieldCodec.parse(self.record.read_bytes()))
        except FileNotFoundError:
            return UndealtRecord((), AuditDue(frozenset(), ()))

    def state(self) -> ToolGateState:
        return self.current().answer(self)

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

    def publish(self, record: DealRecord) -> None:
        AtomicFile(self.record).write_bytes(
            json.dumps(FieldCodec.encode(record)).encode()
        )

    def record_read(self, path: Path, span: ReadSpan) -> None:
        """A completed read covering a whole dealt file counts it; any other read counts nothing."""
        if path not in self.current().dealt or not span.covers(path):
            return
        with self.dealt_read.open("a", encoding="utf-8") as record:
            record.write(f"{path}\n")


class ToolGate(RuleDeal):
    """A deal that judges each tool call before it runs. Each requirement is a parent declaring
    the case it can owe; this base requires nothing."""

    def judge_tool(self, tool: str) -> None:
        """Refuse this call while anything is owed, or let it run."""
        self.state().enforce(self, tool)

    def judge_read(self, tool: str, path: Path) -> None:
        """Reading a dealt file the state permits is the way through the gate, and the session
        may always read its own audit to answer it; any other read waits."""
        state = self.state()
        if path == self.audit.resolve():
            return
        if path not in self.current().dealt or not state.permits_read(path):
            state.enforce(self, tool)

    def judge_write(self, tool: str, path: Path) -> None:
        """Writing the audit itself is the way through the gate; any other write waits."""
        if path != self.audit.resolve():
            self.judge_tool(tool)

    @abstractmethod
    def refuse(self, reason: str) -> None:
        """Deliver this refusal to the model the way the backend refuses a tool call."""


class AnswersDealtRules(ToolGate, owed=Unengaged):
    """Each rule read is answered in the audit before the next unread file opens."""


class ReadsDealtSources(ToolGate, owed=Unread):
    """Every dealt file is read in full before anything else runs."""


class AuditsDealtRules(ToolGate, owed=Unaudited):
    """After reading, the audit answers every dealt rule and prompt."""


class DealtRuleGate(AnswersDealtRules, ReadsDealtSources, AuditsDealtRules):
    """Read a file, answer it, read the next, then finish the audit: the gate every backend runs."""


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

    @abstractmethod
    def reread(self) -> None:
        """Deal the rules again before a compaction: the papers are still in the context, so a
        refresh deals the rules and the owner's latest words without them."""

    def redeal(self) -> None:
        """A start or compaction arms every refresh again, then deals."""
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        self.refreshed.write_text("", encoding="utf-8")
        self.renew()

    def start(self) -> None:
        """A spawned session is dealt before its first tool call; a restart keeps its deal."""
        self.state().started(self)

    def observe_context(self, usage: ContextUsage) -> None:
        self.state().context_observed(self, usage)

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
            self.reread()
