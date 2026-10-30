"""Resident session-start renderer for the shared reader and rule-deal owners."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, TYPE_CHECKING

from ..comms import Comms
from ..held_orders import HeldOrders
from ..instruction_deck import DealtCalls, DeckReader
from ..rule_deal import RuleDeal
from ..todos import TodoStore
from .claude_transcript import ClaudeJsonLines
from .shared import Binding

if TYPE_CHECKING:
    from ..registry_document import RegistrySnapshot
    from ..threads import Thread


@dataclass(frozen=True)
class DealtDeck(DealtCalls, RuleDeal):
    """Claude's inline slices beside the shared session deal and call record."""

    def deck(self, reader: DeckReader) -> Path:
        return Path(self.transcript_path).with_suffix(reader.deck_suffix)


@dataclass(frozen=True)
class DeckStart(DealtDeck):
    owner_prompts: ClassVar[int] = 6
    """How many of the person's own last prompts accompany the deal."""

    def take(self, comms: Comms, binding: Binding, number: int = 0) -> tuple[str, ...]:
        """The resident's actual recipient selects its reader from one registry cut."""
        snapshot = comms.registry.snapshot()
        reader = DeckReader.for_incarnation(snapshot, binding.recipient(comms))
        return self.taken(reader, number, comms, snapshot)

    def taken(self, reader: DeckReader, number: int, comms: Comms,
              snapshot: RegistrySnapshot) -> tuple[str, ...]:
        """A session start deals the deck."""
        return self.dealt(reader, comms, snapshot)

    def dealt(self, reader: DeckReader, comms: Comms,
              snapshot: RegistrySnapshot) -> tuple[str, ...]:
        """Render the reader's order and the shared deal's actual audit requirements."""
        prompts = self.prompts()
        self.prepare(reader, prompts)
        commanders = tuple(snapshot.commander_project.commanders(snapshot))
        return (
            self.notice(commanders) + " " + reader.order(reader.sources()),
            self.owner_words(prompts),
            reader.header(lambda: self.held(comms, snapshot, commanders)),
            *self.current().reminders(self.audit),
        )

    def owner_words(self, prompts: tuple[str, ...]) -> str:
        """The owner's actual words, not the compaction summary's paraphrase."""
        return "\n\n".join((
            "WHAT TRISTAN ACTUALLY SAID LAST, verbatim, oldest first. Not your summary of it."
            " Your audit answers each one, and your next action serves them, not the summary's"
            " next step:",
            *(f"> {words}" for words in prompts),
        ))

    def prompts(self) -> tuple[str, ...]:
        """A new session starts before Claude Code writes its transcript."""
        transcript = Path(self.transcript_path)
        if not transcript.exists():
            return ()
        with transcript.open("rb") as stream:
            return ClaudeJsonLines().owner_words(
                stream, transcript.stat().st_size, self.owner_prompts)

    def held(self, comms: Comms, snapshot: RegistrySnapshot,
             commanders: tuple[Thread, ...]) -> tuple[str, ...]:
        """Complete held-order lines; the reader owns their header and rendering."""
        return tuple(
            hold.recalled()
            for commander in commanders
            for hold in HeldOrders.of(TodoStore(comms.root), snapshot, commander.name).live
        )

    def prepare(self, reader: DeckReader, prompts: tuple[str, ...]) -> None:
        """The shared deal owns read/audit state; Claude retains only its inline slices."""
        self.deal(type(reader), prompts)
        self.deck(reader).write_text(json.dumps(reader.slices()), encoding="utf-8")

    @staticmethod
    def notice(commanders: tuple[Thread, ...]) -> str:
        names = [thread.name for thread in commanders]
        return ("Commander: " + ", ".join(names) + "." if names
                else "No commander project is registered for this session.")
