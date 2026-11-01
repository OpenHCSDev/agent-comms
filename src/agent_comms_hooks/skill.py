"""At session start, resume and compaction, write a bound session's sources, order them read
once, and deal the reminder deck.

Every bound collaborator reads the same papers, rules, voice, failure record, shared skill
and NRA pattern catalog. Explicit prime variants select material, never a rank; a rung file
is read when a source verdict cites its pattern ID. Claude Code caps injected hook text at
10,000 characters and the sources are far longer, so they go into parts beside the transcript
and the hook injects the order to read each part, once; the read gate refuses every other tool
until each part is read. The reminder hook then puts one short slice inline every few tool calls.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines
from agent_comms.class_fact import ClassFact
from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import RuleFile
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.held_orders import HeldOrders
from agent_comms.pattern_sources import PostmortemFailure
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.threads import Thread
from agent_comms.comms import Comms
from agent_comms.cli_bridges.shared import Binding
from agent_comms.rule_deal import (
    ClearedDeal, CompactedDeal, CompactionDeal, DealTrigger, DealtSources, PrimedDeal,
    StartedDeal, UnstampedCompactionDeal,
)
from agent_comms.slop import Slop
from agent_comms_hooks import ClaudeSubagentHook
from agent_comms.cli_bridges.session_watch import WatchClaim
from agent_comms.cli_bridges.hook_read_gate import TranscriptWords

class DeckReader(DealtSources):
    """The shared sources every bound collaborator reads, on both native and CLI backends.

    Explicit prime variants change which material is reread, never who may act.
    """

    deck_suffix: ClassVar[str] = ".deck.json"
    """The one shared deck beside each session's transcript."""
    @ClassFact
    def slice_chars(cls: type[DeckReader]) -> int:
        """Whole wake-ups declare the inline packing width; paragraphs split at their lines.

        A rule cannot be cut before its punchline. Its declared text, not a separately
        maintained character count, therefore supplies the width for every reader.
        """
        return max(map(len, cls.tells()))

    @classmethod
    def proofs(cls) -> tuple[Path, ...]:
        """The papers every rule is a consequence of, read before any rule."""
        return CommanderPackage.paper()

    @classmethod
    def indictment(cls) -> tuple[Path, ...]:
        """The failure record every collaborator reads after the proofs and rules."""
        return (CommanderPackage.indictment(),)

    @classmethod
    def rules(cls) -> tuple[RuleFile, ...]:
        return RuleFile.packaged()

    @classmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        """Interleave the shared voice, failure record, rules and failure catalog."""
        indictment = tuple(paragraph for path in cls.indictment()
                           for paragraph in cls.paragraphs(path.read_text(encoding="utf-8")))
        return tuple(stream for stream in (cls.voice(), indictment, cls.tells(), cls.failures())
                     if stream)

    def header(self, start: DeckStart, comms: Comms, snapshot: RegistrySnapshot,
               commanders: tuple[Thread, ...]) -> str:
        """Retained work, not a hierarchy of which reader is permitted to judge it."""
        return start.held(comms, snapshot, commanders)

    def deaf(self, session: str) -> tuple[str, ...]:
        return tuple(deaf.line() for deaf in WatchClaim(session).deaf())

    def board(self, session: str) -> tuple[str, ...]:
        """Read this session's retained board; publication is independently configured."""
        return WatchClaim(session).state().board()

    @classmethod
    def slices(cls) -> list[str]:
        """One slice of each stream per round, so the shortest stream comes back every round
        and the longest runs through once. The failures are the habits caught so far; they are
        called out by name, repeatedly."""
        streams = cls.streams()
        rounds = max(len(stream) for stream in streams)
        return [stream[number % len(stream)] for number in range(rounds) for stream in streams]

    @classmethod
    def voice(cls) -> tuple[str, ...]:
        return cls.paragraphs(CommanderPackage.voice().read_text(encoding="utf-8"))

    @classmethod
    def tells(cls) -> tuple[str, ...]:
        return tuple(rule.reminder for rule in cls.rules())

    @classmethod
    def failures(cls) -> tuple[str, ...]:
        return tuple(f"Recorded failure {line}" for line in PostmortemFailure.catalog().splitlines())

    @classmethod
    def sources(cls) -> tuple[Path, ...]:
        """The closing rule follows every proof, rule and the recorded failures."""
        return (*cls.leading(), *(rule.path for rule in cls.rules() if rule.closes_deal))

    @classmethod
    def leading(cls) -> tuple[Path, ...]:
        """One source order for every collaborator and every backend."""
        return (
            *cls.proofs(),
            *(rule.path for rule in cls.rules() if not rule.closes_deal),
            CommanderPackage.voice(),
            Path(inspect.getfile(PostmortemFailure)),
            CommanderPackage.skill(),
            Slop.catalog() / "README.md",
            *cls.indictment(),
        )

    @classmethod
    def answered(cls) -> frozenset[str]:
        """The shared skill is read and answered under the same audit contract."""
        return super().answered() | {CommanderPackage.skill().stem}

    @classmethod
    def paragraphs(cls, text: str) -> tuple[str, ...]:
        """Blank-line paragraphs; a heading stays with the paragraph it introduces, and a
        paragraph longer than one slice splits at its line breaks."""
        found: list[str] = []
        heading = ""
        for block in (block.strip() for block in text.split("\n\n")):
            if block.startswith("#"):
                heading = f"{heading}{block}\n"
            elif block:
                found.extend(cls.sliced(heading + block))
                heading = ""
        return tuple(found)

    @classmethod
    def sliced(cls, paragraph: str) -> tuple[str, ...]:
        """Consecutive whole lines of a paragraph, each run within `slice_chars`."""
        runs: list[str] = []
        for line in paragraph.splitlines():
            if runs and len(runs[-1]) + 1 + len(line) <= cls.slice_chars:
                runs[-1] += "\n" + line
            else:
                runs.append(line)
        return tuple(runs)


class RulesOnly(DeckReader):
    """A refresh dealt before any compaction: the papers are still in the context word for word,
    so it deals the rules, the voice and the failures again, never the papers a second time."""

    @classmethod
    def proofs(cls) -> tuple[Path, ...]:
        return ()


class IndictmentFree(DeckReader):
    """A prime asked for mid-context without the indictment, so it does not fill the window."""

    @classmethod
    def indictment(cls) -> tuple[Path, ...]:
        return ()


class LightReader(RulesOnly, IndictmentFree):
    """Explicitly reread rules without papers or indictment, independent of authority."""


class Prime(DeclaredFamily):
    """An explicit material selection over the one shared deal, never a role selector."""

    reader: ClassVar[type[DeckReader]]

    @classmethod
    def trigger(cls) -> PrimedDeal:
        """Why this variant's deal was dealt: the owner asked for it by this variant's name."""
        return PrimedDeal(cls.declared_name)


class FullPrime(Prime, declared_name="full"):
    """The same complete source set as a start or compaction."""

    reader = DeckReader


class PaperlessPrime(Prime, declared_name="no-papers"):
    reader = RulesOnly


class IndictmentlessPrime(Prime, declared_name="no-indictment"):
    reader = IndictmentFree


class LightPrime(Prime, declared_name="light"):
    reader = LightReader


@dataclass(frozen=True)
class DealtDeck(TranscriptWords):
    """The deal and the slices the session start hook dealt beside the transcript."""

    def deck(self, reader: DeckReader) -> Path:
        return self.stem.with_suffix(reader.deck_suffix)


class StartReason(DeclaredFamily):
    """Why Claude Code started the session; a member declares Claude Code's own spelling."""

    def deal(self, start: DeckStart, reader: DeckReader, comms: Comms,
             snapshot: RegistrySnapshot) -> tuple[str, ...]:
        """The context is gone: the deck is dealt from its start and every file is read again."""
        trigger = self.trigger(start)
        return trigger.deliver(
            start.current().mode, lambda: start.dealt(reader, trigger, comms, snapshot), (),
        )

    def trigger(self, start: DeckStart) -> DealTrigger:
        """The cause every refusal of this deal opens with."""
        return StartedDeal()


class LostContext(StartReason):
    """A first start, or any start Claude Code names that no member declares: what the session
    read is no longer in front of it."""


class Compaction(StartReason, declared_name="compact"):
    """A summary replaced the context. The fresh context cannot remember it, so the deal says
    when, from the transcript's own compact boundary."""

    def trigger(self, start: DeckStart) -> DealTrigger:
        return start.compaction()


class Clear(StartReason, declared_name="clear"):
    """The session was cleared."""

    def trigger(self, start: DeckStart) -> DealTrigger:
        return ClearedDeal()


class Resume(StartReason, declared_name="resume"):
    """Claude Code reattaches a session with its context intact. On 2026-10-30 a resume at
    07:17 re-dealt claude-code mid-gate: its answered audit was unlinked and 23 read files
    were demanded again."""

    def deal(self, start: DeckStart, reader: DeckReader, comms: Comms,
             snapshot: RegistrySnapshot) -> tuple[str, ...]:
        return start.current().resume(lambda: start.dealt(reader, self.trigger(start), comms, snapshot), ())


class SessionSource(str):
    """Claude Code's name for why a session started, as its hook record spells it."""

    def reason(self) -> StartReason:
        return StartReason.member_or_default(self, LostContext)()


@dataclass(frozen=True)
class DeckStart(DealtDeck):
    """A session start, resume or compaction: the deal, its order and the deck."""

    source: SessionSource = field(default=SessionSource("startup"), kw_only=True)

    @classmethod
    def of(cls, record: Mapping[str, object], **context: object) -> DeckStart:
        return (SubagentDeckStart if cls.raised_by_subagent(record) else cls).decode(record, **context)

    def take(self, comms: Comms, binding: Binding, number: int = 0) -> tuple[str, ...]:
        """The captured resident identity admits the shared reader at one registry cut."""
        snapshot = comms.registry.snapshot()
        snapshot.born(binding.owner.require_recorded().lookup)
        return self.taken(DeckReader(), number, comms, snapshot)

    def taken(self, reader: DeckReader, number: int, comms: Comms,
              snapshot: RegistrySnapshot) -> tuple[str, ...]:
        return self.source.reason().deal(self, reader, comms, snapshot)

    def primed(self, prime: type[Prime], comms: Comms, binding: Binding) -> tuple[str, ...]:
        """The owner-selected variant, dealt through the same bound resident context."""
        snapshot = comms.registry.snapshot()
        snapshot.born(binding.owner.require_recorded().lookup)
        return self.dealt(prime.reader(), prime.trigger(), comms, snapshot)

    def dealt(self, reader: DeckReader, trigger: DealTrigger, comms: Comms,
              snapshot: RegistrySnapshot) -> tuple[str, ...]:
        """The order to read every dealt file, once, by its packaged path, opening with why."""
        prompts = self.prompts()
        self.prepare(reader, prompts, trigger)
        sources = reader.sources()
        commanders = snapshot.commander_project.leads(snapshot)
        return (
            trigger.reason() + " " + self.notice(commanders) + f" Read each of these {len(sources)} files in full with the Read tool before any other"
            " action: they are every rule you follow and the sources they come from. A summary"
            " or memory of them does not count as reading them, and every other tool is refused"
            " until each one is read. Read them in order, at most four unanswered rule files at"
            " a time, and answer those in the audit: reading alone is passive. Every tool but"
            " Read, and Write or Edit of"
            f" {self.audit}, is refused until it answers. This order is given once per session"
            " start or compaction; a short slice of them comes back inline every few tool calls.\n"
            + "\n".join(str(source) for source in sources),
            self.template_once(self.current().due),
            self.owner_words(prompts),
            reader.header(self, comms, snapshot, commanders),
        )

    def owner_words(self, prompts: tuple[str, ...]) -> str:
        """The person's own last prompts, verbatim from the transcript: the summary turned them
        into its paraphrase and a next step, and on 2026-10-29 the session followed the
        paraphrase while his words sat unanswered."""
        return "\n\n".join((
            "WHAT TRISTAN ACTUALLY SAID LAST, verbatim, oldest first. Not your summary of it."
            " Your audit answers each one, and your next action serves them, not the summary's"
            " next step:",
            *(f"> {words}" for words in prompts),
        ))

    def compaction(self) -> CompactionDeal:
        """The compaction, timed by the newest compact boundary in the transcript."""
        transcript = Path(self.transcript_path)
        lines = ClaudeJsonLines()
        with transcript.open("rb") as stream:
            for offset, raw in lines.reverse_lines(stream, transcript.stat().st_size):
                for moment in lines.decode_line(raw, offset).compacted_at():
                    return CompactedDeal(
                        datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds"))
        return UnstampedCompactionDeal()

    def prompts(self) -> tuple[str, ...]:
        """A new session starts before Claude Code writes its transcript: nobody has spoken."""
        transcript = Path(self.transcript_path)
        if not transcript.exists():
            return ()
        with transcript.open("rb") as stream:
            return ClaudeJsonLines().owner_words(
                stream, transcript.stat().st_size, self.owner_prompts)

    def held(self, comms: Comms, snapshot: RegistrySnapshot,
             commanders: tuple[Thread, ...]) -> str:
        """Held orders from the same bus and cut that selected the reader."""
        return "\n".join((
            "FUCK YOUR COMPACTION. Your summary is a paraphrase. What the shared board holds:",
            *(hold.recalled() for commander in commanders
              for hold in HeldOrders.of(comms.goals.todos, snapshot).live),
        ))

    def prepare(self, reader: DeckReader, prompts: tuple[str, ...], trigger: DealTrigger) -> None:
        """The deal, and the deck dealt again from its start."""
        self.deal(type(reader), prompts, trigger)
        self.deck(reader).write_text(json.dumps(reader.slices()), encoding="utf-8")

    def widened(self, reader: DeckReader, comms: Comms,
                snapshot: RegistrySnapshot) -> tuple[str, ...]:
        """A session newly bound after startup receives the shared sources. Retained reads
        and audit survive when the context remains; changing a tag or storage lead adds no
        second deck or rank-specific source set."""
        deck = self.deck(reader)
        if deck.exists():
            return ()
        said = self.current().widen(
            self, type(reader), lambda: self.dealt(reader, self.source.reason().trigger(self), comms, snapshot), ())
        deck.write_text(json.dumps(reader.slices()), encoding="utf-8")
        return said

    @staticmethod
    def notice(commanders: tuple[Thread, ...]) -> str:
        names = [thread.name for thread in commanders]
        return ("Storage lead: " + ", ".join(names) + ". All collaborators share responsibility." if names
                else "No project storage lead is registered for this session.")


@dataclass(frozen=True)
class SubagentDeckStart(DeckStart, ClaudeSubagentHook):
    """A subagent's compaction deals the subagent's own deck: on 2026-10-30 a commander fork's
    compaction dealt the main agent's stem, whose audit was then mid-answer."""
