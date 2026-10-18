"""Read ledger keys. Earlier Core keyed the ledger by JSON text:
``[viewer, created_at, conversation]`` for message reads and
``[viewer, source, inode]`` for transcript reads. Current Core keys message
reads by the viewer's incarnation, then by conversation, and transcript reads
by the tuple itself. Every read is kept; the bus identity and notice are
unchanged. A ledger current Core already decodes is left alone.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.read_basis import Conversation, ConversationCursor, DisplayedConversation, ReadCursor
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.thread_identity import RecordedThreadIncarnation, ThreadIncarnationRecord

from cutover.carry import Carried, Carry


def carried(raw: dict) -> ReadDocument:
    if isinstance(raw["messages"], list):
        previous = FieldCodec.decode(
            dict[RecordedThreadIncarnation, dict[Conversation, ConversationCursor]], raw["messages"],
        )
        document = FieldCodec.decode(ReadDocument, {**raw, "messages": []})
        return replace(document, messages={owner: ReadCursor(cursors) for owner, cursors in previous.items()})
    messages: dict[RecordedThreadIncarnation, ReadCursor] = {}
    for key, sequences in raw["messages"].items():
        viewer, created_at, conversation = json.loads(key)
        owner = RecordedThreadIncarnation(viewer, created_at)
        # Earlier keys spelled each participant as a [name, created_at] pair.
        pairs = FieldCodec.decode(tuple[tuple[str, float], ...], conversation.pop("participants"))
        conversation = replace(
            FieldCodec.decode(Conversation, conversation),
            participants=tuple(ThreadIncarnationRecord.decode(*pair) for pair in pairs),
        )
        messages[owner] = messages.get(owner, ReadCursor()).add((
            DisplayedConversation(conversation, tuple(sequences)),
        ))
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


class ReadLedgerKeys(Carry):
    """Read ledger keys move from JSON text to incarnation and conversation keys."""

    after = 'carry_goal_wait_owners'

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        ledger = ReadLedger.at(root)
        if not ledger.path.exists():
            return Carried(0)
        raw = json.loads(ledger.path.read_text())
        try:
            FieldCodec.decode(ReadDocument, raw)
        except (TypeError, ValueError):
            document = carried(raw)
        else:
            return Carried(0)
        if apply:
            ledger.replace(document)
        return Carried(len(raw["messages"]) + len(raw["transcripts"]),
                       {"message_reads": len(raw["messages"]), "transcript_reads": len(raw["transcripts"])},
                       (ledger.path.name,))


if __name__ == "__main__":
    ReadLedgerKeys.main()
