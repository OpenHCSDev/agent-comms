"""Packaged rule sources, shared reminders and per-context full-read state."""

from __future__ import annotations

import inspect
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, ClassVar

from agent_comms_hooks import ClaudeTranscriptHook

from .compaction_notices import CompactionNotice, IgnoredCompactionDeal, StartedRuleReading
from .declared_family import DeclaredFamily
from .commander.package import CommanderPackage
from .commander.rule_file import CommanderAudience, FleetAudience, RuleAudience, RuleFile
from .field_codec import ToolCallId, boundary_decoder
from .pattern_sources import PostmortemFailure
from .slop import Slop
from .store_files import StoreGuard
from .tool_denial import SelectedToolDenied


class DeckReader(ABC):
    """The packaged sources for the audiences this reader declares and inherits.

    Every bound session follows the fleet rules; the registered commander also follows the
    commander's. RuleFile's audience owns that split, not a second list of rule names.
    """

    audiences: ClassVar[tuple[type[RuleAudience], ...]] = ()
    deck_suffix: ClassVar[str]
    """A role change finds no deck of the new kind and deals it again."""
    slice_chars: ClassVar[int] = 2_500
    """A longer paragraph splits at its lines; a rule's wake-up is never split."""

    def __init_subclass__(cls, *, audiences: tuple[type[RuleAudience], ...] = (),
                          **options: object) -> None:
        super().__init_subclass__(**options)
        cls.audiences = (*cls.audiences, *audiences)

    @classmethod
    def rules(cls) -> tuple[RuleFile, ...]:
        return tuple(rule for rule in RuleFile.packaged() if rule.audience in cls.audiences)

    @classmethod
    def preface(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def instructions(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def catalogs(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def sources(cls) -> tuple[Path, ...]:
        return (
            CommanderPackage.voice(),
            *cls.preface(),
            Path(inspect.getfile(PostmortemFailure)),
            *cls.instructions(),
            *(rule.path for rule in cls.rules()),
            *cls.catalogs(),
        )

    @classmethod
    def prepare(cls) -> Mapping[Path, str]:
        """Snapshot original files off the loop; completion compares only this text."""
        return {source.resolve(): source.read_text(encoding="utf-8", newline="")
                for source in cls.sources()}

    def header(self, held: Callable[[], Iterable[str]]) -> str:
        return ("FUCK YOUR COMPACTION. Your summary is a paraphrase of these rules, and a"
                " paraphrase drops the exact words they turn on. The files above are the source;"
                " where your summary disagrees with them, the summary is wrong.")

    @staticmethod
    def order(sources: Iterable[Path], *, audit: str = "") -> str:
        files = tuple(sources)
        return (
            f"Read each of these {len(files)} files in full with the Read tool before any other"
            " action: they are every rule you follow and the sources they come from. A summary"
            " or memory of them does not count as reading them, and every other tool is refused"
            " until each one is read. Use no offset or limit. Then, before your next action,"
            " check what you did since your last start or compaction against them and say,"
            " one line each, every rule you broke and the action that broke it, and fix what"
            " you can: reading alone is passive, the audit is what finds the breaks"
            " (on 2026-10-27 one reread found six broken rules in one session)."
            " This order returns after each context reset." + audit + "\n"
            + "\n".join(str(source) for source in files)
        )

    @classmethod
    @abstractmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        """The slice streams interleaved one slice per stream in each round."""

    @classmethod
    def slices(cls) -> list[str]:
        """The shortest stream returns every round; the longest runs through once. Recorded
        failures are the habits already caught, called out by name repeatedly."""
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
        """Keep headings with their blank-line paragraph; split long paragraphs at lines."""
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
        """Consecutive whole lines, each run within slice_chars."""
        runs: list[str] = []
        for line in paragraph.splitlines():
            if runs and len(runs[-1]) + 1 + len(line) <= cls.slice_chars:
                runs[-1] += "\n" + line
            else:
                runs.append(line)
        return tuple(runs)


class FleetReader(DeckReader, audiences=(FleetAudience,)):
    deck_suffix = ".fleet-deck.json"

    @classmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        return (cls.voice(), cls.tells(), cls.failures())


class CommanderReader(FleetReader, audiences=(CommanderAudience,)):
    deck_suffix = ".deck.json"

    @classmethod
    def preface(cls) -> tuple[Path, ...]:
        return (CommanderPackage.indictment(),)

    @classmethod
    def instructions(cls) -> tuple[Path, ...]:
        return (CommanderPackage.skill(),)

    @classmethod
    def catalogs(cls) -> tuple[Path, ...]:
        return (Slop.catalog() / "README.md",)

    @classmethod
    def streams(cls) -> tuple[tuple[str, ...], ...]:
        indictment = cls.paragraphs(CommanderPackage.indictment().read_text(encoding="utf-8"))
        return (cls.voice(), indictment, cls.tells(), cls.failures())

    def header(self, held: Callable[[], Iterable[str]]) -> str:
        return "\n".join((super().header(held), "What your units actually hold:", *held()))


class PartReadCompletion(ABC):
    """A checked read either proves one source or remains behind the ordinary gate."""

    def require(self, require_rules: Callable[[], None]) -> None:
        require_rules()

    @abstractmethod
    def complete(self, text: Callable[[int], str], parts_read: set[Path]) -> None: ...


@dataclass(frozen=True)
class NotPartRead(PartReadCompletion):
    def complete(self, text: Callable[[int], str], parts_read: set[Path]) -> None:
        pass


@dataclass(frozen=True)
class PartRead(PartReadCompletion):
    path: Path
    expected: str

    def require(self, require_rules: Callable[[], None]) -> None:
        pass

    def complete(self, text: Callable[[int], str], parts_read: set[Path]) -> None:
        if text(len(self.expected) + 1) == self.expected:
            parts_read.add(self.path)


@dataclass(kw_only=True)
class RuleReads(ABC):
    """The read lifecycle is a child capability, not a dependency on its runtime."""

    parts_read: set[Path] = field(default_factory=set)
    pending_part_reads: dict[ToolCallId, PartReadCompletion] = field(default_factory=dict)

    @property
    @abstractmethod
    def reading_sources(self) -> Mapping[Path, str]: ...

    @property
    @abstractmethod
    def working_directory(self) -> Path: ...

    def reset_rule_reads(self) -> None:
        self.parts_read.clear()
        self.pending_part_reads = {call_id: NotPartRead() for call_id in self.pending_part_reads}

    def require_rule_reads(self) -> None:
        unread = tuple(path for path in self.reading_sources if path not in self.parts_read)
        if unread:
            raise SelectedToolDenied(DeckReader.order(unread))

    def begin_part_read(self, call_id: ToolCallId, path: str, *, full_read: bool) -> None:
        selected = (self.working_directory / path).resolve()
        completion = (PartRead(selected, self.reading_sources[selected])
                      if full_read and selected in self.reading_sources else NotPartRead())
        self.pending_part_reads[call_id] = completion

    def require_part_read(self, call_id: ToolCallId) -> None:
        self.pending_part_reads[call_id].require(self.require_rule_reads)

    def completed_part_read(self, call_id: ToolCallId, text: Callable[[int], str]) -> None:
        self.pending_part_reads.pop(call_id).complete(text, self.parts_read)

    def discard_part_read(self, call_id: ToolCallId) -> None:
        self.pending_part_reads.pop(call_id)



class GateNotices(DeclaredFamily):
    """The existing call record's leading byte owns which first event remains to report."""

    @classmethod
    def marker(cls) -> bytes:
        return cls.declared_name.encode("ascii")

    @classmethod
    @boundary_decoder
    def from_marker(cls, marker: bytes) -> GateNotices:
        return cls.member_named((marker or QuietGate.marker()).decode("ascii"))()

    def refused(self) -> tuple[type[CompactionNotice], ...]:
        return ()

    def first_read(self) -> tuple[type[CompactionNotice], ...]:
        return ()


class QuietGate(GateNotices, declared_name="."):
    def refused(self) -> tuple[type[CompactionNotice], ...]:
        return (IgnoredCompactionDeal,)


class RefusedGate(GateNotices, declared_name="!"):
    def first_read(self) -> tuple[type[CompactionNotice], ...]:
        return (StartedRuleReading,)


@dataclass(frozen=True)
class RecordedGateCall:
    """The call's ordinal and pre-call state, used only while its counter is locked."""

    notices: GateNotices
    number: int
    record: BinaryIO

    def refused(self) -> tuple[type[CompactionNotice], ...]:
        self.record.seek(0)
        self.record.write(RefusedGate.marker())
        return self.notices.refused()

    def read(self, read_record: Path, source: Path) -> tuple[type[CompactionNotice], ...]:
        line = f"{source.resolve()}\n".encode("utf-8")
        with read_record.open("ab", buffering=0) as record:
            record.write(line)
            first = record.tell() == len(line)
        return self.notices.first_read() if first else ()


class DealtCalls(ClaudeTranscriptHook, StoreGuard):
    """One byte per completed/refused call, retaining the existing .calls representation.

    The leading byte also records that a refusal occurred. Its remaining bytes are
    count padding, not another log to scan. Parallel refusals claim the first notice
    under this file's lock; ordinary counts remain one atomic append.
    """

    @property
    def calls(self) -> Path:
        return Path(self.transcript_path).with_suffix(".calls")

    def count_call(self) -> int:
        with self.calls.open("ab", buffering=0) as calls:
            calls.write(QuietGate.marker())
            return calls.tell()

    @contextmanager
    def recorded_call(self) -> Iterator[RecordedGateCall]:
        """Order the refusal/read transition and its publication against parallel calls.

        The first-read record and the refusal marker must observe the same order as
        their ordinals. Keep this session's lock through publication so the read notice
        cannot overtake the refusal it answers. Ordinary tool completions only append.
        """
        with self.write_guard(self.calls):
            fd = os.open(self.calls, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "r+b", buffering=0) as record:
                notices = GateNotices.from_marker(record.read(1))
                yield RecordedGateCall(notices, self.count_call(), record)
