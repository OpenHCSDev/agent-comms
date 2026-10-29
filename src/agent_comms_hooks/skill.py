"""Deal Core's packaged rule sources at Claude session start and compaction."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines
from agent_comms.held_orders import HeldOrders
from agent_comms.instruction_deck import CommanderReader, DeckReader, FleetReader
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.registry_store import RegistryStore
from agent_comms.threads import Thread
from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook
from agent_comms_hooks.watch import WatchClaim


class HookReader:
    """Bridge reminder capability, composed with a source reader rather than defining one."""

    def deaf(self, session: str) -> tuple[str, ...]:
        return ()

    def board(self, session: str) -> tuple[str, ...]:
        return ()


class FleetDeck(HookReader, FleetReader):
    pass


class CommanderDeck(CommanderReader, FleetDeck):
    def deaf(self, session: str) -> tuple[str, ...]:
        return tuple(deaf.line() for deaf in WatchClaim(session).deaf())

    def board(self, session: str) -> tuple[str, ...]:
        return WatchClaim(session).state().board()


@dataclass(frozen=True)
class DealtDeck(ClaudeTranscriptHook):
    """The slices dealt beside the transcript, and the calls since."""

    def deck(self, reader: DeckReader) -> Path:
        return Path(self.transcript_path).with_suffix(reader.deck_suffix)


@dataclass(frozen=True)
class DeckStart(DealtDeck):
    owner_prompts: ClassVar[int] = 6
    """How many of the person's own last prompts the deal puts back in front of the session."""

    def take(self) -> tuple[str, ...]:
        """Unbound sessions get nothing; bound sessions get their declared role's reader."""
        return WatchClaim(self.session_id).state().by_role(
            unbound=tuple, fleet=lambda record: self.taken(FleetDeck()),
            commander=lambda record: self.taken(CommanderDeck()))

    def taken(self, reader: FleetDeck) -> tuple[str, ...]:
        """A session start deals the deck."""
        return self.dealt(reader)

    def dealt(self, reader: DeckReader) -> tuple[str, ...]:
        """Read original files, then answer every dealt rule and exact owner prompt."""
        prompts = self.prompts()
        self.prepare(reader, prompts)
        return (
            self.notice() + " " + reader.order(reader.sources(), audit=
            " That audit is not optional: every tool but Read, and Write of"
            f" {self.audit}, is refused until the audit is written there as JSON:"
            ' {"rules": [{"rule": NAME, "broke": [each action since the last start or'
            ' compaction that broke it; empty means kept]}, one entry for every rule you were'
            ' dealt], "owner_words": [{"words": EACH PROMPT QUOTED BELOW, "doing": what you are'
            " doing about it right now}]}"),
            self.owner_words(prompts),
            reader.header(self.held),
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

    def held(self) -> Iterator[str]:
        """Every actual held order from the Todo store, not a summary's paraphrase."""
        for root, snapshot, commander in self.commanders():
            yield from HeldOrders.recalled(root, snapshot, commander.name)

    def commanders(self) -> Iterator[tuple[Path, RegistrySnapshot, Thread]]:
        """Each commander registered on a bound bus, with that bus's registry cut."""
        for record in WatchClaim(self.session_id).state().bindings():
            root = Path(record.root)
            snapshot = RegistryStore.at(root).snapshot()
            for thread in snapshot.commander_project.commanders(snapshot):
                yield root, snapshot, thread

    def prepare(self, reader: DeckReader, prompts: tuple[str, ...]) -> None:
        """A start or compaction drops what was read: deal from the start again, and count no
        file as read until it is read again."""
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


class SkillText(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    start = DeckStart.read()
    start.warn(*start.take())


if __name__ == "__main__":
    main()
