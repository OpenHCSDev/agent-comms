"""One-shot old-ledger export. Delete after reviewed live installation completes.

Never overwrite a running ledger: its old writers would restore the old format.
The runtime installation owner installs this output only after retiring old writers.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.bus_route_counts import BusRouteCounts
from agent_comms.field_codec import FieldCodec
from agent_comms.read_basis import Conversation
from agent_comms.read_ledger import ConversationKey, ReadDocument, ReadLedger, ReadProgress, TranscriptKey, PrefixUnavailable
from agent_comms.registry_document import RegistryDocument
from agent_comms.thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class OldConversation:
    target: str = ''
    participants: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class OldDocument:
    messages: dict[str, tuple[int, ...]] = field(default_factory=dict)
    bus_identity: tuple[int, int] | None = None
    transcripts: dict[str, int] = field(default_factory=dict)
    notice: str | None = None


def carry(source, destination, registry, index_root):
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError('Export requires a distinct, new destination; never replace a live writer')
    if destination.name != ReadLedger.filename:
        raise ValueError(f'Destination must name the canonical {ReadLedger.filename}')
    original = source.read_bytes()
    old = FieldCodec.decode(OldDocument, json.loads(original))
    snapshot = FieldCodec.decode(RegistryDocument, json.loads(registry.read_bytes())).snapshot()
    messages = {}
    with BusRouteCounts(index_root / 'bus.jsonl') as index:
        for raw, sequences in old.messages.items():
            name, created, conversation = FieldCodec.decode(tuple[str, float, OldConversation], json.loads(raw))
            conversation = Conversation(conversation.target, tuple(ThreadIncarnation(*p) for p in conversation.participants))
            key = ConversationKey(ThreadIncarnation(name, created), conversation).resolved(snapshot)
            state = messages.get(key, ReadProgress()).mark(sequences, lambda state:
                index.read_prefix(key.conversation, state, snapshot, old.bus_identity)
                if key.viewer.current(snapshot) and key.conversation.current(snapshot) else PrefixUnavailable())
            messages[key] = state
    transcripts = {}
    for raw, through in old.transcripts.items():
        viewer, path, inode = FieldCodec.decode(tuple[str, str, int], json.loads(raw))
        key = TranscriptKey.capture(viewer, path, inode)
        transcripts[key] = max(transcripts.get(key, 0), through)
    document = ReadDocument(messages, old.bus_identity, transcripts, old.notice)
    ReadLedger(destination.parent).replace(document)
    assert source.read_bytes() == original, 'Original changed during export; output is not installation-ready'
    return {'source_sha256': hashlib.sha256(original).hexdigest(),
            'source_bytes': len(original), 'destination_bytes': destination.stat().st_size,
            'conversations': len(messages), 'sparse_sequences': sum(len(p.sparse) for p in messages.values()),
            'prefixes': sum(bool(p.read_through) for p in messages.values()),
            'original_unchanged': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--index-root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(carry(args.source, args.destination, args.registry, args.index_root)))

if __name__ == '__main__':
    main()
