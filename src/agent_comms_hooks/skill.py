"""Deal Core's rule sources at Claude session start and compaction."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.held_orders import HeldOrders
from agent_comms.instruction_deck import (
    CommanderReader as CoreCommanderReader, DeckParts, DeckReader,
    FleetReader as CoreFleetReader,
)
from agent_comms.registry_store import RegistryStore
from agent_comms.todos import TodoStore
from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook
from agent_comms_hooks.watch import WatchClaim

if TYPE_CHECKING:
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.threads import Thread


class HookReader(DeckReader):
    """Hook-only reminders belong beside the bridge, not in Core's source reader."""

    def deaf(self, session: str) -> tuple[str, ...]:
        return ()

    def board(self, session: str) -> tuple[str, ...]:
        return ()


class FleetReader(HookReader, CoreFleetReader):
    pass


class CommanderReader(CoreCommanderReader, FleetReader):
    def deaf(self, session: str) -> tuple[str, ...]:
        return tuple(deaf.line() for deaf in WatchClaim(session).deaf())

    def board(self, session: str) -> tuple[str, ...]:
        return WatchClaim(session).state().board()


@dataclass(frozen=True)
class DealtDeck(ClaudeTranscriptHook):
    """The slices dealt beside the transcript, and the calls since."""

    def deck(self, reader: DeckReader) -> Path:
        return Path(self.transcript_path).with_suffix(reader.deck_suffix)

    @property
    def calls(self) -> Path:
        return Path(self.transcript_path).with_suffix(".calls")


@dataclass(frozen=True)
class DeckStart(DealtDeck):
    def take(self) -> tuple[str, ...]:
        return WatchClaim(self.session_id).state().by_role(
            unbound=tuple, fleet=lambda record: self.taken(FleetReader()),
            commander=lambda record: self.taken(CommanderReader()))

    def taken(self, reader: HookReader) -> tuple[str, ...]:
        return self.dealt(reader)

    def dealt(self, reader: HookReader) -> tuple[str, ...]:
        return DeckParts.order(self.prepare(reader)), reader.header(self.held)

    def held(self) -> Iterator[HeldOrders]:
        for root, snapshot, commander in self.commanders():
            yield HeldOrders.of(TodoStore(root), snapshot, commander.name)

    def commanders(self) -> Iterator[tuple[Path, RegistrySnapshot, Thread]]:
        for record in WatchClaim(self.session_id).state().bindings():
            root = Path(record.root)
            snapshot = RegistryStore.at(root).snapshot()
            for thread in snapshot.commander_project.commanders(snapshot):
                yield root, snapshot, thread

    def prepare(self, reader: HookReader) -> tuple[Path, ...]:
        self.calls.unlink(missing_ok=True)
        self.parts_read.write_text("", encoding="utf-8")
        self.deck(reader).write_text(json.dumps(reader.slices()), encoding="utf-8")
        return DeckParts(self.parts).prepare(reader, self.notice())

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
