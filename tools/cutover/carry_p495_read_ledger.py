"""Read cursors gain their conversation record (P495): each viewer's per-conversation
cursors move under ``ReadCursor.conversations``. Every message and native read position
is preserved. No production reader accepts the previous shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .carry import Carried, Carry

from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.read_basis import ChannelConversation, DMConversation, ConversationCursor, ReadCursor
from agent_comms.read_ledger import ReadDocument, ReadLedger
from agent_comms.recorded_bus import NoRecordedBus, RecordedBusIdentity
from agent_comms.thread_identity import RecordedThreadIncarnation, ThreadIncarnation


@dataclass(frozen=True)
class PreviousConversation:
    target: str = ''
    participants: tuple[ThreadIncarnation, ...] = ()

    def current(self):
        if self.participants:
            if len(self.participants) != 2 or self.target:
                raise ValueError('Invalid previous direct conversation')
            return DMConversation(self.participants)
        if not self.target.startswith('#'):
            raise ValueError('Invalid previous channel conversation')
        return ChannelConversation(self.target)


@dataclass(frozen=True)
class PreviousReadDocument:
    messages: dict[RecordedThreadIncarnation, dict[PreviousConversation, ConversationCursor]] = field(default_factory=dict)
    bus_identity: RecordedBusIdentity = field(default_factory=NoRecordedBus)
    transcripts: dict[tuple[str, str, int], int] = field(default_factory=dict)
    notice: str | None = None


class ReadLedgerConversations(Carry):
    after = "carry_earlier_projects"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        ledger = ReadLedger.at(root)
        if not ledger.path.exists():
            return Carried(0)
        with ledger.locked():
            parsed = FieldCodec.parse(ledger.path.read_bytes())
            try:
                FieldCodec.decode(ReadDocument, parsed)
            except MalformedRecord:
                pass
            else:
                return Carried(0)
            previous = FieldCodec.decode(PreviousReadDocument, parsed)
            current = ReadDocument(
                {owner: ReadCursor({key.current(): value for key, value in conversations.items()})
                 for owner, conversations in previous.messages.items()},
                previous.bus_identity, previous.transcripts, previous.notice,
            )
            if apply:
                ledger._publish_unlocked(current)
        if apply and ledger.read() != current:
            raise ValueError("Read ledger carry did not preserve every read position")
        viewers = len(current.messages)
        return Carried(viewers, {"viewers": viewers}, (ledger.path.name,) if apply else ())


if __name__ == "__main__":
    ReadLedgerConversations.main()
