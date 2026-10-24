"""Verify and unbox delivered participants before the sole audience reader opens them."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.audience_manifest import (
    AUDIENCE_VERSION,
    Audience,
    AudiencedMessage,
    FrozenRecipient,
)
from agent_comms.bus import Bus
from agent_comms.bus_publication import Delivered
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, MessageId, ThreadLookup
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import Message
from agent_comms.typed_table import TypedRow


@dataclass(frozen=True)
class StoredAudience:
    """The retired version-one audience column, read only by this carry."""

    wire_seq: int
    message_id: MessageId
    exact_target: str
    sender_lookup: ThreadLookup
    sender_name: str
    source_revision: str
    recipients: tuple[FrozenRecipient, ...]
    wire_envelope_digest: str
    digest: str
    version: int = AUDIENCE_VERSION

    def verified(self, message: Message) -> AudiencedMessage:
        current = AudiencedMessage.from_message(
            message, sender_lookup=self.sender_lookup,
            source_revision=self.source_revision, recipients=self.recipients,
        )
        expected = (
            AUDIENCE_VERSION, current.seq, current.message_id, current.target, current.sender,
            current.wire_envelope_digest, current.digest,
        )
        stored = (
            self.version, self.wire_seq, self.message_id, self.exact_target, self.sender_name,
            self.wire_envelope_digest, self.digest,
        )
        if stored != expected:
            raise RelationViolationError(
                f"Delivered audience differs from message at seq {message.seq}"
            )
        return current


@dataclass(frozen=True)
class StoredDelivery(TypedRow):
    seq: int
    audience: StoredAudience


class DeliveredAudience(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (Bus,)

    def carry(self, *, apply: bool) -> Carried:
        changed = 0
        verified = 0
        converted: dict[Path, list[AudiencedMessage]] = {}
        rewritten: list[Path] = []
        delivered = Delivered.table
        for path in self.input_files():
            with closing(Bus.read_snapshot(path)) as db:
                if not delivered.present(db) or "audience" not in delivered.stored_columns(db):
                    continue
                audiences = [
                    stored.audience.verified(Message.read_from(db, stored.seq))
                    for stored in StoredDelivery.read(db.execute(
                        f'SELECT seq,audience FROM "{delivered.declared_name}" ORDER BY seq'
                    ))
                ]
            verified += len(audiences)
            changed += 1
            converted[path] = audiences
        if apply:
            for path, audiences in converted.items():
                with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
                    Audience.table.create(db)
                    for message in audiences:
                        FieldCodec.construct(Audience.table, {
                            "seq": message.seq, "sender_lookup": message.sender_lookup,
                            "source_revision": message.source_revision,
                            "recipients": message.recipients,
                        }).insert(db)
                    delivered.rebuild(db)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail={"verified": verified}, rewritten=tuple(rewritten))
