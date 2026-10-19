"""Carry archived sparse read positions into the current conversation cursors (P495)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.carry import Carried, Carry
from tools.cutover.carry_p495_read_ledger import PreviousConversation

from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.read_basis import ConversationCursor, ReadCursor
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.recorded_bus import RecordedBusIdentity
from agent_comms.thread_identity import RecordedThreadIncarnation, ThreadIncarnationRecord


@dataclass(frozen=True)
class ArchivedConversation:
    target: str
    participants: tuple[tuple[str, float], ...]

    def current(self):
        return PreviousConversation(self.target, tuple(
            ThreadIncarnationRecord.decode(*person) for person in self.participants)).current()


@dataclass(frozen=True)
class ArchivedReads:
    messages: dict[str, tuple[int, ...]]
    bus_identity: RecordedBusIdentity
    transcripts: dict[tuple[str, str, int], int] = field(default_factory=dict)
    notice: str | None = None

    def current(self) -> ReadDocument:
        readers = {}
        for encoded, positions in self.messages.items():
            name, birth, conversation = FieldCodec.decode(
                tuple[str, float, ArchivedConversation], FieldCodec.parse(encoded))
            if positions != tuple(sorted(set(positions))) or any(n <= 0 for n in positions):
                raise ValueError("Archived read positions must be positive, ordered and unique")
            owner = RecordedThreadIncarnation(name, birth)
            cursors = readers.setdefault(owner, {})
            key = conversation.current()
            if key in cursors:
                raise ValueError("Duplicate archived conversation")
            cursors[key] = ConversationCursor(exceptions=positions)
        return ReadDocument({owner: ReadCursor(cursors) for owner, cursors in readers.items()},
                            self.bus_identity, self.transcripts, self.notice)


class ArchivedReadLedgers(Carry):
    after = "carry_p495_read_ledger"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        files = positions = 0
        rewritten = []
        for path in sorted((root / "history").glob("*/read_ledger.json")):
            ledger = ReadLedger.at(path.parent)
            with ledger.locked():
                original = path.read_bytes()
                parsed = FieldCodec.parse(original)
                try:
                    FieldCodec.decode(ReadDocument, parsed)
                except MalformedRecord:
                    previous = FieldCodec.decode(ArchivedReads, parsed)
                else:
                    continue
                current = previous.current()
                positions += sum(len(values) for values in previous.messages.values())
                files += 1
                if apply:
                    backup = path.with_name("read_ledger.before_archived_cursors.json")
                    with backup.open("xb") as stream:
                        os.chmod(backup, 0o600)
                        stream.write(original)
                        stream.flush()
                        os.fsync(stream.fileno())
                    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    ledger._publish_unlocked(current)
                    saved = FieldCodec.decode(ReadDocument, FieldCodec.parse(path.read_bytes()))
                    if saved != current:
                        raise ValueError("Archived read positions changed during conversion")
                    rewritten.extend((str(path.relative_to(root)), str(backup.relative_to(root))))
        return Carried(files, {"files": files, "positions": positions}, tuple(rewritten))


if __name__ == "__main__":
    ArchivedReadLedgers.main()
