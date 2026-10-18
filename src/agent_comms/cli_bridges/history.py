"""A coding session's past, read once per harness and shared by import and the live bridges.

Every harness has two ways to reach a session's past: an offline reader of the files the harness
stores, and an online reader that asks the running harness. Both decode into the transcript
updates the live bridges publish, so an import and a bridge read a session through one decoder.
A bridge is its harness's online reader plus delivery and following.
"""
from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import BinaryIO, ClassVar, Self

from agent_comms.declared_family import DeclaredFamily
from agent_comms.harness_install import Harness

from .shared import EarlierRequest, HistoryNotice, TranscriptUpdate


class History(DeclaredFamily, affix="History", discovery_package="agent_comms.cli_bridges"):
    """One session's past, oldest first, as transcript updates."""

    harness: ClassVar[type[Harness]]

    @abstractmethod
    def history(self) -> AsyncIterator[TranscriptUpdate]: ...

    @classmethod
    def readers(cls, harness: type[Harness]) -> tuple[type[Self], ...]:
        """This harness's readers of this kind; a bridge specializes a reader and is not one more."""
        members = tuple(member for member in cls.members_with(cls) if member.harness is harness)
        return tuple(member for member in members
                     if not any(other is not member and issubclass(member, other) for other in members))


class OfflineHistory(History):
    """Reads the files a harness stores; the harness need not be running."""

    suffixes: ClassVar[frozenset[str]]

    @classmethod
    def reads(cls, source: Path) -> bool:
        return source.suffix in cls.suffixes

    @classmethod
    @abstractmethod
    def opened(cls, source: Path, session_id: str | None) -> Self: ...

    @abstractmethod
    def read(self) -> Iterator[TranscriptUpdate]: ...

    async def history(self) -> AsyncIterator[TranscriptUpdate]:
        for update in self.read():
            yield update

    @classmethod
    def of(cls, harness: type[Harness], source: Path, session_id: str | None) -> OfflineHistory:
        readers = tuple(reader for reader in cls.readers(harness) if reader.reads(source))
        if len(readers) != 1:
            raise ValueError(f'No single {harness.declared_name} reader for {source}')
        return readers[0].opened(source, session_id)


class OnlineHistory(History):
    """Asks the running harness for a session it holds."""

    @classmethod
    @abstractmethod
    def connected(cls, thread: str, address: str | None, directory: Path) -> Self:
        """`address` is how this harness is reached (socket, server URL); `directory` the session's."""

    @classmethod
    def of(cls, harness: type[Harness], thread: str, address: str | None, directory: Path) -> OnlineHistory:
        reader, = cls.readers(harness)
        return reader.connected(thread, address, directory)


class JsonLines:
    """A session stored as one JSON record per line, read up to the size it had when opened.

    Records appended while reading belong to a later read. Reading starts at the newest record
    that begins the session's current history (a compaction), so a long session's discarded
    prefix is never read.
    """

    REVERSE_BLOCK: ClassVar[int] = 64 * 1024

    @abstractmethod
    def decode_line(self, raw: bytes): ...

    @abstractmethod
    def begins_history(self, record) -> bool:
        """Whether everything before this record was replaced by it."""

    @abstractmethod
    def start_state(self):
        """What decoding remembers between records (open tool calls, the current turn)."""

    @abstractmethod
    def record_updates(self, record, state, offset: int, raw: bytes) -> Iterator[TranscriptUpdate]: ...

    def decoded(self, stream: BinaryIO, start: int, boundary: int, state) -> Iterator[TranscriptUpdate]:
        for offset, raw in self.complete_lines(stream, start, boundary):
            yield from self.line_updates(raw, state, offset, 'A source record that was not valid JSON was omitted.')

    def line_updates(self, raw: bytes, state, offset: int, damage: str) -> Iterator[TranscriptUpdate]:
        """A line that is not JSON is the harness's damage (a killed writer, two writers on one
        line) and is left out with a notice; JSON this decoder does not know is our gap and fails."""
        try:
            record = self.decode_line(raw)
        except ValueError:
            try:
                json.loads(raw)
            except ValueError:
                yield HistoryNotice(damage)
                return
            raise
        yield from self.record_updates(record, state, offset, raw)

    def earlier_request(self, stream: BinaryIO, start: int) -> str:
        for _, raw in self.reverse_lines(stream, start):
            try:
                record = self.decode_line(raw)
            except ValueError:
                continue
            if text := record.request():
                return text
        return ''

    def history_start(self, stream: BinaryIO, boundary: int) -> int:
        for offset, raw in self.reverse_lines(stream, boundary):
            try:
                record = self.decode_line(raw)
            except ValueError:
                continue  # Decoded again, with its notice or failure, when read forward.
            if self.begins_history(record):
                return offset
        return 0

    def reverse_lines(self, stream: BinaryIO, boundary: int) -> Iterator[tuple[int, bytes]]:
        """Complete records newest first, without reading the older prefix."""
        position, carry, complete = boundary, b'', False
        while position > 0:
            start = max(0, position - self.REVERSE_BLOCK)
            stream.seek(start)
            data = stream.read(position - start) + carry
            cursor = len(data)
            while (newline := data.rfind(b'\n', 0, cursor)) >= 0:
                if complete and newline + 1 < cursor:
                    yield start + newline + 1, data[newline + 1:cursor]
                complete, cursor = True, newline
            carry, position = data[:cursor], start
        if complete and carry:
            yield 0, carry

    @staticmethod
    def complete_lines(stream: BinaryIO, start: int, boundary: int) -> Iterator[tuple[int, bytes]]:
        """Newline-terminated records from `start`; an unterminated last record is left unread."""
        stream.seek(start)
        while (offset := stream.tell()) < boundary:
            raw = stream.readline(boundary - offset)
            if not raw.endswith(b'\n'):
                stream.seek(offset)
                return
            if raw.strip():
                yield offset, raw[:-1]


class JsonLinesFile(JsonLines, OfflineHistory):
    """A stored JSONL session read once, including a final record its harness never finished."""

    source: Path

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[TranscriptUpdate]:
        """Session facts that precede the selected history; none unless the format keeps them apart."""
        return iter(())

    def read(self) -> Iterator[TranscriptUpdate]:
        with self.source.open('rb') as stream:
            boundary = stream.seek(0, 2)
            yield from self.facts(stream, boundary)
            state = self.start_state()
            start = self.history_start(stream, boundary)
            yield from self.decoded(stream, start, boundary, state)
            # The harness may still be writing its last record.
            offset = stream.tell()
            if raw := stream.read(boundary - offset).strip():
                yield from self.line_updates(raw, state, offset, 'An incomplete final source record was omitted.')
            yield EarlierRequest(lambda: self.earlier_request(stream, start))
