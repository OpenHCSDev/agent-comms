"""At session start, resume and compaction, write a bound session's sources, order them read
once, and deal the reminder deck.

Every session bound to a bus is dealt the fleet's sources: the voice, the recorded failures and
the fleet rules. The session the registry names commander is also dealt the indictment, the
commander skill, the commander's own rules and the refactor-audit catalog's index; a rung file
is read when an order or roast cites its pattern ID. Claude Code caps injected hook text at
10,000 characters and the sources are far longer, so they go into parts beside the transcript
and the hook injects the order to read each part, once; the read gate refuses every other tool
until each part is read. The reminder hook then puts one short slice inline every few tool calls.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import CommanderAudience, FleetAudience, RuleAudience, RuleFile
from agent_comms.held_orders import HeldOrders
from agent_comms.pattern_sources import PostmortemFailure
from agent_comms.registry_store import RegistryStore
from agent_comms.slop import Slop
from agent_comms.todos import TodoStore
from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook
from agent_comms_hooks.watch import WatchClaim

if TYPE_CHECKING:
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.threads import Thread


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
    def sources(cls, notice: str) -> str:
        """Everything this reader is ordered to read in full after a start or compaction."""

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
    def sources(cls, notice: str) -> str:
        return "\n\n".join((
            notice,
            CommanderPackage.voice().read_text(encoding="utf-8", newline=""),
            "# Recorded failures\n\n" + PostmortemFailure.catalog(),
            *(rule.render() for rule in cls.rules()),
        ))

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
    def sources(cls, notice: str) -> str:
        return "\n\n".join((
            notice,
            CommanderPackage.voice().read_text(encoding="utf-8", newline=""),
            CommanderPackage.indictment().read_text(encoding="utf-8", newline=""),
            "# Recorded failures\n\n" + PostmortemFailure.catalog(),
            RuleFile.document(CommanderPackage.skill())[1],
            *(rule.render() for rule in cls.rules()),
            (Slop.catalog() / "README.md").read_text(encoding="utf-8", newline=""),
        ))

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

    @property
    def calls(self) -> Path:
        """One byte per tool call since the deck was dealt. An append is atomic, so tool
        calls that run in parallel each count once."""
        return Path(self.transcript_path).with_suffix(".calls")


@dataclass(frozen=True)
class DeckStart(DealtDeck):
    part_chars: ClassVar[int] = 60_000
    """The most characters one Read of a part returns in full."""

    def take(self) -> tuple[str, ...]:
        """What this hook says: nothing to a session no bus binding names, the fleet's deck to
        a bound session, and the commander's deck to the one the registry names commander now."""
        return WatchClaim(self.session_id).state().by_role(
            unbound=tuple, fleet=lambda: self.taken(FleetReader()),
            commander=lambda: self.taken(CommanderReader()))

    def taken(self, reader: DeckReader) -> tuple[str, ...]:
        """A session start deals the deck."""
        return self.dealt(reader)

    def dealt(self, reader: DeckReader) -> tuple[str, ...]:
        """The order to read every part, once; the deck carries the sources after it."""
        self.prepare(reader)
        parts = sorted(self.parts.iterdir())
        return (
            f"Read each of these {len(parts)} files in full with the Read tool before any other"
            " action: they are every rule you follow and the sources they come from. A summary"
            " or memory of them does not count as reading them, and every other tool is refused"
            " until each one is read. Then, before your next action, check what you did since"
            " your last start or compaction against them and say, one line each, every rule you"
            " broke and the action that broke it, and fix what you can: reading alone is passive,"
            " the audit is what finds the breaks (on 2026-10-27 one reread found six broken rules"
            " in one session). This order is given once per session start or compaction;"
            " a short slice of them comes back inline every few tool calls.\n"
            + "\n".join(str(part) for part in parts),
            reader.header(self),
        )

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

    def prepare(self, reader: DeckReader) -> None:
        """A start or compaction drops what was read: the deck is dealt again from its start,
        and no part counts as read until it is read again."""
        self.calls.unlink(missing_ok=True)
        self.parts_read.write_text("", encoding="utf-8")
        self.deck(reader).write_text(json.dumps(reader.slices()), encoding="utf-8")
        self.parts.mkdir(exist_ok=True)
        for old in self.parts.iterdir():
            old.unlink()
        chunks: list[list[str]] = [[]]
        size = 0
        for line in reader.sources(self.notice()).splitlines(keepends=True):
            if size + len(line) > self.part_chars and chunks[-1]:
                chunks.append([])
                size = 0
            chunks[-1].append(line)
            size += len(line)
        for number, chunk in enumerate(chunks, 1):
            (self.parts / f"{number:02}.md").write_text("".join(chunk), encoding="utf-8")

    def notice(self) -> str:
        names = [thread.name for _, _, thread in self.commanders()]
        return ("Commander: " + ", ".join(names) + "." if names
                else "No commander project is registered for this session.")


class SkillText(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    start = DeckStart.read()
    for text in start.take():
        start.warn(text)


if __name__ == "__main__":
    main()
