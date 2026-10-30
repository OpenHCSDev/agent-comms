"""At session start, resume and compaction, write a bound session's sources, order them read
once, and deal the reminder deck.

Every session bound to a bus is dealt the fleet's sources: paper1 first, the proof the rules are
consequences of, then the fleet rules in their reading order, the voice and the recorded
failures. The session the registry names commander is also dealt the indictment, the
commander skill, the commander's own rules and NRA's pattern catalog index; a rung file
is read when an order or roast cites its pattern ID. Claude Code caps injected hook text at
10,000 characters and the sources are far longer, so they go into parts beside the transcript
and the hook injects the order to read each part, once; the read gate refuses every other tool
until each part is read. The reminder hook then puts one short slice inline every few tool calls.
"""

from __future__ import annotations

import inspect
import json
import sys
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines
from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import CommanderAudience, FleetAudience, RuleAudience, RuleFile
from agent_comms.declared_family import DeclaredFamily
from agent_comms.held_orders import HeldOrders
from agent_comms.pattern_sources import PostmortemFailure
from agent_comms.registry_store import RegistryStore
from agent_comms.rule_deal import DealtSources, RuleDeal
from agent_comms.slop import Slop
from agent_comms.todos import TodoStore
from agent_comms_hooks import ClaudeHookProgram, ClaudeSubagentHook, ClaudeTranscriptHook
from agent_comms_hooks.watch import WatchClaim

if TYPE_CHECKING:
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.threads import Thread


class DeckReader(DealtSources):
    """Which packaged sources a bound session is dealt, by the audience its rules declare, on
    every backend: the Claude hooks and Core's Pi deal both read this one list.

    Every session bound to a bus follows the fleet rules; the session the registry names
    commander also follows the commander's. RuleFile's audience is the one owner of that split.
    """

    audiences: ClassVar[tuple[type[RuleAudience], ...]] = ()
    """Its own audience after every audience its base readers serve."""
    deck_suffix: ClassVar[str]
    """Where this reader's deck sits beside the transcript: a session whose role changes finds
    no deck of its new kind and is dealt again."""
    slice_chars: ClassVar[int] = 2_500
    """The most characters one inline slice carries; a longer paragraph splits at its lines. A
    rule's wake-up is never split, since a greentext cut before its punchline teaches nothing,
    so the cap sits above the longest one and the deck test fails when one outgrows it."""

    def __init_subclass__(cls, audience: type[RuleAudience], **options: object) -> None:
        super().__init_subclass__(**options)
        cls.audiences = tuple(dict.fromkeys((*cls.audiences, audience)))

    @classmethod
    def proofs(cls) -> tuple[Path, ...]:
        """The papers every rule is a consequence of, read before any rule."""
        return CommanderPackage.paper()

    @classmethod
    def indictment(cls) -> tuple[Path, ...]:
        """The record of what the reader actually did; a fleet session is dealt none."""
        return ()

    @classmethod
    def rules(cls) -> tuple[RuleFile, ...]:
        return tuple(rule for rule in RuleFile.packaged() if rule.audience in cls.audiences)

    @classmethod
    @abstractmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        """The slice streams this reader's deck interleaves, one slice of each per round."""

    @abstractmethod
    def header(self, start: DeckStart) -> str:
        """The line after the read order: what the summary cannot be trusted to hold."""

    def deaf(self, session: str) -> tuple[str, ...]:
        """A fleet session is not told to re-arm the commander's watch."""
        return ()

    def board(self, session: str) -> tuple[str, ...]:
        """A fleet session has no commander board."""
        return ()

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


class FleetReader(DeckReader, audience=FleetAudience):
    """A bound session the registry does not name commander: the paper the rules derive from,
    the fleet rules, the voice they are said in and the failures already caught."""

    deck_suffix = ".fleet-deck.json"

    @classmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        return (cls.voice(), cls.tells(), cls.failures())

    @classmethod
    def sources(cls) -> tuple[Path, ...]:
        """Proof first, then the rules it makes binding, then how they are said and broken."""
        return (
            *cls.proofs(),
            *(rule.path for rule in cls.rules()),
            CommanderPackage.voice(),
            Path(inspect.getfile(PostmortemFailure)),
        )

    def header(self, start: DeckStart) -> str:
        return ("FUCK YOUR COMPACTION. Your summary is a paraphrase of these rules, and a"
                " paraphrase drops the exact words they turn on. The files above are the source;"
                " where your summary disagrees with them, the summary is wrong.")


class CommanderReader(FleetReader, audience=CommanderAudience):
    """The session the registry names commander: the fleet's sources plus the indictment, the
    commander skill, the commander's own rules and the slop catalog's index."""

    deck_suffix = ".deck.json"

    @classmethod
    def indictment(cls) -> tuple[Path, ...]:
        return (CommanderPackage.indictment(),)

    @classmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        indictment = tuple(paragraph for path in cls.indictment()
                           for paragraph in cls.paragraphs(path.read_text(encoding="utf-8")))
        return tuple(stream for stream in (cls.voice(), indictment, cls.tells(), cls.failures())
                     if stream)

    @classmethod
    def sources(cls) -> tuple[Path, ...]:
        """The fleet's files, its own rules among them by audience, then the commander's."""
        return (*super().sources(), *cls.indictment(), CommanderPackage.skill(),
                Slop.catalog() / "README.md")

    def header(self, start: DeckStart) -> str:
        return start.held()

    def deaf(self, session: str) -> tuple[str, ...]:
        return tuple(deaf.line() for deaf in WatchClaim(session).deaf())

    def board(self, session: str) -> tuple[str, ...]:
        return WatchClaim(session).state().board()


class RulesOnly(DeckReader, audience=FleetAudience):
    """A refresh dealt before any compaction: the papers are still in the context word for word,
    so it deals the rules, the voice and the failures again, never the papers a second time."""

    @classmethod
    def proofs(cls) -> tuple[Path, ...]:
        return ()


class FleetRulesReader(RulesOnly, FleetReader, audience=FleetAudience):
    """A fleet session's refresh."""


class CommanderRulesReader(RulesOnly, CommanderReader, audience=CommanderAudience):
    """The commander's refresh."""


class IndictmentFree(DeckReader, audience=FleetAudience):
    """A prime asked for mid-context without the indictment, so it does not fill the window."""

    @classmethod
    def indictment(cls) -> tuple[Path, ...]:
        return ()


class CommanderIndictmentFreeReader(IndictmentFree, CommanderReader, audience=CommanderAudience):
    """The commander's papers and rules without the indictment."""


class CommanderLightReader(RulesOnly, IndictmentFree, CommanderReader, audience=CommanderAudience):
    """The commander's rules alone: no papers, no indictment."""


class Prime(DeclaredFamily):
    """A deal the owner asks for on demand (/prime, `agent-comms-prime`). Each member names
    the readers it deals by role; the readers own which files those are, so a variant is a
    selection over the one dealt order, never a second list."""

    fleet: ClassVar[type[DeckReader]]
    commander: ClassVar[type[DeckReader]]


class FullPrime(Prime, declared_name="full"):
    """Papers, rules and, for a commander, the indictment: the same deal as a compaction."""

    fleet = FleetReader
    commander = CommanderReader


class PaperlessPrime(Prime, declared_name="no-papers"):
    fleet = FleetRulesReader
    commander = CommanderRulesReader


class IndictmentlessPrime(Prime, declared_name="no-indictment"):
    fleet = FleetReader
    commander = CommanderIndictmentFreeReader


class LightPrime(Prime, declared_name="light"):
    fleet = FleetRulesReader
    commander = CommanderLightReader


@dataclass(frozen=True)
class DealtDeck(ClaudeTranscriptHook, RuleDeal):
    """The deal and the slices the session start hook dealt beside the transcript."""

    def deck(self, reader: DeckReader) -> Path:
        return self.stem.with_suffix(reader.deck_suffix)


class StartReason(DeclaredFamily):
    """Why Claude Code started the session; a member declares Claude Code's own spelling."""

    def deal(self, start: DeckStart, reader: DeckReader) -> tuple[str, ...]:
        """The context is gone: the deck is dealt from its start and every file is read again."""
        return start.dealt(reader)


class LostContext(StartReason):
    """A first start, a compaction or a clear: what the session read is no longer in front of it."""


class Resume(StartReason, declared_name="resume"):
    """Claude Code reattaches a session with its context intact. On 2026-10-30 a resume at
    07:17 re-dealt claude-code mid-gate: its answered audit was unlinked and 23 read files
    were demanded again."""

    def deal(self, start: DeckStart, reader: DeckReader) -> tuple[str, ...]:
        return start.current().resume(lambda: start.dealt(reader), ())


class SessionSource(str):
    """Claude Code's name for why a session started, as its hook record spells it."""

    def reason(self) -> StartReason:
        return StartReason.member_or_default(self, LostContext)()


@dataclass(frozen=True)
class DeckStart(DealtDeck):
    """A session start, resume or compaction: the deal, its order and the deck."""

    source: SessionSource = SessionSource("startup")

    @classmethod
    def of(cls, record: Mapping[str, object]) -> DeckStart:
        return (SubagentDeckStart if cls.raised_by_subagent(record) else cls).decode(record)

    def take(self) -> tuple[str, ...]:
        """What this hook says: nothing to a session no bus binding names, the fleet's deck to
        a bound session, and the commander's deck to the one the registry names commander now."""
        return WatchClaim(self.session_id).state().by_role(
            unbound=tuple, fleet=lambda record: self.taken(FleetReader()),
            commander=lambda record: self.taken(CommanderReader()))

    def taken(self, reader: DeckReader) -> tuple[str, ...]:
        """A start that lost the context deals the deck; a resume keeps the deal it holds."""
        return self.source.reason().deal(self, reader)

    def primed(self, prime: type[Prime]) -> tuple[str, ...]:
        """The owner's /prime: a fresh deal of the variant's reader for this session's role,
        whatever its context holds, through the same gate as a compaction."""
        return WatchClaim(self.session_id).state().by_role(
            unbound=tuple, fleet=lambda record: self.dealt(prime.fleet()),
            commander=lambda record: self.dealt(prime.commander()))

    def dealt(self, reader: DeckReader) -> tuple[str, ...]:
        """The order to read every dealt file, once, by its packaged path."""
        prompts = self.prompts()
        self.prepare(reader, prompts)
        sources = reader.sources()
        return (
            self.notice() + f" Read each of these {len(sources)} files in full with the Read tool before any other"
            " action: they are every rule you follow and the sources they come from. A summary"
            " or memory of them does not count as reading them, and every other tool is refused"
            " until each one is read. After each rule file, before the next unread file opens,"
            " answer it in the audit: greentext lines it holds quoted verbatim, what they did, and why"
            " it holds. Check what you did since your last start or compaction against it and"
            " name every action that broke it, and fix what you can: reading alone is passive, the"
            " audit is what finds the breaks (on 2026-10-27 one reread found six broken rules in"
            " one session). That audit is not optional: every tool but Read, and Write or Edit of"
            f" {self.audit}, is refused until the audit is written there as JSON:"
            ' {"rules": [{"rule": NAME, "quote": WHOLE GREENTEXT LINES OF IT VERBATIM, "does": what they did,'
            ' "why": why it holds, "broke": [each action since the last start or compaction that'
            ' broke it; empty means kept]}, one entry for every rule you were dealt],'
            ' "owner_words": [{"words": EACH PROMPT QUOTED BELOW, "doing": what you are doing about'
            " it right now}]}. This order is given once per session start or"
            " compaction; a short slice of them comes back inline every few tool calls.\n"
            + "\n".join(str(source) for source in sources),
            self.owner_words(prompts),
            reader.header(self),
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

    def prompts(self) -> tuple[str, ...]:
        """A new session starts before Claude Code writes its transcript: nobody has spoken."""
        transcript = Path(self.transcript_path)
        if not transcript.exists():
            return ()
        with transcript.open("rb") as stream:
            return ClaudeJsonLines().owner_words(
                stream, transcript.stat().st_size, self.owner_prompts)

    def held(self) -> str:
        """Every order the units hold, from the Todo store: a summary is a paraphrase."""
        return "\n".join((
            "FUCK YOUR COMPACTION. Your summary is a paraphrase. What your units actually hold:",
            *(hold.recalled()
              for root, snapshot, commander in self.commanders()
              for hold in HeldOrders.of(TodoStore(root), snapshot, commander.name).live),
        ))

    def commanders(self) -> Iterator[tuple[Path, RegistrySnapshot, Thread]]:
        """Each commander registered on a bus this session is bound to, with that bus's cut."""
        for record in WatchClaim(self.session_id).state().bindings():
            root = Path(record.root)
            snapshot = RegistryStore.at(root).snapshot()
            for thread in snapshot.commander_project.commanders(snapshot):
                yield root, snapshot, thread

    def prepare(self, reader: DeckReader, prompts: tuple[str, ...]) -> None:
        """The deal, and the deck dealt again from its start."""
        self.deal(type(reader), prompts)
        self.deck(reader).write_text(json.dumps(reader.slices()), encoding="utf-8")

    def widened(self, reader: DeckReader) -> tuple[str, ...]:
        """The deck of this session's role is the one record of the role it was dealt. A
        session whose role changed finds none of its kind and is dealt only the files the role
        adds, keeping what it read and wrote, since its context was never lost; a session bound
        after it started is dealt in full and told so."""
        deck = self.deck(reader)
        if deck.exists():
            return ()
        said = self.current().widen(self, type(reader), lambda: self.dealt(reader), ())
        deck.write_text(json.dumps(reader.slices()), encoding="utf-8")
        return said

    def notice(self) -> str:
        names = [thread.name for _, _, thread in self.commanders()]
        return ("Commander: " + ", ".join(names) + "." if names
                else "No commander project is registered for this session.")


@dataclass(frozen=True)
class SubagentDeckStart(DeckStart, ClaudeSubagentHook):
    """A subagent's compaction deals the subagent's own deck: on 2026-10-30 a commander fork's
    compaction dealt the main agent's stem, whose audit was then mid-answer."""


class SkillText(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    start = DeckStart.of(json.load(sys.stdin))
    start.warn(*start.take())


if __name__ == "__main__":
    main()
