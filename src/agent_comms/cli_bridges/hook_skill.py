"""Resident Claude skill hook behavior, behind the existing session socket."""
from __future__ import annotations

import inspect
import json
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import (
    ClassVar,
    TYPE_CHECKING,
)

from agent_comms_hooks import ClaudeTranscriptHook

from .claude_transcript import ClaudeJsonLines
from .session_watch import WatchClaim
from ..commander.package import CommanderPackage
from ..commander.rule_file import (
    CommanderAudience,
    FleetAudience,
    RuleAudience,
    RuleFile,
)
from ..held_orders import HeldOrders
from ..pattern_sources import PostmortemFailure
from ..registry_store import RegistryStore
from ..slop import Slop
from ..todos import TodoStore

if TYPE_CHECKING:
    from ..registry_document import RegistrySnapshot
    from ..threads import Thread


class DeckReader(ABC):
    """Which packaged sources a bound session is dealt, by the audience its rules declare.

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
        cls.audiences = (*cls.audiences, audience)

    @classmethod
    def rules(cls) -> tuple[RuleFile, ...]:
        return tuple(rule for rule in RuleFile.packaged() if rule.audience in cls.audiences)

    @classmethod
    @abstractmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        """The slice streams this reader's deck interleaves, one slice of each per round."""

    @classmethod
    @abstractmethod
    def sources(cls) -> tuple[Path, ...]:
        """The packaged files this reader is ordered to read in full after a start or
        compaction: the rules themselves, never a copy of them."""

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
    """A bound session the registry does not name commander: the fleet rules, the voice they
    are said in and the failures already caught."""

    deck_suffix = ".fleet-deck.json"

    @classmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        return (cls.voice(), cls.tells(), cls.failures())

    @classmethod
    def sources(cls) -> tuple[Path, ...]:
        return (
            CommanderPackage.voice(),
            Path(inspect.getfile(PostmortemFailure)),
            *(rule.path for rule in cls.rules()),
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
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        indictment = cls.paragraphs(CommanderPackage.indictment().read_text(encoding="utf-8"))
        return (cls.voice(), indictment, cls.tells(), cls.failures())

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

    def header(self, start: DeckStart) -> str:
        return start.held()

    def deaf(self, session: str) -> tuple[str, ...]:
        return tuple(deaf.line() for deaf in WatchClaim(session).deaf())

    def board(self, session: str) -> tuple[str, ...]:
        return WatchClaim(session).state().board()


@dataclass(frozen=True)
class DealtDeck(ClaudeTranscriptHook):
    """The slices the session start hook dealt beside the transcript, and the calls since."""

    def deck(self, reader: DeckReader) -> Path:
        return Path(self.transcript_path).with_suffix(reader.deck_suffix)


@dataclass(frozen=True)
class DeckStart(DealtDeck):
    owner_prompts: ClassVar[int] = 6
    """How many of the person's own last prompts the deal puts back in front of the session."""

    def take(self) -> tuple[str, ...]:
        """What this hook says: nothing to a session no bus binding names, the fleet's deck to
        a bound session, and the commander's deck to the one the registry names commander now."""
        return WatchClaim(self.session_id).state().by_role(
            unbound=tuple, fleet=lambda record: self.taken(FleetReader()),
            commander=lambda record: self.taken(CommanderReader()))

    def taken(self, reader: DeckReader) -> tuple[str, ...]:
        """A session start deals the deck."""
        return self.dealt(reader)

    def dealt(self, reader: DeckReader) -> tuple[str, ...]:
        """The order to read every dealt file, once, by its packaged path."""
        prompts = self.prompts()
        self.prepare(reader, prompts)
        sources = reader.sources()
        return (
            self.notice() + f" Read each of these {len(sources)} files in full with the Read tool before any other"
            " action: they are every rule you follow and the sources they come from. A summary"
            " or memory of them does not count as reading them, and every other tool is refused"
            " until each one is read. Then, before your next action, check what you did since"
            " your last start or compaction against them and say, one line each, every rule you"
            " broke and the action that broke it, and fix what you can: reading alone is passive,"
            " the audit is what finds the breaks (on 2026-10-27 one reread found six broken rules"
            " in one session). That audit is not optional: every tool but Read, and Write of"
            f" {self.audit}, is refused until the audit is written there as JSON:"
            ' {"rules": [{"rule": NAME, "broke": [each action since the last start or'
            ' compaction that broke it; empty means kept]}, one entry for every rule you were'
            ' dealt], "owner_words": [{"words": EACH PROMPT QUOTED BELOW, "doing": what you are'
            " doing about it right now}]}. This order is given once per session start or"
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
        """A start or compaction drops what was read: the deck is dealt again from its start,
        and no file counts as read until it is read again."""
        self.calls.unlink(missing_ok=True)
        self.dealt_read.write_text("", encoding="utf-8")
        self.audit.unlink(missing_ok=True)
        self.audit_due.write_text(json.dumps({
            "rules": [rule.name for rule in reader.rules()],
            "owner_words": list(prompts),
        }), encoding="utf-8")
        self.deck(reader).write_text(json.dumps(reader.slices()), encoding="utf-8")
        self.dealt_files.write_text(
            "".join(f"{source.resolve()}\n" for source in reader.sources()), encoding="utf-8")

    def notice(self) -> str:
        names = [thread.name for _, _, thread in self.commanders()]
        return ("Commander: " + ", ".join(names) + "." if names
                else "No commander project is registered for this session.")
