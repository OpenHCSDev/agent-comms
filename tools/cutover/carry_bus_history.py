"""One-time JSONL history boundary; delete after every retained root is carried.

The retired envelope is decoded here only, never by the new bus readers.
Applying through the installer is refused until it supplies the HDD staging path.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields, replace
from itertools import batched
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from agent_comms.audience_manifest import FrozenAudience
from agent_comms.bus import Bus, ClaimRecord, KeyedPrivateRecord, PrivateRecord, PublicRecord
from agent_comms.bus_tables import BusRow
from agent_comms.declared_family import DeclaredFamily
from agent_comms.envelope_claim_transitions import ClaimTransition
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import (
    FieldCodec,
    MalformedRecord,
    RecordMember,
    SubclassUnion,
    WireName,
    WireOrder,
)
from agent_comms.installer.carry import Carried, Carry
from agent_comms.messages import BusRecord, Message
from agent_comms.response_acknowledgement import KeyedResponseAcknowledgement
from agent_comms.typed_table import Column, DerivedTables, SharedFileTables, TypedTable
from agent_comms.wake_policy import (
    MessageAudience,
    NoWakeDecision,
    NoWakeReason,
    RecipientDecision,
    WakeDecision,
    WakePolicy,
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

    ascii: ClassVar[bool] = True

    @abstractmethod
    def record(self) -> BusRecord: ...

    def restored(self, record: BusRecord) -> HistoryRow:
        return replace(self, **{item.name: getattr(record, item.name) for item in fields(Message)})

    def line(self) -> bytes:
        # The old writer put the two envelope identities before the declared fields.
        return json.dumps(
            {"seq": self.seq, "id": self.message_id, **FieldCodec.encode(self)},
            allow_nan=False, ensure_ascii=self.ascii,
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
    # The old initializer wrote selected history with ensure_ascii=False.
    ascii: ClassVar[bool] = False

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


@dataclass(frozen=True)
class HistoryOffset(TypedTable, SharedFileTables, DerivedTables):
    """One-use address translation, dropped after B1 consumes it."""

    offset: Annotated[int, Column(primary_key=True)]
    seq: Annotated[int, Column(unique=True)]


@dataclass(frozen=True)
class HistoryLine:
    number: int
    offset: int
    raw: bytes
    row: HistoryRow

    @classmethod
    def rows(cls, source: Path):
        previous = 0
        number = 0
        with source.open("rb") as stream:
            while raw := stream.readline():
                number += 1
                row = HistoryRow.read(raw, number)
                if row.seq <= 0 or (number > 1 and row.seq != previous + 1):
                    raise RelationViolationError(
                        f"Bus history line {number}: sequence gap/repeat at {row.seq}"
                    )
                yield cls(number, stream.tell() - len(raw), raw, row)
                previous = row.seq

    def verify(self, record: BusRecord) -> None:
        if self.row.restored(record).line() != self.raw:
            raise RelationViolationError(
                f"Bus history seq {self.row.seq}: record round trip differs"
            )


class BusHistory(Carry):
    """Validate and stream the retired history through the bus's one insert path."""

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
        for line in HistoryLine.rows(source):
            line.verify(line.row.record())
            count = line.number
        return Carried(0, detail=dict(rows=count, continuous=count, round_tripped=count))

    @classmethod
    def transfer(cls, source: Path, bus: Bus) -> Carried:
        """The staged source stays immutable; interrupted batches resume by original seq.

        Installer entry remains refused until HDD staging and activation are connected.
        B2 owns the scoped bus schema; C's Claimed carry is not connected yet.
        """
        if source.resolve() != source or source.stat().st_mode & 0o222:
            raise RelationViolationError(
                "Bus history source must be a physical read-only staged file"
            )
        with bus.writing() as db:
            if not HistoryOffset.present(db):
                HistoryOffset.create(db)
            HistoryOffset.require_schema(db)
            committed = BusRow.last_seq(db)
        count = 0
        changed = 0
        # File reads, field decoding and record construction happen outside the write lock.
        for batch in batched(HistoryLine.rows(source), 1000, strict=False):
            records = tuple(line.row.record() for line in batch)
            for line in batch:
                if line.row.seq <= committed:
                    line.verify(bus.lookup(line.row.seq))
            with bus.writing() as db:
                for line, record in zip(batch, records, strict=True):
                    if record.seq > committed:
                        seq = record.append_to(db, record.seq)
                        if seq != record.seq:
                            raise RelationViolationError(
                                f"Bus history seq {record.seq}: duplicate publication key "
                                f"resolves to {seq}"
                            )
                        HistoryOffset(line.offset, seq).insert(db)
                        changed += 1
                    elif HistoryOffset.select(db, offset=line.offset) != [
                        HistoryOffset(line.offset, record.seq)
                    ]:
                        raise RelationViolationError(
                            f"Bus history seq {record.seq}: committed offset mapping differs"
                        )
            for line in batch:
                line.verify(bus.lookup(line.row.seq))
                count = line.number
        with bus.observing() as db:
            if BusRow.count(db) != count or HistoryOffset.count(db) != count:
                raise RelationViolationError(
                    f"Bus history line count {count} differs from stored rows/offsets"
                )
        return Carried(changed, detail=dict(rows=count, continuous=count, round_tripped=count),
                       rewritten=(bus.path.name,))
