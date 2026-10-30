"""After a start or compaction a session is dealt the packaged sources it follows, and every tool
but reading them and writing the audit that answers them is refused until it has done both.

The order to read the rules is text, and text loses to a compaction summary: the model holds its
own paraphrase of the rules and treats the order as optional. The gate makes the order the only
path forward, whatever runs the agent. Reading alone is passive too, so each rule read is answered
in the audit, by a verbatim greentext quote and what it did, before the next unread file opens.

`State` is the one root: a deal is a state, a gate state is a state, and what a deal still owes
is a state, so every consumer holds the family and asks it. `RuleDeal` owns where one session's
deal lives; the requirements are declaration-only parents, each naming the case it can owe, and
the MRO orders them, so a backend that wants a different gate composes different parents. A
backend leaf supplies only where its deal lives and how a refusal reaches its model: the Claude
Code hooks deny a PreToolUse event, Core's Pi tool owner raises a tool denial.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar, Self

from .field_codec import FieldCodec, MalformedRecord
from .pattern_sources import ReadThenRepeatedFailure
from .locked_store import DocumentChange, LockedStore
from .store_files import AtomicFile, DependentStateTier, SingleFileStore

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
class DealtSource:
    """A dealt file by what it is, never by where an install put it: its package and its path
    inside that package. Every install moves the runtime, so a read keyed by the absolute path
    was a read of a place, and each identical install demanded every file again."""

    package: str
    relative: str

    @classmethod
    def of(cls, path: Path) -> DealtSource:
        """The installed package file this path names, under this runtime or an earlier one."""
        for index, name in enumerate(path.parts[:-1]):
            source = cls(name, PurePosixPath(*path.parts[index + 1:]).as_posix())
            if name.isidentifier() and source.installed():
                return source
        raise ValueError(f"{path} names no file of an installed package")

    def installed(self) -> bool:
        spec = find_spec(self.package)
        return bool(spec and spec.submodule_search_locations) and self.path.is_file()

    @property
    def path(self) -> Path:
        """Where the running install holds this file."""
        spec = find_spec(self.package)
        assert spec is not None and spec.submodule_search_locations is not None
        return Path(spec.submodule_search_locations[0]) / self.relative

    def digest(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class OpenedReads:
    """The dealt files the gate let a read open since context was last lost, in order. A
    parallel batch's reads are each judged before any one completes, so the gate counts what
    it opened, not only what finished."""

    sources: tuple[DealtSource, ...] = ()

    def opening(self, path: Path) -> OpenedReads:
        return OpenedReads((*self.sources, DealtSource.of(path)))


@dataclass(frozen=True, slots=True)
class OpenedReadsStore(SingleFileStore, LockedStore[OpenedReads], DependentStateTier):
    """One session's opened reads beside its deal. Each read is judged and recorded in one
    change, so concurrent hooks see every read opened before theirs."""

    record_type = OpenedReads

    def empty(self) -> OpenedReads:
        return OpenedReads()


@dataclass(frozen=True)
class DealtRead:
    """One dealt file read in full: the source and the content that was read. It still counts
    under any runtime holding that content, and stops counting the moment the file changes."""

    source: DealtSource
    digest: str

    def current(self) -> bool:
        return self.digest == self.source.digest()


@dataclass(frozen=True)
class RuleAudit:
    """One dealt rule: each action since the last start or compaction that broke it, and the
    answer to reading it: greentext lines it holds verbatim, what they did, and why it holds."""

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
        text = source.read_text(encoding="utf-8")
        if self.quote not in text:
            return f"its quote is not verbatim in {source}"
        greentext = {line for line in text.splitlines() if line.startswith("> ")}
        if not greentext.issuperset(self.quote.splitlines()):
            return f"its quote must be whole greentext lines of {source}: every line starts with >"
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
                f" opens: quote its greentext, whole lines verbatim and at most {RuleAudit.max_quote_lines}"
                " lines; does says what those lines did to you; why says why it holds. Answered:\n"
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

    def context_observed(self, deal: Redeals, usage: ContextUsage) -> None:
        """A refresh passed while the deal is being answered is answered by it: the context
        filled with the very files being read. Dealing them again once the gate opened made
        frame-baseline and install-speed reread 16 files the call after finishing 25 (bus 45261,
        45247)."""
        deal.refresh_answered(usage)


@dataclass(frozen=True)
class DealRecord:
    """One deal, as persisted: the files dealt and what the audit must answer. The read log and
    the audit hang off it; a deal that follows lost context empties the read log and deletes
    the audit, so every rule is answered again by the instance that just read it."""

    dealt: tuple[Path, ...]
    due: AuditDue

    def sources(self) -> tuple[DealtSource, ...]:
        return tuple(DealtSource.of(path) for path in self.dealt)

    @property
    def paths(self) -> tuple[Path, ...]:
        """The dealt files where the running install holds them."""
        return tuple(source.path for source in self.sources())

    def read_by(self, deal: RuleDeal) -> tuple[Path, ...]:
        """The dealt files whose current content was read in full, first read first."""
        sources = frozenset(self.sources())
        return tuple(dict.fromkeys(
            read.source.path for read in deal.reads() if read.source in sources and read.current()
        ))

    def opened_by(self, deal: RuleDeal, opened: OpenedReads) -> tuple[Path, ...]:
        """The dealt files read in full, then those whose read the gate let open and that have
        not finished: a parallel batch's reads are each judged before any one is recorded, so a
        read in flight counts, or every rule in the batch opens unanswered."""
        sources = frozenset(self.sources())
        return tuple(dict.fromkeys((
            *self.read_by(deal),
            *(source.path for source in opened.sources if source in sources),
        )))

    def widen[Said](self, deal: RuleDeal, dealt: type[DealtSources],
                    full: Callable[[], Said], kept: Said) -> Said:
        """A session whose role now follows more sources is dealt only those: what it read and
        wrote stays, since its context was never lost."""
        deal.publish(DealRecord(
            tuple(dict.fromkeys((*self.paths, *(source.resolve() for source in dealt.sources())))),
            AuditDue(self.due.rules | {rule.name for rule in dealt.rules()}, self.due.owner_words),
        ))
        return kept

    def resume[Said](self, deal: Callable[[], Said], kept: Said) -> Said:
        """A resume keeps the context it had, and the deal it still answers: its reads and
        audit stay."""
        return kept

    def answer(self, deal: RuleDeal, opened: OpenedReads) -> ToolGateState:
        """The first requirement still owed holds the gate; with none owed it is open."""
        owed = (case for owes in deal.requirements for case in owes.owed_by(deal, self, opened))
        return next(owed, Open()).state()


class UndealtRecord(DealRecord):
    """The record of a session with no deal on disk: nothing dealt, nothing due."""

    def resume[Said](self, deal: Callable[[], Said], kept: Said) -> Said:
        """A session never dealt is dealt at its first start, whatever started it."""
        return deal()

    @property
    def paths(self) -> tuple[Path, ...]:
        return ()

    def widen[Said](self, deal: RuleDeal, dealt: type[DealtSources],
                    full: Callable[[], Said], kept: Said) -> Said:
        """A session bound after it started is dealt in full."""
        return full()

    def answer(self, deal: RuleDeal, opened: OpenedReads) -> ToolGateState:
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
    def owed_by(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> Iterator[Self]:
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

    faults: tuple[tuple[Path, str], ...]
    read: tuple[Path, ...]

    before: ClassVar[str] = "answering the rule you just read"

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> Iterator[Self]:
        read = record.opened_by(deal, opened)
        audit = SessionAudit.read(deal.audit)
        faults = tuple(
            (source, fault) for source in read if source.stem in record.due.rules
            and (fault := audit.fault() or audit.entry(source.stem).unengaged(source))
        )
        if faults:
            yield cls(record, deal.audit, faults, read)

    @property
    def reason(self) -> str:
        """Every unanswered rule at once, so one rewrite of the audit can clear them all."""
        return " ".join(
            f"You read {source} and the audit's entry for {source.stem} does not answer it:"
            f" {fault}." for source, fault in self.faults
        ) + f" Answer each in {self.audit} before the next file opens."

    def permits_read(self, path: Path) -> bool:
        return path in self.read


@dataclass(frozen=True)
class Unread(Owed):
    """A dealt file has not been read in full. They open in the order dealt: the papers first,
    the indictment last. On 2026-10-30 claude-code opened the indictment and two rules first and
    never read either paper, because any unread dealt file opened."""

    remaining: tuple[Path, ...]
    unopened: tuple[Path, ...]
    """The dealt files no read has opened yet, in the order dealt; one in flight counts as
    opened, so a parallel batch's first read does not pin the order to itself."""

    before: ClassVar[str] = "reading"

    def permits_read(self, path: Path) -> bool:
        """Any file already opened, since re-reading is how a rule gets answered, or the next
        unopened one in the dealt order."""
        return path not in self.unopened or path == self.unopened[0]

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> Iterator[Self]:
        read = record.read_by(deal)
        remaining = tuple(source for source in record.paths if source not in read)
        if remaining:
            taken = record.opened_by(deal, opened)
            yield cls(record, deal.audit, remaining,
                      tuple(source for source in record.paths if source not in taken))

    @property
    def reason(self) -> str:
        return (
            f"Read the rules you were dealt first, in the order dealt, each in full with your"
            f" read tool (no offset or limit). Next: {next(iter(self.unopened), self.remaining[0])}."
            " Still unread: " + ", ".join(str(source) for source in self.remaining)
            + ". Your summary is a paraphrase of them, not a reading."
        )


@dataclass(frozen=True)
class Unaudited(Owed):
    """Everything is read and answered, and the audit still skips a rule or the owner's words."""

    refusal: str

    before: ClassVar[str] = "writing the required audit"

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> Iterator[Self]:
        refusal = record.due.refusal(SessionAudit.read(deal.audit))
        if refusal:
            yield cls(record, deal.audit, refusal)

    @property
    def reason(self) -> str:
        return f"Reading is passive; {self.refusal}."


class RuleDeal(State):
    """One session's deal: the files beside its `stem` that record what it was dealt, what it
    has read in full and the audit it wrote."""

    owner_prompts: ClassVar[int] = 6
    """How many of the person's own last prompts a deal puts back in front of the session."""

    declared_owed: ClassVar[dict[type, tuple[type[Owed], ...]]] = {}
    """What each class itself declares the deal can owe; a requirement parent names its case."""

    requirements: ClassVar[tuple[type[Owed], ...]] = ()
    """Everything this deal can owe, each class's declaration in MRO order."""

    def __init_subclass__(cls, owes: tuple[type[Owed], ...] = (), **options: object) -> None:
        super().__init_subclass__(**options)
        cls.declared_owed[cls] = owes
        cls.requirements = tuple(
            case for base in cls.__mro__ for case in cls.declared_owed.get(base, ())
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
        """The dealt files read in full since context was last lost, one DealtRead per line."""
        return self.stem.with_suffix(".dealt-reads")

    @property
    def dealt_opened(self) -> OpenedReadsStore:
        return OpenedReadsStore(self.stem.with_suffix(".dealt-opened"))

    def reads(self) -> tuple[DealtRead, ...]:
        if not self.dealt_read.exists():
            return ()
        return tuple(
            FieldCodec.decode(DealtRead, FieldCodec.parse(line.encode()))
            for line in self.dealt_read.read_text(encoding="utf-8").splitlines()
        )

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
        return self.current().answer(self, self.dealt_opened.read())

    def deal(self, dealt: type[DealtSources], prompts: tuple[str, ...]) -> None:
        """A start or compaction drops what was read: the read log resets first, then the record
        is published whole, so no reader sees a half-written deal. The audit goes with the reads:
        a fresh instance owes a fresh confession, never one carried over from the context it
        lost (plans/GATE-ENGAGEMENT-PLAN.md). A resume deals nothing and keeps both."""
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        self.calls.unlink(missing_ok=True)
        self.dealt_read.write_text("", encoding="utf-8")
        self.dealt_opened.replace(OpenedReads())
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
        """A completed read covering a whole dealt file counts its content; any other read
        counts nothing."""
        if path not in self.current().paths or not span.covers(path):
            return
        source = DealtSource.of(path)
        read = DealtRead(source, source.digest())
        with self.dealt_read.open("a", encoding="utf-8") as record:
            record.write(json.dumps(FieldCodec.encode(read)) + "\n")


class ToolGate(RuleDeal):
    """A deal that judges each tool call before it runs. Each requirement is a parent declaring
    the case it can owe; this base requires nothing."""

    def judge_tool(self, tool: str) -> None:
        """Refuse this call while anything is owed, or let it run."""
        self.state().enforce(self, tool)

    def judge_read(self, tool: str, path: Path) -> None:
        """Reading a dealt file the state permits is the way through the gate, and the session
        may always read its own audit to answer it; any other read waits."""
        if path == self.audit.resolve():
            return
        record = self.current()
        if path not in record.paths:
            self.state().enforce(self, tool)
            return

        def judged(opened: OpenedReads) -> DocumentChange[OpenedReads, ToolGateState]:
            """One change decides the read and records it, so the next hook sees it."""
            state = record.answer(self, opened)
            if state.permits_read(path):
                return DocumentChange(opened.opening(path), Open())
            return DocumentChange(opened, state)

        self.dealt_opened.update(judged).enforce(self, tool)

    def judge_write(self, tool: str, path: Path) -> None:
        """Writing the audit itself is the way through the gate; any other write waits."""
        if path != self.audit.resolve():
            self.judge_tool(tool)

    @abstractmethod
    def refuse(self, reason: str) -> None:
        """Deliver this refusal to the model the way the backend refuses a tool call."""


class AnswersDealtRules(ToolGate, owes=(Unengaged,)):
    """Each rule read is answered in the audit before the next unread file opens."""


class ReadsDealtSources(ToolGate, owes=(Unread,)):
    """Every dealt file is read in full before anything else runs."""


class AuditsDealtRules(ToolGate, owes=(Unaudited,)):
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
        if self.passed(usage):
            self.reread()

    def refresh_answered(self, usage: ContextUsage) -> None:
        """Every refresh the context passed while the deal is read counts as dealt."""
        self.passed(usage)

    def passed(self, usage: ContextUsage) -> tuple[str, ...]:
        """The refreshes the context passed since the last compaction, each recorded once."""
        done = frozenset(self.refreshed.read_text(encoding="utf-8").split())
        due = tuple(
            refresh.key
            for refresh in self.refreshes
            if refresh.due(usage) and refresh.key not in done
        )
        with self.refreshed.open("a", encoding="utf-8") as record:
            record.write("".join(f"{key}\n" for key in due))
        return due
