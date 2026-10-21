"""One-time JSONL history boundary; delete after every retained root is carried.

The retired envelope is decoded here only, never by the new bus readers.
Applying through the installer is refused until it supplies the HDD staging path.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Annotated, Literal

from agent_comms.audience_manifest import FrozenAudience
from agent_comms.bus import Bus, ClaimRecord, KeyedPrivateRecord, PrivateRecord, PublicRecord
from agent_comms.envelope_claim_transitions import ClaimTransition
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import (
    FieldCodec, MalformedRecord, RecordMember, SubclassUnion, WireName, WireOrder,
)
from agent_comms.installer.carry import Carried, Carry
from agent_comms.messages import BusRecord, Message
from agent_comms.response_acknowledgement import KeyedResponseAcknowledgement
from agent_comms.declared_family import DeclaredFamily
from agent_comms.wake_policy import (
    MessageAudience, NoWakeDecision, NoWakeReason, RecipientDecision, WakeDecision, WakePolicy,
)


class HistoryDecision(SubclassUnion, ABC, affix="HistoryDecision"):
    @abstractmethod
    def decision(self) -> RecipientDecision: ...


@dataclass(frozen=True)
class WakeHistoryDecision(RecordMember, HistoryDecision):
    recipient: Annotated[str, WireName("recipient_lookup")]
    audience: MessageAudience
    wake_mode: type[WakePolicy]

    def decision(self) -> WakeDecision:
        return WakeDecision(self.recipient, self.audience, self.wake_mode())


@dataclass(frozen=True)
class PassiveHistoryDecision(RecordMember, HistoryDecision):
    recipient: Annotated[str, WireName("recipient_lookup")]
    reason: Annotated[NoWakeReason, WireName("no_wake")]

    def decision(self) -> NoWakeDecision:
        return NoWakeDecision(self.recipient, self.reason)


@dataclass(frozen=True)
class HistoryManifest:
    wire_root_id: str
    manifest_codec: str
    resolver_version: str
    policy_version: str
    control: str
    audience: FrozenAudience
    decisions: tuple[HistoryDecision, ...]
    decisions_digest: str

    def restored(self, record: PrivateRecord) -> HistoryManifest:
        return replace(
            self, audience=record.audience, control=record.control,
            decisions=tuple(FieldCodec.decode(HistoryDecision, item.to_wire())
                            for item in record.decisions),
        )

    def capabilities(self) -> dict[str, object]:
        return dict(audience=self.audience, control=self.control,
                    decisions=tuple(item.decision() for item in self.decisions))


@dataclass(frozen=True, kw_only=True)
class HistorySideband(DeclaredFamily, ABC, affix="HistorySideband"):
    version: Literal[1]
    initial: HistoryManifest

    @abstractmethod
    def record(self, message: Message) -> PrivateRecord: ...

    def restored(self, record: PrivateRecord) -> HistorySideband:
        return replace(self, initial=self.initial.restored(record))


class InitialHistorySideband(HistorySideband):
    def record(self, message: Message) -> PrivateRecord:
        return PrivateRecord.from_message(message, **self.initial.capabilities())


@dataclass(frozen=True, kw_only=True)
class ResponseHistorySideband(HistorySideband):
    response: KeyedResponseAcknowledgement

    def record(self, message: Message) -> KeyedPrivateRecord:
        return KeyedPrivateRecord.from_message(
            message, acknowledgement=self.response, **self.initial.capabilities(),
        )

    def restored(self, record: KeyedPrivateRecord) -> ResponseHistorySideband:
        return replace(self, initial=self.initial.restored(record), response=record.acknowledgement)


class HistoryRow(SubclassUnion, Message, ABC, affix="HistoryRow"):
    """The unchanged public fields come from Message, not another field roster."""

    @abstractmethod
    def record(self) -> BusRecord: ...

    def restored(self, record: BusRecord) -> HistoryRow:
        return replace(self, **{item.name: getattr(record, item.name) for item in fields(Message)})

    def line(self) -> bytes:
        # The old writer put the two envelope identities before the declared fields.
        return json.dumps(
            {"seq": self.seq, "id": self.message_id, **FieldCodec.encode(self)},
            allow_nan=False,
        ).encode("utf-8") + b"\n"

    @classmethod
    def read(cls, raw: bytes, line: int) -> HistoryRow:
        try:
            if not raw.endswith(b"\n"):
                raise ValueError("incomplete final row")
            row = FieldCodec.decode(cls, FieldCodec.parse(raw))
            if row.line() != raw:
                raise ValueError("row is not byte-identical to its declared envelope")
            return row
        except (MalformedRecord, ValueError) as error:
            raise RelationViolationError(f"Bus history line {line}: {error}") from error


@dataclass(frozen=True)
class PublicHistoryRow(RecordMember, HistoryRow):
    def record(self) -> PublicRecord:
        return PublicRecord.from_message(self)


@dataclass(frozen=True, kw_only=True)
class ClaimHistoryRow(RecordMember, HistoryRow):
    claim_transition: Annotated[ClaimTransition, WireOrder(11)]

    def record(self) -> ClaimRecord:
        return ClaimRecord.from_message(self, claim_transition=self.claim_transition)

    def restored(self, record: ClaimRecord) -> ClaimHistoryRow:
        return replace(super().restored(record), claim_transition=record.claim_transition)


@dataclass(frozen=True, kw_only=True)
class PrivateHistoryRow(RecordMember, HistoryRow):
    private: Annotated[HistorySideband, WireName("_agent_comms_private_v1"), WireOrder(15)]

    def record(self) -> PrivateRecord:
        return self.private.record(self)

    def restored(self, record: PrivateRecord) -> PrivateHistoryRow:
        return replace(super().restored(record), private=self.private.restored(record))


class BusHistory(Carry):
    """Validate the retired stream before the new bus can be published."""

    filename = "bus.jsonl"

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        source = root / cls.filename
        cls.require_confined(root, source)
        return (source,)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        source = root / cls.filename
        cls.require_confined(root, source)
        if apply:
            raise RelationViolationError(
                "BusHistory needs the installer's spill_dir to stage bus.jsonl read-only "
                "before creating bus.sqlite3; Carry.carry currently receives only root/apply"
            )
        count = 0
        previous = 0
        with source.open("rb") as stream:
            for count, raw in enumerate(stream, 1):
                row = HistoryRow.read(raw, count)
                if row.seq <= 0 or (count > 1 and row.seq != previous + 1):
                    raise RelationViolationError(f"Bus history line {count}: sequence gap/repeat at {row.seq}")
                record = row.record()
                if row.restored(record).line() != raw:
                    raise RelationViolationError(f"Bus history seq {row.seq}: record round trip differs")
                previous = row.seq
        return Carried(0, detail=dict(rows=count, continuous=count, round_tripped=count))
