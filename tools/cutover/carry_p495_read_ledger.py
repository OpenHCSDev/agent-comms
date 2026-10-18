"""One-time P495 read-ledger conversion. Stop writers; back up before publishing.

Run with the P495 runtime's Python: carry_p495_read_ledger.py ROOT --backup FILE.
No production reader accepts the previous shape.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.read_basis import Conversation, ConversationCursor, ReadCursor
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.thread_identity import RecordedThreadIncarnation


@dataclass(frozen=True)
class PreviousReadDocument:
    messages: dict[RecordedThreadIncarnation, dict[Conversation, ConversationCursor]] = field(default_factory=dict)
    bus_identity: tuple[int, int] | None = None
    transcripts: dict[tuple[str, str, int], int] = field(default_factory=dict)
    notice: str | None = None


def carry(root: Path, backup: Path) -> None:
    ledger = ReadLedger.at(root)
    with ledger.locked():
        original = ledger.path.read_bytes()
        previous = FieldCodec.decode(PreviousReadDocument, FieldCodec.parse(original))
        current = ReadDocument(
            {owner: ReadCursor(conversations) for owner, conversations in previous.messages.items()},
            previous.bus_identity, previous.transcripts, previous.notice,
        )
        with backup.open('xb') as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        ledger._publish_unlocked(current)
    assert ledger.read() == current
    print(f'Converted {len(current.messages)} viewers; all message and native read positions preserved. Backup: {backup}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    args = parser.parse_args()
    carry(args.root, args.backup)


if __name__ == '__main__':
    main()
