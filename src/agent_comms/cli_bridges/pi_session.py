"""A native Pi session file: the format every agent-comms thread already runs."""

from __future__ import annotations

from collections.abc import Iterator

from agent_comms.cli_bridges.history import OfflineHistory, PiHistory
from agent_comms.importing import ImportLimits, KeptPiSession
from agent_comms.native_entries import (
    NativeEntry,
    NativeEvidenceRead,
    SessionEntry,
    TranscriptProjection,
)
from agent_comms.runtime_updates import HiddenItem, RuntimeUpdate, SessionFacts


class PiSession(PiHistory, OfflineHistory):
    """Pi's own session JSONL. Importing it keeps the file byte for byte: no rebuild, no limits."""

    suffixes = frozenset({".jsonl"})

    def observed(self) -> tuple[SessionEntry, tuple[NativeEntry, ...]]:
        with NativeEvidenceRead.open(self.source) as evidence:
            header, entries = evidence.observe()
        header.require_reopen()
        if not self.session.holds(header.require_entry_id()):
            raise ValueError(
                f"{self.source} holds Pi session {header.require_entry_id()}, not the one named"
            )
        return header, entries

    def read(self) -> Iterator[RuntimeUpdate]:
        header, entries = self.observed()
        yield SessionFacts(source_id=header.require_entry_id(), project=header.cwd.text_or(""))
        for position, entry in enumerate(entries):
            yield HiddenItem(tuple(entry.events(TranscriptProjection(position=position))))

    def imported(self, limits: ImportLimits) -> KeptPiSession:
        header, entries = self.observed()
        return KeptPiSession(
            harness=self.harness,
            source=self.source,
            source_id=header.require_entry_id(),
            project=header.cwd.text_or(""),
            messages_seen=sum(len(entry.messages) for entry in entries),
        )
