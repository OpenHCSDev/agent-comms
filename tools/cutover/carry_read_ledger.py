"""One-shot read ledger carry; quiesce old readers/writers first.

Run after carry_storage_roots.py. Earlier Core keyed the ledger by JSON text:
``[viewer, created_at, conversation]`` for message reads and
``[viewer, source, inode]`` for transcript reads. Current Core keys message
reads by the viewer's incarnation, then by conversation, and transcript reads
by the tuple itself. Every read is kept; the bus identity and notice are
unchanged. Dry-run is the default. A ledger current Core already decodes is
left alone.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.read_basis import Conversation, ReadCursor
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.store_files import _store_lock
from agent_comms.thread_identity import RecordedThreadIncarnation, ThreadIncarnationRecord


def carried(raw: dict) -> ReadDocument:
    messages: dict[RecordedThreadIncarnation, dict[Conversation, ReadCursor]] = {}
    for key, sequences in raw["messages"].items():
        viewer, created_at, conversation = json.loads(key)
        cursors = messages.setdefault(RecordedThreadIncarnation(viewer, created_at), {})
        # Earlier keys spelled each participant as a [name, created_at] pair.
        pairs = FieldCodec.decode(tuple[tuple[str, float], ...], conversation.pop("participants"))
        conversation = replace(
            FieldCodec.decode(Conversation, conversation),
            participants=tuple(ThreadIncarnationRecord.decode(*pair) for pair in pairs),
        )
        cursors[conversation] = cursors.get(conversation, ReadCursor()).add(tuple(sequences))
    transcripts = {}
    for key, through in raw["transcripts"].items():
        viewer, source, inode = json.loads(key)
        transcripts[(viewer, source, inode)] = through
    return ReadDocument(
        messages,
        tuple(raw["bus_identity"]) if raw["bus_identity"] is not None else None,
        transcripts,
        raw["notice"],
    )


def carry_locked(root: Path, *, apply: bool = False) -> dict:
    """Carry under the caller's retained wire lock; acquire only the ledger."""
    ledger = ReadLedger.at(root)
    if not ledger.path.exists():
        return {"message_reads": 0, "transcript_reads": 0, "carried": False, "applied": apply}
    raw = json.loads(ledger.path.read_text())
    try:
        FieldCodec.decode(ReadDocument, raw)
    except (TypeError, ValueError):
        document = carried(raw)
    else:
        return {"message_reads": 0, "transcript_reads": 0, "carried": False, "applied": apply}
    if apply:
        ledger.replace(document)
    return {
        "message_reads": len(raw["messages"]),
        "transcript_reads": len(raw["transcripts"]),
        "carried": True,
        "applied": apply,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=lambda value: Path(value).resolve())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with _store_lock(args.root / "wire"):
        print(json.dumps(carry_locked(args.root, apply=args.apply)))


if __name__ == "__main__":
    main()
