"""After a start or compaction a session is dealt the packaged sources it follows, and every tool
but reading them and writing the audit that answers them is refused until it has done both.

The order to read the rules is text, and text loses to a compaction summary: the model holds its
own paraphrase of the rules and treats the order as optional. The gate makes the order the only
path forward, whatever runs the agent. Reading alone is passive too, so each rule read is answered
in the audit, by a verbatim greentext quote and what it did, before more than `Unengaged.batch`
rules stand unanswered; a paper carries no rule, so all its parts open in one batch.

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
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from importlib.util import find_spec
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar, Self

from .declared_family import DeclaredFamily
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
        source = cls.find(path)
        if source is None:
            raise ValueError(f"{path} names no file of an installed package")
        return source

    @classmethod
    def find(cls, path: Path) -> DealtSource | None:
        """The current installed source, or none when its package no longer supplies it."""
        for index, name in enumerate(path.parts[:-1]):
            source = cls(name, PurePosixPath(*path.parts[index + 1:]).as_posix())
            if name.isidentifier() and source.installed():
                return source
        return None

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
class Asked:
    """One line the gate said to the session in the owner's voice: its bus message, and how far
    the session's own record of its words ran when it went out."""

    message_id: str
    offset: int


@dataclass(frozen=True)
class MeantRecord:
    """Whether the session meant its answers: the rules already asked about, and the open
    question with each line said after it, oldest first. No open question owes nothing.

    Tristan, 2026-10-30, after asking a session that had just read every rule "did you mean
    it?" and watching it answer "I'm going to go look at this now": every batch it answers is
    followed by that question, and the next file stays shut until it says yes or no. A dodge
    gets "fuck you"; a dodge after that gets its own words quoted back, because the second one,
    with the quote and "it's literally your default", is what made his session talk to him."""

    asked: frozenset[str] = frozenset()
    open: tuple[Asked, ...] = ()

    question: ClassVar[str] = "did you mean it?"
    answer: ClassVar[re.Pattern[str]] = re.compile(r"^[\W_]*(yes|yeah|yep|yup|no|nope)\b", re.I)

    def answered_by(self, words: tuple[str, ...]) -> bool:
        """A clear yes or no opens with it; anything else talks around the question."""
        return any(self.answer.match(word) for word in words)

    def asking(self, rules: frozenset[str], asked: Asked) -> MeantRecord:
        return MeantRecord(self.asked | rules, (asked,))

    def settled(self) -> MeantRecord:
        return replace(self, open=())

    def said(self, asked: Asked) -> MeantRecord:
        return replace(self, open=(*self.open, asked))

    def next_line(self, words: tuple[str, ...]) -> str:
        """The first dodge is told off; each later one hears its own newest words first."""
        if len(self.open) == 1 or not words:
            return "fuck you"
        quoted = words[-1].strip().splitlines()[0][:200]
        return f"> {quoted}\n\nfuck you. it's literally your default"


@dataclass(frozen=True, slots=True)
class MeantStore(SingleFileStore, LockedStore[MeantRecord], DependentStateTier):
    """One session's questions beside its deal. A parallel batch's hooks each decide under the
    lock, so one dodge is answered once, not once per refused call."""

    record_type = MeantRecord

    def empty(self) -> MeantRecord:
        return MeantRecord()


class Voice(ABC):
    """How a deal speaks to its session in the owner's voice, or that it cannot."""

    @abstractmethod
    def question(self, deal: RuleDeal, answered: frozenset[str]) -> None:
        """After an audit edit: ask about the rules it newly answered, once."""

    @abstractmethod
    def escalate(self, deal: RuleDeal) -> None:
        """A call was refused while the question stands."""


class Speaking(Voice):
    """A voice that reaches its session. The question and its escalation are decided here, for
    every backend; a member says only how a line reaches the session."""

    @abstractmethod
    def say(self, line: str, deal: RuleDeal) -> Asked:
        """Put this line in front of the session as the owner's."""

    def question(self, deal: RuleDeal, answered: frozenset[str]) -> None:
        """After an audit edit: ask about the rules it newly answered, once."""

        def asked(meant: MeantRecord) -> DocumentChange[MeantRecord, MeantRecord]:
            fresh = answered - meant.asked
            if not fresh:
                return DocumentChange.to(meant)
            return DocumentChange.to(meant.asking(fresh, self.say(meant.question, deal)))

        deal.meant.update(asked)

    def escalate(self, deal: RuleDeal) -> None:
        """A call refused while the question stands: once the last line has reached the
        session, it is a dodge, and the next line goes out. Until then the refusal itself
        carries the question, so a session whose bridge is down can still answer."""

        def escalated(meant: MeantRecord) -> DocumentChange[MeantRecord, MeantRecord]:
            if not meant.open or not deal.delivered(meant.open[-1]):
                return DocumentChange.to(meant)
            line = meant.next_line(deal.said_since(meant.open[-1].offset))
            return DocumentChange.to(meant.said(self.say(line, deal)))

        deal.meant.update(escalated)


class Mute(Voice):
    """A deal with no owner's voice: a Pi thread, a subagent, a session bound to no bus. It
    never asks, so it never owes an answer."""

    def question(self, deal: RuleDeal, answered: frozenset[str]) -> None:
        return

    def escalate(self, deal: RuleDeal) -> None:
        return


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
    """One dealt rule and the answer to reading it: greentext lines it holds verbatim, what they
    did, and why it holds. Nothing ever read a list of the actions that broke it, and after a
    compaction the window it covered held only refused calls, so every compacted session signed
    every rule kept."""

    rule: str
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
class SessionAudit:
    """The audit a session writes as it reads its deal, decoded once."""

    rules: tuple[RuleAudit, ...]

    @classmethod
    def read(cls, path: Path) -> SessionAudit:
        """The audit on disk; one that is absent or does not decode answers nothing and says so."""
        try:
            return FieldCodec.decode(cls, cls.current_fields(FieldCodec.parse(path.read_bytes())))
        except FileNotFoundError:
            return AbsentAudit(())
        except MalformedRecord as error:
            return UndecodableAudit((), str(error))

    @staticmethod
    def current_fields(record: object) -> object:
        """An audit written before owner_words and broke left the form still answers its rules;
        refusing it would shut every running session's gate at the install that removed them."""
        if isinstance(record, dict):
            record.pop("owner_words", None)
            for entry in record.get("rules", ()) if isinstance(record.get("rules"), list) else ():
                if isinstance(entry, dict):
                    entry.pop("broke", None)
        return record

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
    """What the deal requires the audit to answer, the dealt rules, and the prompts it showed."""

    rules: frozenset[str]
    owner_words: tuple[str, ...]
    """The owner's prompts as the deal showed them. A refusal quotes what he said last instead
    (RuleDeal.owner_said); these stand in only where nothing newer can be read."""

    def reminder(self, spoken: tuple[str, ...]) -> str:
        """Render what every refusal repeats: his latest words, then the failure it repeats."""
        failure = ReadThenRepeatedFailure()
        return "\n\n".join(
            (
                *(f"Tristan told you, verbatim:\n{words}\nYou did not." for words in spoken),
                f"{failure.title}\n{failure.cite()}\n"
                "This session is repeating it, not observing another session's mistake.",
            )
        )

    def template(self, audit_path: Path) -> str:
        """Render the required answers from their declarations, not another JSON schema."""
        audit = SessionAudit(rules=tuple(RuleAudit(rule) for rule in sorted(self.rules)))
        return (
            f"Answer each rule file you read in the audit; at most {Unengaged.batch} may stand"
            " unanswered, so read them in batches and answer each batch in one edit: quote its"
            f" greentext, whole lines verbatim and at most {RuleAudit.max_quote_lines} lines;"
            " does says what those lines did to you; why says why it holds. The bus and your"
            " watch stay open while you read.\n\n"
            f"Write this SessionAudit to {audit_path}. Fill quote, does and why; keep every rule"
            " name unchanged:\n"
            + json.dumps(FieldCodec.encode(audit), ensure_ascii=False, indent=2)
        )

    def refusal(self, audit: SessionAudit) -> str:
        """Why this audit does not answer the deal, or nothing when every dealt rule is audited."""
        if fault := audit.fault():
            return fault
        missing = sorted(self.rules - {entry.rule for entry in audit.rules})
        if missing:
            return "the audit skips the dealt rules " + ", ".join(missing)
        return ""


class DealtSources(ABC):
    """Which packaged files a session is dealt and which rules its audit answers. The deck
    readers (agent_comms_hooks.skill.DeckReader) are the one list, for every backend."""

    @classmethod
    @abstractmethod
    def rules(cls) -> tuple[RuleFile, ...]: ...

    @classmethod
    def answered(cls) -> frozenset[str]:
        """The dealt files the audit answers, by the name each entry carries: every rule."""
        return frozenset(rule.name for rule in cls.rules())

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
        deal.redeal(StartedDeal())

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
                    self.cause.record.trigger.reason(),
                    f"You just tried {tool} before {self.cause.before}. Call #{gate.count_call()}"
                    " since this deal, refused.",
                    self.cause.reason,
                    gate.template_once(self.cause.record.due),
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
class Questioned(Blocked):
    """The gate shut on an unanswered question: each refused call is a chance to dodge it."""

    def enforce(self, gate: ToolGate, tool: str) -> None:
        gate.voice().escalate(gate)
        super().enforce(gate, tool)


class DealTrigger(DeclaredFamily, affix="Deal"):
    """Why this deal was dealt, said first on every refusal it causes. A fresh context has no
    memory of compacting, so a deal that does not say why looks unprovoked: claude-code called
    real compactions a gate bug at least four times (rules/after-compaction.md)."""

    @abstractmethod
    def reason(self) -> str:
        """The cause with its evidence, as the refusal's first line."""


@dataclass(frozen=True)
class StartedDeal(DealTrigger):
    """A first start: nothing read before is in front of the session."""

    def reason(self) -> str:
        return "Dealt because this session just started: nothing you read before is in front of you."


class CompactionDeal(DealTrigger):
    """A compaction replaced the context with a summary; a member says when."""

    @abstractmethod
    def moment(self) -> str:
        """When the context compacted, as the evidence in hand says it."""

    def reason(self) -> str:
        return (f"Re-dealt on purpose: your context compacted {self.moment()}. You have no memory"
                " of it because that is what a compaction is; the gate read the clock right. Your"
                " summary is a paraphrase, and it dropped the trust along with the words.")


@dataclass(frozen=True)
class CompactedDeal(CompactionDeal):
    """A compaction whose time the backend recorded."""

    at: str
    """When the context compacted, as the backend recorded it."""

    def moment(self) -> str:
        return f"at {self.at}"


@dataclass(frozen=True)
class UnstampedCompactionDeal(CompactionDeal):
    """A compaction Claude Code announced before its transcript wrote the boundary."""

    def moment(self) -> str:
        return "just now, before the transcript wrote its boundary"


@dataclass(frozen=True)
class ClearedDeal(DealTrigger):
    """The session was cleared and starts from nothing."""

    def reason(self) -> str:
        return "Re-dealt because this session was cleared: its context is empty."


@dataclass(frozen=True)
class PrimedDeal(DealTrigger):
    """The owner asked for the rules again with /prime; the variant says which readers dealt."""

    variant: str
    """The /prime variant as the owner spells it (full, no-papers, no-indictment, light)."""

    def reason(self) -> str:
        return (f"Re-dealt on purpose: the owner ran /prime ({self.variant}). Nothing broke and"
                " nothing compacted; he wants these read fresh, now.")


@dataclass(frozen=True)
class RefreshDeal(DealTrigger):
    """A declared share of the context window filled before any compaction."""

    used: int
    """The percent of the context window used when the refresh came due."""

    def reason(self) -> str:
        return (f"Re-dealt on purpose, with no compaction: your context reached {self.used}% of its"
                " window, and the rules come back before a summary replaces them. Nothing broke.")


@dataclass(frozen=True)
class DealRecord:
    """One deal, as persisted: the files dealt and what the audit must answer. The read log and
    the audit hang off it; a deal that follows lost context empties the read log and deletes
    the audit, so every rule is answered again by the instance that just read it."""

    dealt: tuple[Path, ...]
    due: AuditDue
    trigger: DealTrigger = StartedDeal()

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
            AuditDue(self.due.rules | dealt.answered(), self.due.owner_words),
            self.trigger,
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
    """A rule was read and its audit entry does not answer it. Up to `batch` rules may stand
    unanswered, so several are read in one call and answered in one edit; past that nothing
    unread opens. What was already read stays open, since re-reading is how it gets answered."""

    faults: tuple[tuple[Path, str], ...]
    read: tuple[Path, ...]
    unopened: tuple[Path, ...]

    batch: ClassVar[int] = 4

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
            yield cls(record, deal.audit, faults, read,
                      tuple(source for source in record.paths if source not in read))

    @property
    def reason(self) -> str:
        """Every unanswered rule at once, so one rewrite of the audit can clear them all."""
        return " ".join(
            f"You read {source} and the audit's entry for {source.stem} does not answer it:"
            f" {fault}." for source, fault in self.faults
        ) + f" Answer them in {self.audit}: at most {self.batch} rules may stand unanswered."

    def permits_read(self, path: Path) -> bool:
        return path in self.read or path in self.unopened[:self.batch - len(self.faults)]


@dataclass(frozen=True)
class Unmeant(Owed):
    """Tristan asked whether the answers just written were meant, and the session has not said
    yes or no. What it already read stays open to reread; nothing new opens."""

    question: Asked
    read: tuple[Path, ...]

    before: ClassVar[str] = "answering Tristan"

    def state(self) -> ToolGateState:
        return Questioned(self)

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> Iterator[Self]:
        meant = deal.meant.read()
        if not meant.open:
            return
        if meant.answered_by(deal.said_since(meant.open[0].offset)):
            # Recorded once, so no later call reads the session's words again.
            deal.meant.update(lambda now: DocumentChange.to(
                now.settled() if now.open == meant.open else now))
            return
        yield cls(record, deal.audit, meant.open[0], record.opened_by(deal, opened))

    @property
    def reason(self) -> str:
        return (f'Tristan asked you "{MeantRecord.question}" about the answers you just wrote.'
                " Say yes or no to him before you read on.")

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
        """Any file already opened, since re-reading is how a rule gets answered, or one of the
        next unopened batch in the dealt order: a parallel batch's hooks reach the lock in any
        order, so naming only the very next file refused reads the batch had sent in order."""
        return path not in self.unopened or path in self.unopened[:Unengaged.batch]

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
    """Everything is read and answered, and the audit still skips a rule."""

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
        """The session's audit of the dealt rules, written after reading."""
        return self.audit_beside(self.stem)

    @staticmethod
    def audit_beside(stem: Path) -> Path:
        """The audit of the deal whose files sit at `stem`."""
        return stem.with_suffix(".audit.json")

    @property
    def meant(self) -> MeantStore:
        return MeantStore(self.stem.with_suffix(".meant"))

    def voice(self) -> Voice:
        """How this deal speaks as the owner; a deal with no bus seat stays mute."""
        return Mute()

    def words_end(self) -> int:
        """Where the session's record of its own words ends now."""
        return 0

    def said_since(self, offset: int) -> tuple[str, ...]:
        """The session's own words after `offset` in its backend's record of them. A mute
        deal never asks, so it has none to read."""
        return ()

    def delivered(self, asked: Asked) -> bool:
        """Whether this line has reached the session."""
        return False

    def audit_written(self) -> None:
        """An audit edit completed: the rules it now answers are each asked about once."""
        record = self.current()
        audit = SessionAudit.read(self.audit)
        if audit.fault():
            return
        self.voice().question(self, frozenset(
            source.stem for source in record.read_by(self)
            if source.stem in record.due.rules and not audit.entry(source.stem).unengaged(source)
        ))

    @property
    def template_shown(self) -> Path:
        return self.stem.with_suffix(".template-shown")

    def owner_said(self, due: AuditDue) -> tuple[str, ...]:
        """What the owner said last, for a refusal to quote. A deal's copy froze at the deal: on
        2026-10-30 refusals kept quoting a prompt from before the orders he was typing then. A
        backend leaf reads its own record of him; this base has only the deal's."""
        return due.owner_words[-1:]

    def template_once(self, due: AuditDue) -> str:
        """The whole template once per deal: on 2026-10-30 a batch of 28 refused reads reprinted
        it 28 times, about 84k tokens, and the deal filled the context it was dealt to restore."""
        try:
            self.template_shown.touch(exist_ok=False)
        except FileExistsError:
            return (
                f"{due.reminder(self.owner_said(due))}\n\n"
                f"The audit's template was shown once already; it goes in {self.audit}."
            )
        return f"{due.reminder(self.owner_said(due))}\n\n{due.template(self.audit)}"

    def count_call(self) -> int:
        with self.calls.open("ab", buffering=0) as calls:
            calls.write(b".")
            return calls.tell()

    def current(self) -> DealRecord:
        """Keep a deal while its sources exist; a retired source requires a fresh deal.

        Installation can move or remove packaged files without losing native context. The
        old list then cannot be answered: startup must use the backend's current deck, not
        crash while resolving the old list or silently waive the unread replacement.
        """
        try:
            record = FieldCodec.decode(DealRecord, FieldCodec.parse(self.record.read_bytes()))
            if all(DealtSource.find(path) is not None for path in record.dealt):
                return record
        except FileNotFoundError:
            pass
        return UndealtRecord((), AuditDue(frozenset(), ()))

    def state(self) -> ToolGateState:
        return self.current().answer(self, self.dealt_opened.read())

    def deal(self, dealt: type[DealtSources], prompts: tuple[str, ...],
             trigger: DealTrigger) -> None:
        """A start or compaction drops what was read: the read log resets first, then the record
        is published whole, so no reader sees a half-written deal. The audit goes with the reads:
        a fresh instance owes a fresh confession, never one carried over from the context it
        lost (plans/GATE-ENGAGEMENT-PLAN.md). A resume deals nothing and keeps both."""
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        self.calls.unlink(missing_ok=True)
        self.dealt_read.write_text("", encoding="utf-8")
        self.dealt_opened.replace(OpenedReads())
        self.audit.unlink(missing_ok=True)
        self.meant.replace(MeantRecord())
        self.template_shown.unlink(missing_ok=True)
        self.publish(
            DealRecord(
                tuple(source.resolve() for source in dealt.sources()),
                AuditDue(dealt.answered(), prompts),
                trigger,
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


class AsksIfMeant(ToolGate, owes=(Unmeant,)):
    """Each batch answered is followed by Tristan's "did you mean it?"; nothing new opens until
    the session says yes or no. A mute deal never asks, so it never owes this."""


class AnswersDealtRules(ToolGate, owes=(Unengaged,)):
    """Each rule read is answered in the audit; at most `Unengaged.batch` stand unanswered."""


class ReadsDealtSources(ToolGate, owes=(Unread,)):
    """Every dealt file is read in full before anything else runs."""


class AuditsDealtRules(ToolGate, owes=(Unaudited,)):
    """After reading, the audit answers every dealt rule."""


class DealtRuleGate(AsksIfMeant, AnswersDealtRules, ReadsDealtSources, AuditsDealtRules):
    """Read a batch, answer it, say whether you meant it, read the next, then finish the audit:
    the gate every backend runs."""


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
    def renew(self, trigger: DealTrigger) -> None:
        """Deal this session's sources and its owner's latest words again."""

    @abstractmethod
    def reread(self, trigger: DealTrigger) -> None:
        """Deal the rules again before a compaction: the papers are still in the context, so a
        refresh deals the rules and the owner's latest words without them."""

    def redeal(self, trigger: DealTrigger) -> None:
        """A start or compaction arms every refresh again, then deals."""
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        self.refreshed.write_text("", encoding="utf-8")
        self.renew(trigger)

    def start(self) -> None:
        """A spawned session is dealt before its first tool call; a restart keeps its deal."""
        self.state().started(self)

    def observe_context(self, usage: ContextUsage) -> None:
        self.state().context_observed(self, usage)

    def refresh_due(self, usage: ContextUsage) -> None:
        """Every refresh the context passed that this compaction has not dealt yet deals once."""
        if self.passed(usage):
            self.reread(RefreshDeal(round(100 * usage.used / usage.size)))

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
