"""At session start, resume and compaction, write the commander's sources, order them read once,
and deal the reminder deck.

The sources are the commander's voice, the indictment, the recorded failures, the commander skill,
the packaged rules and the refactor-audit catalog's index; a rung file is read when an order or
roast cites its pattern ID. Claude Code caps
injected hook text at 10,000 characters and the sources are far longer, so they
go into parts beside the transcript and the hook injects the order to read
each part, once. The reminder hook then puts one short slice inline every few tool calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import RuleFile
from agent_comms.pattern_sources import PostmortemFailure
from agent_comms.registry_store import RegistryStore
from agent_comms.slop import Slop
from agent_comms_hooks import ClaudeHookProgram
from agent_comms_hooks.reread import CommanderDeck
from agent_comms_hooks.watch import WatchClaim


@dataclass(frozen=True)
class CommanderStart(CommanderDeck):
    part_chars: ClassVar[int] = 60_000
    """The most characters one Read of a part returns in full."""
    def due(self) -> bool:
        """Every session the commander's bridge binding does not rule out: a session bound to
        another bus thread (a Claude unit) never gets the commander's sources."""
        for record in WatchClaim(self.session_id).state().bindings():
            snapshot = RegistryStore.at(Path(record.root)).read_snapshot_unlocked()
            subject = snapshot.require(record.bus)
            if not any(thread.incarnation == subject.incarnation
                       for thread in snapshot.commander_project.commanders(snapshot)):
                return False
        return True

    def take(self) -> tuple[str, ...]:
        """The order to read every part, once; the deck carries the sources after it."""
        if not self.due():
            return ()
        self.prepare()
        parts = sorted(self.parts.iterdir())
        return (
            f"Read each of these {len(parts)} files in full with the Read tool before any other"
            " action: they are the indictment, the recorded failures, the commander skill, every"
            " packaged rule and the refactor-audit catalog index. A summary or memory of them"
            " does not count as reading them. This order is given once per session start or"
            " compaction; a short slice of them comes back inline every few tool calls.\n"
            + "\n".join(str(part) for part in parts),
        )

    def prepare(self) -> None:
        """A start or compaction drops what was read: the deck is dealt again from its start."""
        self.calls.unlink(missing_ok=True)
        self.deck.write_text(json.dumps(self.slices()), encoding="utf-8")
        self.parts.mkdir(exist_ok=True)
        for old in self.parts.iterdir():
            old.unlink()
        chunks: list[list[str]] = [[]]
        size = 0
        for line in self.sources(self.notice()).splitlines(keepends=True):
            if size + len(line) > self.part_chars and chunks[-1]:
                chunks.append([])
                size = 0
            chunks[-1].append(line)
            size += len(line)
        for number, chunk in enumerate(chunks, 1):
            (self.parts / f"{number:02}.md").write_text("".join(chunk), encoding="utf-8")

    @classmethod
    def slices(cls) -> list[str]:
        """The voice, the indictment, the drift tells and the recorded failures, one slice of each
        per round, so each comes back every fourth slice and the longest source runs through once.
        The failures are the habits caught so far; they are called out by name, repeatedly."""
        voice = cls.paragraphs(CommanderPackage.voice().read_text(encoding="utf-8"))
        indictment = cls.paragraphs(
            CommanderPackage.indictment().read_text(encoding="utf-8"))
        tells = RuleFile.drift_tells()
        failures = tuple(f"Recorded failure {line}" for line in PostmortemFailure.catalog().splitlines())
        streams = (voice, indictment, tells, failures)
        rounds = max(len(stream) for stream in streams)
        return [stream[number % len(stream)] for number in range(rounds) for stream in streams]

    @staticmethod
    def paragraphs(text: str) -> tuple[str, ...]:
        """Blank-line paragraphs; a heading stays with the paragraph it introduces."""
        found: list[str] = []
        heading = ""
        for block in (block.strip() for block in text.split("\n\n")):
            if block.startswith("#"):
                heading = f"{heading}{block}\n"
            elif block:
                found.append(heading + block)
                heading = ""
        return tuple(found)

    @classmethod
    def sources(cls, notice: str = "Commander instructions.") -> str:
        return "\n\n".join(
            (
                notice,
                CommanderPackage.voice().read_text(encoding="utf-8", newline=""),
                CommanderPackage.indictment().read_text(encoding="utf-8", newline=""),
                "# Recorded failures\n\n" + PostmortemFailure.catalog(),
                RuleFile.document(CommanderPackage.skill())[1],
                *(rule.render() for rule in RuleFile.packaged()),
                (Slop.catalog() / "README.md").read_text(encoding="utf-8", newline=""),
            )
        )

    def notice(self) -> str:
        names = []
        for record in WatchClaim(self.session_id).state().bindings():
            snapshot = RegistryStore.at(Path(record.root)).read_snapshot_unlocked()
            names.extend(thread.name for thread in snapshot.commander_project.commanders(snapshot))
        return ("Commander: " + ", ".join(names) + "." if names
                else "No commander project is registered for this session.")


class SkillText(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    start = CommanderStart.read()
    for text in start.take():
        start.warn(text)


if __name__ == "__main__":
    main()
