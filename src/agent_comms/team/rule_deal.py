"""After a start or compaction a session is dealt the packaged sources it follows, and every tool
but reading them and writing the audit that answers them is refused until it has done both.

The order to read the rules is text, and text loses to a compaction summary: the model holds its
own paraphrase of the rules and treats the order as optional. The gate makes the order the only
path forward, whatever runs the agent. Reading alone is passive too, so the files open in batches
of `Unread.batch` and the audit then answers the costly probes (rule_probes.py): a quote and a
paraphrase per rule were cheap to write and proved nothing, so the deal asks what only looking at
the code, the papers or Tristan's words can answer.

`State` is the one root: a deal is a state, a gate state is a state, and what a deal still owes
is a state, so every consumer holds the family and asks it. `RuleDeal` owns where one session's
deal lives; the requirements are declaration-only parents, each naming the case it can owe, and
the MRO orders them, so a backend that wants a different gate composes different parents. A
backend leaf supplies only where its deal lives and how a refusal reaches its model: the Claude
Code hooks deny a PreToolUse event, Core's Pi tool owner raises a tool denial.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, ClassVar, Self

from annotated_types import Ge, Gt, Lt
from python_introspect.jsonable import RegisteredClassChoices
from python_introspect.validation import validate_annotated_dataclass

from agent_comms.execution.context_reading import ContextUsage
from agent_comms.nominal.configuration_record import ConfigurationRecord
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    Description,
    FieldCodec,
    MalformedRecord,
    MessageId,
)
from agent_comms.projects.repo import Repo
from agent_comms.projects.rule_probes import Probe, ProbeContext
from agent_comms.storage.locked_store import DocumentChange, LockedStore
from agent_comms.storage.store_files import AtomicFile, DependentStateTier, SingleFileStore
from agent_comms.team.dealt_sources import DealtSources
from agent_comms.team.instruction_deck import DealtCalls
from agent_comms.team.prompt_reads import DealtRead, DealtSource, PreparedPrompt, PromptReads
from agent_comms.team.rule_file import RuleFile


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
    """One line the gate published in the owner's voice: its original message and what it said.
    A refusal repeats these words rather than quoting the audit as it stands now, so the session
    sees the row this id asked about. On 2026-11-04 a session answered four ids about the wrong
    rows, because every refusal quoted the audit's newest answer under the old id."""

    message_id: MessageId
    line: str = "did you mean it?"


@dataclass(frozen=True)
class MeantRecord:
    """Whether the session meant its answers: the probes already asked about, and the open
    question with each line said after it, oldest first. No open question owes nothing.

    Tristan, 2026-10-30, after asking a session that had just read every rule "did you mean
    it?" and watching it answer "I'm going to go look at this now": every batch it answers is
    followed by that question, and the next file stays shut until it says yes or no. A dodge
    gets "fuck you"; a dodge after that gets its own words quoted back, because the second one,
    with the quote and "it's literally your default", is what made his session talk to him."""

    asked: frozenset[str] = frozenset()
    open: tuple[Asked, ...] = ()

    question: ClassVar[str] = Asked.line
    answer: ClassVar[re.Pattern[str]] = re.compile(r"^[\W_]*(yes|yeah|yep|yup|no|nope)\b", re.I)
    answer_words: ClassVar[int] = 12

    def answered_by(self, audit: SessionAudit) -> bool:
        """Only this open question's explanatory audit answer settles its commitment."""
        return bool(self.open and self.answers_in(audit.answers_for(self.open[0].message_id)))

    @classmethod
    def answers_in(cls, words: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(word for word in words
                     if cls.answer.match(word) and len(word.split()) >= cls.answer_words)

    def covering(self, answered: frozenset[str]) -> MeantRecord:
        """An open question covers every probe answered while it stands: nothing new opens until
        it is answered. Asking again would replace it,
        and the session's answer must name the question it read. On 2026-11-02 pi-teammate
        answered "Yes" four times while each audit edit replaced the question it named."""
        return replace(self, asked=self.asked | answered) if self.open else self

    def asking(self, probes: frozenset[str], asked: Asked) -> MeantRecord:
        return replace(self, asked=self.asked | probes, open=(asked,))

    def settled(self, audit: SessionAudit) -> MeantRecord:
        if not self.answered_by(audit):
            raise ValueError("No explanatory audit answer names the open question")
        return replace(self, open=())

    def answer_to_open(self, audit: SessionAudit) -> str:
        """The audit's first explanatory answer naming the open question."""
        return self.answers_in(audit.answers_for(self.open[0].message_id))[0]

    def said(self, asked: Asked) -> MeantRecord:
        return replace(self, open=(*self.open, asked))

    def next_line(self, audit: SessionAudit) -> str:
        """The first dodge is told off; later ones quote the audit's own probe answer."""
        if len(self.open) == 1:
            return "fuck you"
        quoted = audit.last_said(audit.answered_probes())
        return (f"> {quoted}\n\n" if quoted else "") + "fuck you. it's literally your default"


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
    def answered(self, question: Asked, answer: str) -> None:
        """The session's audit settled this question: its answer is published as the reply."""

    @abstractmethod
    def question(self, deal: RuleDeal, audit: SessionAudit, answered: frozenset[str]) -> None:
        """After a successful audit edit: ask about the probes it newly answered, once."""

    @abstractmethod
    def escalate(self, deal: RuleDeal, audit: SessionAudit) -> None:
        """A successful audit edit wrote a non-answer to its open question."""


class Speaking(Voice):
    """A voice that reaches its session. The question and its escalation are decided here, for
    every backend; a member says how a line reaches the session and how its answer is shown."""

    @abstractmethod
    def say(self, line: str, deal: RuleDeal) -> Asked:
        """Put this line in front of the session as the owner's."""

    def question(self, deal: RuleDeal, audit: SessionAudit, answered: frozenset[str]) -> None:
        """After an audit edit: ask about the probes it newly answered, once."""

        def asked(meant: MeantRecord) -> DocumentChange[MeantRecord, MeantRecord]:
            covered = meant.covering(answered)
            fresh = answered - covered.asked
            if not fresh:
                return DocumentChange.to(covered)
            line = audit.his_question(fresh)
            return DocumentChange.to(covered.asking(fresh, self.say(line, deal)))

        deal.meant.update(asked)

    def escalate(self, deal: RuleDeal, audit: SessionAudit) -> None:
        """Each successful non-answer edit is one dodge. Retrying a refused tool is not a
        new commitment and cannot repeatedly escalate the same unchanged audit."""

        def escalated(meant: MeantRecord) -> DocumentChange[MeantRecord, MeantRecord]:
            words = audit.answers_for(meant.open[0].message_id) if meant.open else ()
            if not words or meant.answered_by(audit):
                return DocumentChange.to(meant)
            return DocumentChange.to(meant.said(self.say(meant.next_line(audit), deal)))

        deal.meant.update(escalated)


class Mute(Voice):
    """A deal that cannot publish in the owner's voice: it never asks or owes an answer."""

    def answered(self, question: Asked, answer: str) -> None:
        return

    def question(self, deal: RuleDeal, audit: SessionAudit, answered: frozenset[str]) -> None:
        return

    def escalate(self, deal: RuleDeal, audit: SessionAudit) -> None:
        return


class DealDelivery:
    """How a session receives the sources it is dealt. The runtime that launches the session
    owns the answer, through the strongest channel it has. By default nothing reaches the
    session until it reads each file with its own read tool."""

    open_meanwhile: ClassVar[str] = (
        "Until the deal is read and answered, the gate refuses every tool except reading these"
        " files and the audit, writing the audit, loading a tool's schema, and a Monitor running"
        " the bus watch."
    )

    def reads(self) -> tuple[DealtRead, ...]:
        """What the session already holds when its deal starts: nothing."""
        return ()

    def told(self, record: DealRecord, audit: Path) -> str:
        """The order a hook gives with the deal: read every file, then answer it."""
        sources = record.paths
        return (
            f"Read each of these {len(sources)} files in full with the Read tool before any other"
            " action: they are every rule you follow and the sources they come from. A summary"
            " or memory of them does not count as reading them. Read them in order, in batches"
            f" of {record.read_batch}, then answer the audit at {audit}: reading alone is passive."
            f" {self.open_meanwhile} This order is given once"
            " per session start or compaction; a short slice of them comes back inline every few"
            " tool calls.\n" + "\n".join(str(source) for source in sources)
        )


@dataclass(frozen=True)
class SystemPromptDelivery(PreparedPrompt, DealDelivery):
    """The deal arrived in the session's system prompt, papers first, the way its launcher
    prepared it: the session read it all before its first token, with the system prompt's
    authority. It owes the witness, not the reads: the audit and "did you mean it?" still stand
    before the first tool call (Tristan, 2026-11-01)."""

    def reads(self) -> tuple[DealtRead, ...]:
        return FieldCodec.decode(PromptReads, FieldCodec.parse(self.manifest.read_bytes())).reads

    def told(self, record: DealRecord, audit: Path) -> str:
        sources = record.paths
        return (
            f"These {len(sources)} files are in your system prompt, in this order, and count as"
            " read: they are every rule you follow and the sources they come from. Before any"
            f" other action, answer every rule in the audit at {audit}. {self.open_meanwhile} A"
            " file that changed after your prompt was prepared is not covered: read that one with"
            " the Read tool. A short slice comes back inline every few tool calls.\n"
            + "\n".join(str(source) for source in sources)
        )


@dataclass(frozen=True)
class OwnerWordsAnswer:
    """One of the owner's own prompts the deal showed, and what the session is doing about it."""

    words: str
    doing: str

    def acts(self) -> bool:
        """An answer that names no action is not an answer."""
        return bool(self.doing.strip())


@dataclass(frozen=True)
class MeantAnswer:
    """The session's own written commitment to one exact published question."""

    asked: MessageId
    answer: str = ""


@dataclass(frozen=True)
class SessionAudit:
    """The session's owner actions, probe answers and question commitments, decoded once."""

    owner_words: tuple[OwnerWordsAnswer, ...]
    meant: tuple[MeantAnswer, ...] = field(default=(), kw_only=True)
    probes: tuple[Probe, ...] = field(default=(), kw_only=True)

    @classmethod
    def read(cls, path: Path) -> SessionAudit:
        """The audit on disk; one that is absent or does not decode answers nothing and says so."""
        try:
            return FieldCodec.decode(cls, FieldCodec.parse(path.read_bytes()))
        except FileNotFoundError:
            return AbsentAudit(())
        except MalformedRecord as error:
            return UndecodableAudit((), str(error))

    def fault(self) -> str:
        """Why nothing written here can answer anything; a decoded audit has no such fault."""
        return ""

    def unreadable(self) -> str:
        """Why the file on disk is not an audit at all. A missing audit is not this: the deal
        writes it, and a blank one is simply unanswered."""
        return ""

    def answers_for(self, question: MessageId) -> tuple[str, ...]:
        """Written answers naming this question, in the session's original order."""
        return tuple(entry.answer for entry in self.meant if entry.asked == question)

    def answered_probes(self) -> frozenset[str]:
        """The probes this audit answers, by name; the gate judges each answer separately."""
        return frozenset(probe.declared_name for probe in self.probes if probe.text().strip())

    def last_said(self, probes: frozenset[str]) -> str:
        """The last nonempty line of the session's own answer to one of these probes, bounded."""
        return next((line.strip()[:200] for probe in reversed(self.probes)
                     if probe.declared_name in probes
                     for line in probe.text().splitlines() if line.strip()), "")

    def his_question(self, probes: frozenset[str]) -> str:
        """His question quotes the session's own answer and asks; it never narrates what the
        session did. An audit that says nothing hears the question alone."""
        statement = self.last_said(probes)
        return f"> {statement}\n\n{MeantRecord.question}" if statement else MeantRecord.question


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

    def unreadable(self) -> str:
        return self.fault()


@dataclass(frozen=True)
class AuditDue:
    """What the deal requires the audit to answer: the prompts shown, then the probes."""

    owner_words: tuple[str, ...]
    """The owner's prompts exactly as the deal showed them, each occurrence once."""
    repos: tuple[Repo, ...] = ()
    """The team's registered repositories when the deal was dealt, where a probe's cited
    file:line is read. Recorded with the deal because the gate answers from the audit on
    every tool call, long after the registry snapshot that knew them."""

    def reminder(self) -> str:
        """His last words, shown once with the template; the audit carries them after."""
        return "\n\n".join(
            f"Tristan told you, verbatim:\n{words}\nYou did not." for words in self.owner_words[-1:]
        )

    def skeleton(self) -> SessionAudit:
        """The audit owed before anything is answered: every prompt, every field blank.
        A deal writes it in place of the audit it drops, so the file a session fills always
        exists in the right shape: on 2026-11-02 compacted sessions met a missing audit, the
        template already scrolled away, and rebuilt the format wrong from memory."""
        return SessionAudit(
            owner_words=tuple(OwnerWordsAnswer(words, "") for words in self.owner_words),
        )

    def probe_shapes(self) -> str:
        """Each owed probe's question beside the shape its answer takes.

        The blank audit shows ``probes: []`` and nothing about what belongs in it, while
        ``meant`` has had its declaration-derived shape printed all along. rule_probes.py is
        unreadable until the audit parses, so the only documentation left was a sequence of
        type errors that each give away half the answer: a tagged object is told the
        discriminator is wrong, a bare string is told it wants an object. On 2026-11-08 that
        cost four sessions an hour between them. Same family, same derivation, both fields."""
        return "\n".join(
            f"{probe.declared_name}: {probe.question}\n"
            + json.dumps(FieldCodec.record_schema(probe), ensure_ascii=False)
            for probe in Probe.owed(self.owner_words)
        )

    def template(self, audit_path: Path) -> str:
        """Render the required answers from their declarations, not another JSON schema."""
        audit = self.skeleton()
        return "\n\n".join(
            (
                "When a question arrives, add its exact id and your explanatory answer to meant."
                " Each entry has this declaration-derived shape: "
                + json.dumps(FieldCodec.record_schema(MeantAnswer), ensure_ascii=False),
                "Every probe this deal owes, one object each in probes, each naming its member"
                f' in a {Probe.family_discriminator} field, as in'
                f' {{"{Probe.family_discriminator}": "prior", ...}}:\n' + self.probe_shapes(),
                f"The deal wrote this SessionAudit blank to {audit_path}; fill quote, does, why and"
                " doing in place (write it whole if the file is gone), keeping every rule name and"
                " quoted words unchanged. Doing names your current action; probes come after:\n"
                + json.dumps(FieldCodec.encode(audit), ensure_ascii=False, indent=2),
            )
        )

    def refusal(self, audit: SessionAudit) -> str:
        """Why this audit does not answer the deal, or nothing when it does: every shown prompt
        is answered by its own words with an action, and every owed probe costs its look."""
        if fault := audit.fault():
            return fault
        if idle := [answer.words for answer in audit.owner_words if not answer.acts()]:
            return "the audit names no action for: " + self.quoted(idle)
        unanswered = Counter(self.owner_words) - Counter(
            answer.words for answer in audit.owner_words
        )
        if unanswered:
            return ("the audit does not answer the owner's prompts, each copied exactly as this"
                    " JSON string into owner_words.words: " + self.quoted(unanswered.elements()))
        return self.probe_refusal(audit)

    def probe_refusal(self, audit: SessionAudit) -> str:
        """Why the audit's probes do not answer them: one answer per probe, each checked against
        what the rules cannot supply, and none mostly copied from the rules or another answer."""
        if missing := Probe.missing(audit.probes, self.owner_words):
            return "the audit answers no " + "; no ".join(
                f"{probe.declared_name} probe: {probe.question}" for probe in missing)
        dealt = tuple(f"{rule.wakeup}\n{rule.body}" for rule in RuleFile.packaged())
        faults = []
        for answer in audit.probes:
            others = tuple(other.text() for other in audit.probes if other is not answer)
            try:
                cwd = Path.cwd()
            except FileNotFoundError:
                faults.append("the session's working directory no longer exists; start a new session")
                break
            context = ProbeContext(self.owner_words, cwd, self.repos, (*dealt, *others))
            if fault := answer.fault(context):
                faults.append(f"its {answer.declared_name} probe does not answer: {fault}")
        return "; ".join(faults)

    @staticmethod
    def quoted(words: Iterable[str]) -> str:
        """Prompts as JSON strings: the quotes bound the exact text, so punctuation a refusal
        sentence adds after them is never copied in (2026-11-02: two sessions copied a period)."""
        return ", ".join(json.dumps(text, ensure_ascii=False) for text in words)


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

    def permits_source_read(self) -> bool:
        """Whether a file outside the deal may be read now."""
        return False

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

    def permits_source_read(self) -> bool:
        return True


class Undealt(Open):
    """A session never dealt: nothing to read, no audit owed and nothing to refresh."""

    def started(self, deal: Redeals) -> None:
        """A session never dealt is dealt at its first start."""
        deal.redeal(StartedDeal())


@dataclass(frozen=True)
class Blocked(ToolGateState):
    """One thing the deal still owes holds the gate shut."""

    cause: Owed

    def enforce(self, gate: ToolGate, tool: str) -> None:
        gate.blocked(self.cause, tool)

    def permits_read(self, path: Path) -> bool:
        return self.cause.permits_read(path)

    def permits_source_read(self) -> bool:
        return self.cause.permits_source_read()

    def context_observed(self, deal: Redeals, usage: ContextUsage) -> None:
        """A refresh passed while the deal is being answered is answered by it: the context
        filled with the very files being read. Dealing them again once the gate opened made
        frame-baseline and install-speed reread 16 files the call after finishing 25 (bus 45261,
        45247)."""
        deal.refresh_answered(usage)


class DealMode(DeclaredFamily, affix="DealMode"):
    """Session gate policy, retained with the deal and shared by every backend."""

    @classmethod
    def answer(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> ToolGateState:
        return Open()

    @classmethod
    def compaction[Said](cls, action: Callable[[], Said], kept: Said) -> Said:
        """Lost-context reading is independent of periodic refreshes."""
        return kept

    @classmethod
    def refresh[Said](cls, action: Callable[[], Said], kept: Said) -> Said:
        return kept


class OffDealMode(DealMode):
    """Do not enforce the read/audit gate or automatically redeal."""


class StartupDealMode(DealMode):
    """Enforce first-start and explicit deals, but never interrupt for automatic redeals."""

    @classmethod
    def answer(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> ToolGateState:
        owed = (case for owes in deal.requirements for case in owes.owed_by(deal, record, opened))
        return next(owed, Open()).state()


class CompactionDealMode(StartupDealMode):
    """Read on startup and after lost context, never at mid-session thresholds."""

    @classmethod
    def compaction[Said](cls, action: Callable[[], Said], kept: Said) -> Said:
        return action()


class FullDealMode(CompactionDealMode):
    """Enforce the gate and redeal on compaction and context thresholds."""

    @classmethod
    def refresh[Said](cls, action: Callable[[], Said], kept: Said) -> Said:
        return action()


class DealTrigger(DeclaredFamily, affix="Deal"):
    """Why this deal was dealt, said first on every refusal it causes. A fresh context has no
    memory of compacting, so a deal that does not say why looks unprovoked: claude-code called
    real compactions a gate bug at least four times (rules/spine.md)."""

    def deliver[Said](self, mode: type[DealMode], action: Callable[[], Said], kept: Said) -> Said:
        """First starts and explicit requests are not automatic interruptions."""
        return action()

    @abstractmethod
    def reason(self) -> str:
        """The cause with its evidence, as the refusal's first line."""

    def boards(self) -> str:
        """The line that opens the boards the deal recalls."""
        return "What the registered boards hold:"


@dataclass(frozen=True)
class StartedDeal(DealTrigger):
    """A first start: nothing read before is in front of the session."""

    def reason(self) -> str:
        return "Dealt because this session just started: nothing you read before is in front of you."


class CompactionDeal(DealTrigger):
    """A compaction replaced the context with a summary; a member says when."""

    def deliver[Said](self, mode: type[DealMode], action: Callable[[], Said], kept: Said) -> Said:
        return mode.compaction(action, kept)

    @abstractmethod
    def moment(self) -> str:
        """When the context compacted, as the evidence in hand says it."""

    def reason(self) -> str:
        return (f"Re-dealt on purpose: your context compacted {self.moment()}. You have no memory"
                " of it because that is what a compaction is; the gate read the clock right. Your"
                " summary is a paraphrase, and it dropped the trust along with the words.")

    def boards(self) -> str:
        return "FUCK YOUR COMPACTION. Your summary is a paraphrase. What the registered boards hold:"


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
    """The /prime variant as the owner spells it (full, no-papers, no-patterns, light)."""

    def reason(self) -> str:
        return (f"Re-dealt on purpose: the owner ran /prime ({self.variant}). Nothing broke and"
                " nothing compacted; he wants these read fresh, now.")


@dataclass(frozen=True)
class RefreshDeal(DealTrigger):
    """A declared share of the context window filled before any compaction."""

    def deliver[Said](self, mode: type[DealMode], action: Callable[[], Said], kept: Said) -> Said:
        return mode.refresh(action, kept)

    used: int
    """The percent of the context window used when the refresh came due."""

    def reason(self) -> str:
        return (f"Re-dealt on purpose, with no compaction: your context reached {self.used}% of its"
                " window, and the rules come back before a summary replaces them. Nothing broke.")


@dataclass(frozen=True, kw_only=True)
class RuleGateSettings(ConfigurationRecord):
    """One session's editable rule policy; read and audit evidence is not configuration."""

    mode: Annotated[type[DealMode], RegisteredClassChoices(DealMode),
                    Description("When this session must read and answer its rules")] = FullDealMode
    read_batch: Annotated[int, Ge(1),
                          Description("Files opened together before answering the audit")] = 5
    owner_prompts: Annotated[int, Ge(1),
                             Description("Recent owner prompts included in each deal")] = 6
    refresh_remaining: Annotated[tuple[Annotated[float, Gt(0), Lt(1)], ...],
                                 Description("Context remaining at each periodic refresh")] = (0.66, 0.33)


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


@dataclass(frozen=True)
class DealRecord(RuleGateSettings):
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
        deal.publish(replace(self, dealt=tuple(dict.fromkeys(
            (*self.paths, *(source.resolve() for source in dealt.sources()))
        ))))
        return kept

    def resume[Said](self, deal: Callable[[], Said], kept: Said) -> Said:
        """A resume keeps the context it had, and the deal it still answers: its reads and
        audit stay."""
        return kept

    def answer(self, deal: RuleDeal, opened: OpenedReads) -> ToolGateState:
        """The first requirement still owed holds the gate; with none owed it is open."""
        return self.mode.answer(deal, self, opened)

    @property
    def refreshes(self) -> tuple[RefreshAt, ...]:
        return tuple(RefreshAt(remaining) for remaining in self.refresh_remaining)

    def refreshes_passed(self, usage: ContextUsage) -> tuple[RefreshAt, ...]:
        """The refreshes this deal declares that the context has reached."""
        return tuple(refresh for refresh in self.refreshes if refresh.due(usage))

    def while_supplied(self) -> DealRecord:
        """This deal while every file it dealt is still installed.

        Installation can move or remove packaged files without losing native context. The
        old list then cannot be answered: startup must use the backend's current deck, not
        crash while resolving the old list or silently waive the unread replacement.
        """
        if all(DealtSource.find(path) is not None for path in self.dealt):
            return self
        return UndealtRecord()


@dataclass(frozen=True)
class UndealtRecord(DealRecord):
    """The record of a session with no deal on disk: nothing dealt, nothing due."""

    dealt: tuple[Path, ...] = ()
    due: AuditDue = AuditDue(())

    def refreshes_passed(self, usage: ContextUsage) -> tuple[RefreshAt, ...]:
        """Nothing was dealt, so nothing comes back."""
        return ()

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

    nudges: ClassVar[tuple[str, ...]] = (
        "Your tools are locked until the deal below is read and answered.",
        "Still locked, and the step has not moved. What is owed is named below.",
        "Still locked. This is not a punishment and not a log: it is one step, below.",
        "That is {number} tries at the same closed door. It opens on the one step below.",
    )
    """The gate's own line when a locked tool is tried, one per refused call: a person asking,
    not a log. On 2026-11-06 the refusal was a wall of paths, rules and failure citations, and
    Tristan asked that it read like a person: "this 100% changes the attention". The last
    line repeats.

    It is not signed with his name, and it no longer speaks as him. Tristan, 2026-11-08: a
    message of his that arrives "in the same message as something that's clearly automated"
    leaves the session unable to tell his words from the tooling's, "because it makes a
    difference in how you respond to it". His words reach a session as comms messages through
    Voice, so anything in his name is his and this line is the gate's. Prose invented here and
    signed Tristan is Failure 10, inventing rules and blaming them on him, in the one place a
    session cannot check."""

    def state(self) -> ToolGateState:
        return Blocked(self)

    def permits_source_read(self) -> bool:
        """Code stays closed while something other than the audit is owed."""
        return False

    def nudge(self, number: int) -> str:
        return self.nudges[min(number, len(self.nudges)) - 1].format(number=number)

    @classmethod
    @abstractmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> Iterator[Self]:
        """This case, when the deal owes it."""

    @property
    @abstractmethod
    def reason(self) -> str:
        """What the session skipped, and what clears it."""

    def explain_refusal(self, tool: str, refusal: int, template: str) -> str:
        """The owed case owns its refusal; the backend owns how it is delivered. The gate's own
        line first, why the deal came on the first refusal only, then the one step that opens it.

        Nothing here is attributed to Tristan: this text is automated, so a line of his inside
        it reads as automation and gets answered as automation. His own words arrive through
        Voice as comms messages instead.
        """
        return "\n\n".join((
            self.nudge(refusal),
            *((self.record.trigger.reason(),) if refusal == 1 else ()),
            f"{tool} waits on {self.before}. {self.reason}",
            *((template,) if template else ()),
        ))

    def permits_read(self, path: Path) -> bool:
        """Reading a dealt file is the way through the gate."""
        return True


@dataclass(frozen=True)
class Unmeant(Owed):
    """Tristan asked whether the answers just written were meant, and the session has not said
    yes or no. What it already read stays open to reread; nothing new opens."""

    question: Asked
    read: tuple[Path, ...]
    written: tuple[str, ...]
    """What the audit already answers under this question's id."""

    before: ClassVar[str] = "answering Tristan"

    nudges: ClassVar[tuple[str, ...]] = (
        "I asked you something. answer me before anything else.",
        "did you see my question? it's right below.",
        "still waiting on yes or no from you. the question is below.",
    )

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> Iterator[Self]:
        meant = deal.meant.read()
        if not meant.open:
            return
        audit = SessionAudit.read(deal.audit)
        yield cls(record, deal.audit, meant.open[0], record.opened_by(deal, opened),
                  audit.answers_for(meant.open[0].message_id))

    @property
    def reason(self) -> str:
        """The original question id is available even when the first template predated it."""
        entry = MeantAnswer(self.question.message_id)
        return (f"Tristan asks:\n\n{self.question.line}\n\n"
                "The gate: write the answer into this entry of the audit's meant list, keeping"
                f" the other entries, and save {self.audit}:\n"
                f"{json.dumps(FieldCodec.encode(entry), ensure_ascii=False)}\n"
                f"The gate accepts an answer that opens with yes or no and runs at least"
                f" {MeantRecord.answer_words} words. Lines Tristan says after this question carry"
                " their own message ids; none replaces it, so answer this id."
                + "".join(f"\nYour answer {json.dumps(answer, ensure_ascii=False)} under this id"
                          f" does not count: it has {len(answer.split())} words, fewer than"
                          f" {MeantRecord.answer_words}." if
                          MeantRecord.answer.match(answer) else
                          f"\nYour answer {json.dumps(answer, ensure_ascii=False)} under this id"
                          " does not count: it does not open with yes or no."
                          for answer in self.written))

    def permits_read(self, path: Path) -> bool:
        return path in self.read


@dataclass(frozen=True)
class Unread(Owed):
    """A dealt file has not been read in full. They open in the order dealt: the papers first,
    the pattern catalog last. On 2026-10-30 claude-code opened the closing file and two rules first and
    never read either paper, because any unread dealt file opened."""

    remaining: tuple[Path, ...]
    unopened: tuple[Path, ...]
    """The dealt files no read has opened yet, in the order dealt; one in flight counts as
    opened, so a parallel batch's first read does not pin the order to itself."""

    before: ClassVar[str] = "reading"

    @property
    def batch(self) -> int:
        return self.record.read_batch

    def permits_read(self, path: Path) -> bool:
        """Any file already opened, since re-reading is how a rule gets answered, or one of the
        next unopened batch in the dealt order: a parallel batch's hooks reach the lock in any
        order, so naming only the very next file refused reads the batch had sent in order."""
        return path not in self.unopened or path in self.unopened[:self.batch]

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> Iterator[Self]:
        read = record.read_by(deal)
        remaining = tuple(source for source in record.paths if source not in read)
        if remaining:
            taken = record.opened_by(deal, opened)
            yield cls(record, deal.audit, remaining,
                      tuple(source for source in record.paths if source not in taken))

    def next_batch(self) -> tuple[Path, ...]:
        """The next `batch` files still unread, in the dealt order: a file opened but
        read only in part is named first, and every one named may open now."""
        return self.remaining[:self.batch]

    @property
    def reason(self) -> str:
        """The batch is named on every refusal: told only the next file, sessions read one file
        per call."""
        return (
            f"Read the next batch whole, in one parallel call (batches of {self.batch})."
            " Next batch: "
            + ", ".join(str(source) for source in self.next_batch())
            + f". {len(self.remaining)} dealt files left."
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

    def permits_source_read(self) -> bool:
        """Every dealt file is read, and the probes ask for lines of code: reading the code
        is how they get answered. On 2026-11-09 two sub-agents spent 547k tokens refused,
        because a fresh session must cite a line it was not allowed to read."""
        return True


@dataclass(frozen=True)
class UnreadableAudit(Owed):
    """The audit on disk does not parse, so nothing written in it can answer anything. This is
    owed first and says so, with the parser's own position. On 2026-11-04 one stray brace from an
    edit left the gate reading an empty audit: every saved answer counted for nothing and each
    refusal repeated a question with a blank quote, until the owner had to turn the hooks off."""

    fault: str

    before: ClassVar[str] = "repairing your audit"

    @classmethod
    def owed_by(cls, deal: RuleDeal, record: DealRecord, opened: OpenedReads) -> Iterator[Self]:
        fault = SessionAudit.read(deal.audit).unreadable()
        if fault:
            yield cls(record, deal.audit, fault)

    @property
    def reason(self) -> str:
        shapes = self.record.due.probe_shapes()
        return (f"Nothing in {self.audit} counts until it parses: {self.fault}. Repair that spot"
                " in the file; your saved answers come back with it."
                + (f"\n\nProbe shapes (one object per probe in the probes array,"
                   f' each with a "{Probe.family_discriminator}" field):\n{shapes}'
                   if shapes else ""))

    def permits_read(self, path: Path) -> bool:
        return False


class RuleDeal(State, DealtCalls):
    """One session's deal: the files beside its `stem` that record what it was dealt, what it
    has read in full and the audit it wrote."""

    @property
    def owner_prompts(self) -> int:
        return self.current().owner_prompts

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
    def record(self) -> Path:
        return self.stem.with_suffix(".deal.json")

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

    @property
    def meant(self) -> MeantStore:
        return MeantStore(self.stem.with_suffix(".meant"))

    def voice(self) -> Voice:
        """How this deal speaks as the owner; a deal with no bus seat stays mute."""
        return Mute()

    def delivery(self) -> DealDelivery:
        """How the session receives what it is dealt; a deal that knows no launcher reads."""
        return DealDelivery()

    def audit_written(self) -> None:
        """A successful audit edit settles its exact question, then asks about new rules."""
        record = self.current()
        audit = SessionAudit.read(self.audit)
        if audit.fault():
            return
        def settle(meant: MeantRecord) -> DocumentChange[MeantRecord, tuple[Asked, str] | None]:
            if not meant.answered_by(audit):
                return DocumentChange(meant, None)
            return DocumentChange(meant.settled(audit), (meant.open[0], meant.answer_to_open(audit)))

        settled = self.meant.update(settle)
        meant = self.meant.read()
        voice = self.voice()
        if settled is not None:
            voice.answered(*settled)
        if meant.open:
            voice.escalate(self, audit)
        voice.question(self, audit, audit.answered_probes())

    @property
    def template_shown(self) -> Path:
        return self.stem.with_suffix(".template-shown")

    def template_once(self, due: AuditDue) -> str:
        """The whole template once per deal: on 2026-10-30 a batch of 28 refused reads reprinted
        it 28 times, about 84k tokens, and the deal filled the context it was dealt to restore."""
        try:
            self.template_shown.touch(exist_ok=False)
        except FileExistsError:
            return ""
        return "\n\n".join(part for part in (due.reminder(), due.template(self.audit)) if part)

    def stored(self) -> DealRecord:
        """The deal on disk as it was dealt, before asking whether its sources still exist."""
        try:
            return FieldCodec.decode(DealRecord, FieldCodec.parse(self.record.read_bytes()))
        except FileNotFoundError:
            pass
        except MalformedRecord:
            pass  # A record of another shape names no deal this gate can hold its session to.
        return UndealtRecord()

    def current(self) -> DealRecord:
        """Keep a deal while its sources exist; a retired source requires a fresh deal."""
        return self.stored().while_supplied()

    def state(self) -> ToolGateState:
        return self.current().answer(self, self.dealt_opened.read())

    def configure(self, settings: RuleGateSettings) -> None:
        """Change policy without discarding read evidence, the audit, or the current deal."""
        validate_annotated_dataclass(settings)
        with self.write_guard(self.record):
            self.record.stat()  # Configuration targets an existing session, never fabricates a deal.
            self.publish(settings.applied_to(self.current()))

    def deal(self, dealt: type[DealtSources], due: AuditDue, trigger: DealTrigger) -> None:
        with self.write_guard(self.record):
            trigger.deliver(self.current().mode, lambda: self.replace_deal(dealt, due, trigger), None)

    def replace_deal(self, dealt: type[DealtSources], due: AuditDue,
                     trigger: DealTrigger) -> None:
        """A start or compaction drops what was read: the read log resets first, to what the
        session's delivery already put in front of it (nothing, unless its launcher put the deal
        in its system prompt), then the record is published whole, so no reader sees a
        half-written deal. The audit's answers go with the reads, replaced by the blank skeleton:
        a fresh instance owes a fresh confession, never one carried over from the context it
        lost (plans/GATE-ENGAGEMENT-PLAN.md). A resume deals nothing and keeps both."""
        settings = RuleGateSettings.capture(self.current())
        paths = tuple(source.resolve() for source in dealt.sources())
        held = frozenset(DealtSource.of(path) for path in paths)
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        self.calls.unlink(missing_ok=True)
        self.refusals.unlink(missing_ok=True)
        self.dealt_read.write_text("".join(
            read.line() for read in self.delivery().reads() if read.source in held
        ), encoding="utf-8")
        self.dealt_opened.replace(OpenedReads())
        AtomicFile(self.audit).write_bytes(json.dumps(
            FieldCodec.encode(due.skeleton()), ensure_ascii=False, indent=2).encode())
        self.meant.replace(MeantRecord())
        self.template_shown.unlink(missing_ok=True)
        self.publish(settings.applied_to(DealRecord(paths, due, trigger)))

    def publish(self, record: DealRecord) -> None:
        AtomicFile(self.record).write_bytes(
            json.dumps(FieldCodec.encode(record)).encode()
        )

    def record_read(self, path: Path, span: ReadSpan) -> bool:
        """A completed read covering a whole dealt file counts its content; any other read
        counts nothing."""
        if path not in self.current().paths or not span.covers(path):
            return False
        source = DealtSource.of(path)
        line = DealtRead(source, source.digest()).line()
        with self.write_guard(self.dealt_read):
            with self.dealt_read.open("a", encoding="utf-8") as record:
                first = record.tell() == 0
                record.write(line)
                return first


@dataclass(frozen=True)
class SessionRuleDeal(RuleDeal):
    """A session's persisted deal, independently of its running tool transport."""

    path: Path

    @property
    def stem(self) -> Path:
        return self.path


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
            state = self.state()
            if not state.permits_source_read():
                state.enforce(self, tool)
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

    def blocked(self, cause: Owed, tool: str) -> None:
        template = self.template_once(cause.record.due)
        self.count_call()
        self.refuse(cause.explain_refusal(tool, self.count_refusal(), template))

    @abstractmethod
    def refuse(self, reason: str) -> None:
        """Deliver this refusal to the model the way the backend refuses a tool call."""


class ParsesItsAudit(ToolGate, owes=(UnreadableAudit,)):
    """An audit that does not parse is said out loud before anything else is asked of it."""


class AsksIfMeant(ToolGate, owes=(Unmeant,)):
    """Each batch answered is followed by Tristan's "did you mean it?"; nothing new opens until
    the session says yes or no. A mute deal never asks, so it never owes this."""


class ReadsDealtSources(ToolGate, owes=(Unread,)):
    """Every dealt file is read in full before anything else runs."""


class AuditsDealtRules(ToolGate, owes=(Unaudited,)):
    """After reading, the audit answers every prompt and every owed probe."""


class DealtRuleGate(ParsesItsAudit, AsksIfMeant, ReadsDealtSources, AuditsDealtRules):
    """Read every batch, answer the owner's words and the probes, say whether you meant it:
    the gate every backend runs."""


class Redeals(RuleDeal):
    """A deal its backend renews itself: at the first start, after each compaction, and at
    each declared share of context left, read from the backend's own context-use report."""

    @property
    def refreshes(self) -> tuple[RefreshAt, ...]:
        return self.stored().refreshes

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
        """Every model request reports its context. The gate's answer decides only what a
        refresh still owed does, so a report that owes none never asks for it."""
        if self.refreshes_owed(self.stored(), usage):
            self.state().context_observed(self, usage)

    def refresh_due(self, usage: ContextUsage) -> None:
        """Every refresh the context passed that this compaction has not dealt yet deals once."""
        if self.passed(usage):
            self.reread(RefreshDeal(round(100 * usage.used / usage.size)))

    def refresh_answered(self, usage: ContextUsage) -> None:
        """Every refresh the context passed while the deal is read counts as dealt."""
        self.passed(usage)

    def refreshes_owed(self, record: DealRecord, usage: ContextUsage) -> tuple[str, ...]:
        """The refreshes of this deal the context reached that this compaction has not dealt."""
        reached = record.refreshes_passed(usage)
        if not reached:
            return ()
        done = frozenset(self.refreshed.read_text(encoding="utf-8").split())
        return tuple(refresh.key for refresh in reached if refresh.key not in done)

    def passed(self, usage: ContextUsage) -> tuple[str, ...]:
        """The refreshes the context passed since the last compaction, each recorded once."""
        due = self.refreshes_owed(self.current(), usage)
        with self.refreshed.open("a", encoding="utf-8") as record:
            record.write("".join(f"{key}\n" for key in due))
        return due
