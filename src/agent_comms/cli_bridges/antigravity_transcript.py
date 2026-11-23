"""Antigravity transcript files, read whether stored or still being written."""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, ClassVar, Self

from agent_comms.cli_bridges.antigravity_protocol import (
    ANTIGRAVITY_RECORD,
    AntigravityRecord,
    AntigravitySession,
)
from agent_comms.cli_bridges.history import (
    AntigravityHistory,
    HarnessAddress,
    JsonLines,
    JsonLinesFile,
    OfflineHistory,
    OnlineHistory,
)
from agent_comms.cli_bridges.runtime_updates import RuntimeUpdate, SessionFacts
from agent_comms.nominal.field_codec import StoredSessionId
from agent_comms.registry.thread_text import NamedSession


class AntigravityJsonLines(JsonLines[AntigravityRecord]):
    """How an Antigravity `transcript.jsonl` file decodes, offline or live."""

    records = ANTIGRAVITY_RECORD
    state = AntigravitySession

    def decode_line(self, raw: bytes, position: int) -> AntigravityRecord:
        return self.records.validate_python({**json.loads(raw), "position": position})

    def record_updates(
        self, record: AntigravityRecord, state: AntigravitySession, offset: int, raw: bytes
    ) -> Iterator[RuntimeUpdate]:
        del offset, raw
        for update in record.updates(state):
            yield from update.during(state.phase)


class AntigravityTranscript(
    AntigravityJsonLines, JsonLinesFile[AntigravityRecord], AntigravityHistory, OfflineHistory
):
    """Antigravity writes one JSONL transcript per conversation at
    `~/.gemini/<product>/brain/<conversation_id>/.system_generated/logs/transcript.jsonl`."""

    suffixes = frozenset({".jsonl"})

    @staticmethod
    def conversation_id_of(source: Path) -> str:
        """The conversation directory owns the ID (`.../<conversation_id>/.system_generated/logs/transcript.jsonl`)."""
        parts = source.parts
        if ".system_generated" in parts:
            idx = parts.index(".system_generated")
            if idx > 0:
                return parts[idx - 1]
        return source.stem

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        del stream, boundary
        conversation_id = self.conversation_id_of(self.source)
        if not self.session.holds(StoredSessionId(conversation_id)):
            raise ValueError("The transcript belongs to a different Antigravity conversation.")
        yield SessionFacts(source_id=conversation_id)


class RecentAntigravityWindow(AntigravityJsonLines):
    """Bounded recent tail for a live Antigravity session, starting at a complete line."""

    RECENT_BYTES: ClassVar[int] = 4 * 1024 * 1024

    def history_start(self, stream: BinaryIO, boundary: int) -> int:
        compaction = super().history_start(stream, boundary)
        start = max(compaction, boundary - self.RECENT_BYTES)
        if start > compaction:
            stream.seek(start - 1)
            stream.readline()
            start = stream.tell()
        return start


class RunningAntigravity(AntigravityTranscript, OnlineHistory):
    """A live Antigravity conversation found under `~/.gemini/` (CLI, desktop, IDE, or cloud)."""

    TRANSCRIPT_GLOBS: ClassVar[tuple[str, ...]] = (
        ".gemini/antigravity-cli/brain/{thread}/.system_generated/logs/transcript.jsonl",
        ".gemini/antigravity/brain/{thread}/.system_generated/logs/transcript.jsonl",
        ".gemini/antigravity-ide/brain/{thread}/.system_generated/logs/transcript.jsonl",
        ".gemini/jetski/brain/*/*/*/{thread}/.system_generated/logs/transcript.jsonl",
    )

    def __init__(self, thread: str, *, home: Path | None = None) -> None:
        base = home or Path.home()
        matches = tuple(
            path
            for pattern in self.TRANSCRIPT_GLOBS
            for path in base.glob(pattern.format(thread=thread))
            if path.is_file()
        )
        if not matches:
            raise ValueError(f"No Antigravity transcript for {thread}")
        newest = max(matches, key=lambda path: path.stat().st_mtime)
        super().__init__(newest, NamedSession(thread))

    @classmethod
    def connected(cls, thread: str, address: HarnessAddress, directory: Path) -> Self:
        del address, directory
        return cls(thread)

    @classmethod
    def active(cls, *, home: Path | None = None) -> Self:
        """Locate the active Antigravity session transcript from explicit env vars or the newest
        conversation transcript under `~/.gemini/`."""
        if explicit := os.environ.get("ANTIGRAVITY_TRANSCRIPT", "").strip():
            path = Path(explicit)
            if not path.is_file():
                raise ValueError(f"ANTIGRAVITY_TRANSCRIPT does not exist: {path}")
            instance = object.__new__(cls)
            AntigravityTranscript.__init__(
                instance, path, NamedSession(cls.conversation_id_of(path))
            )
            return instance
        if conversation_id := os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip():
            return cls(conversation_id, home=home)
        base = home or Path.home()
        candidates = tuple(
            path
            for pattern in cls.TRANSCRIPT_GLOBS
            for path in base.glob(pattern.format(thread="*"))
            if path.is_file()
        )
        if not candidates:
            raise ValueError("No Antigravity transcript found under ~/.gemini/")
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        instance = object.__new__(cls)
        AntigravityTranscript.__init__(
            instance, newest, NamedSession(cls.conversation_id_of(newest))
        )
        return instance
