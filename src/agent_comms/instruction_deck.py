"""The sources and full-read parts shared by bound Claude and Pi sessions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .commander.package import CommanderPackage
from .commander.rule_file import RuleAudience, RuleFile
from .pattern_sources import PostmortemFailure
from .slop import Slop

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .participant_identity import ParticipantIdentity


class DeckReader(ABC):
    """Every bound session reads every rule; audience headings explain authority."""

    deck_suffix: ClassVar[str]
    slice_chars: ClassVar[int] = 2_500

    @staticmethod
    def for_owner(snapshot: RegistrySnapshot, owner: ParticipantIdentity) -> DeckReader:
        return (CommanderReader() if any(thread.incarnation.require_recorded().lookup == owner.lookup
                for thread in snapshot.commander_project.commanders(snapshot)) else FleetReader())

    @classmethod
    def rules(cls) -> tuple[RuleFile, ...]:
        return RuleFile.packaged()

    @classmethod
    def sources(cls, notice: str) -> str:
        return "\n\n".join((
            notice,
            CommanderPackage.voice().read_text(encoding="utf-8", newline=""),
            CommanderPackage.indictment().read_text(encoding="utf-8", newline=""),
            "# Recorded failures\n\n" + PostmortemFailure.catalog(),
            *(audience.heading + "\n\n" + "\n\n".join(rule.render() for rule in rules)
              for audience, rules in RuleAudience.grouped(cls.rules())),
            (Slop.catalog() / "README.md").read_text(encoding="utf-8", newline=""),
            *cls.additional_sources(),
        ))

    @classmethod
    def additional_sources(cls) -> tuple[str, ...]:
        return ()

    def header(self, held: Callable[[], str]) -> str:
        return ("FUCK YOUR COMPACTION. Your summary is a paraphrase of these rules, and a"
                " paraphrase drops the exact words they turn on. The files above are the source;"
                " where your summary disagrees with them, the summary is wrong.")

    @classmethod
    @abstractmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]: ...

    @classmethod
    def slices(cls) -> list[str]:
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
        runs: list[str] = []
        for line in paragraph.splitlines():
            if runs and len(runs[-1]) + 1 + len(line) <= cls.slice_chars:
                runs[-1] += "\n" + line
            else:
                runs.append(line)
        return tuple(runs)


class FleetReader(DeckReader):
    deck_suffix = ".fleet-deck.json"

    @classmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        return (cls.voice(), cls.tells(), cls.failures())


class CommanderReader(FleetReader):
    deck_suffix = ".deck.json"

    @classmethod
    def additional_sources(cls) -> tuple[str, ...]:
        return (RuleFile.document(CommanderPackage.skill())[1],)

    @classmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        indictment = cls.paragraphs(CommanderPackage.indictment().read_text(encoding="utf-8"))
        return (cls.voice(), indictment, cls.tells(), cls.failures())

    def header(self, held: Callable[[], str]) -> str:
        return held()


@dataclass(frozen=True)
class DeckParts:
    """One chunker, bounded below both tools' default byte and line limits."""

    directory: Path
    part_bytes: ClassVar[int] = 48_000
    part_lines: ClassVar[int] = 1_900

    @classmethod
    def beside_session(cls, directory: Path) -> DeckParts:
        return cls(directory / "rule-parts")

    @classmethod
    def chunks(cls, text: str) -> Iterator[str]:
        lines: list[str] = []
        size = 0
        for line in text.splitlines(keepends=True):
            width = len(line.encode("utf-8"))
            if width > cls.part_bytes:
                raise ValueError("A rule source line exceeds a full Read; split it at its source")
            if lines and (size + width > cls.part_bytes or len(lines) >= cls.part_lines):
                yield "".join(lines)
                lines, size = [], 0
            lines.append(line)
            size += width
        if lines:
            yield "".join(lines)

    def numbered(self, number: int) -> Path:
        return self.directory / f"{number:02}.md"

    def planned(self, reader: DeckReader, notice: str = "") -> tuple[Path, ...]:
        """Names for a prompt built before its child starts; retain no source text."""
        return tuple(self.numbered(number) for number, _ in enumerate(self.chunks(reader.sources(notice)), 1))

    def prepare(self, reader: DeckReader, notice: str = "") -> tuple[Path, ...]:
        self.directory.mkdir(parents=True, exist_ok=True)
        for old in self.directory.iterdir():
            old.unlink()
        parts = []
        for number, chunk in enumerate(self.chunks(reader.sources(notice)), 1):
            path = self.numbered(number)
            path.write_text(chunk, encoding="utf-8")
            parts.append(path)
        return tuple(parts)

    @staticmethod
    def order(parts: tuple[Path, ...]) -> str:
        return (
            f"Read each of these {len(parts)} files in full with the Read tool before any other"
            " action: they are every rule you follow and the sources they come from. A summary"
            " or memory of them does not count as reading them, and every other tool is refused"
            " until each one is read. Use no offset or limit. Then, before your next action,"
            " check what you did since your last start or compaction against them and say,"
            " one line each, every rule you broke and the action that broke it, and fix what"
            " you can. This order returns after each context reset.\n"
            + "\n".join(str(part) for part in parts)
        )
