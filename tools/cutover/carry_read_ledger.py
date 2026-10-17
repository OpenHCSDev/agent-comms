"""One-time read-ledger carry. Codex runs this with writers stopped at cutover."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.read_basis import Conversation, ReadCursor
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.store_files import _store_lock
from agent_comms.thread_identity import RecordedThreadIncarnation, ThreadIncarnationRecord


def carry(root: Path, backup: Path) -> None:
    ledger = ReadLedger(root / ReadLedger.filename)
    with _store_lock(ledger.path):
        original = ledger.path.read_text()
        raw = json.loads(original)
        messages = {}
        for key, value in raw['messages'].items():
            name, birth, conversation = json.loads(key)
            conversation['participants'] = [ThreadIncarnationRecord.decode(*pair).to_wire()
                                            for pair in conversation['participants']]
            owner = RecordedThreadIncarnation(name, birth)
            scope = FieldCodec.decode(Conversation, conversation)
            cursor = (FieldCodec.decode(ReadCursor, value) if isinstance(value, dict)
                      else ReadCursor().add(FieldCodec.decode(tuple[int, ...], value)))
            scopes = messages.setdefault(owner, {})
            scopes[scope] = ReadCursor.union((scopes.get(scope, ReadCursor()), cursor))
        transcripts = {tuple(json.loads(key)): value for key, value in raw['transcripts'].items()}
        document = ReadDocument(messages, FieldCodec.decode(tuple[int, int] | None, raw['bus_identity']), transcripts, raw['notice'])
        encoded = FieldCodec.encode(document)
        assert FieldCodec.decode(ReadDocument, encoded) == document
        with backup.open('x') as output:
            output.write(original)
        ledger._write_unlocked(json.dumps(encoded))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    options = parser.parse_args()
    carry(options.root, options.backup)
