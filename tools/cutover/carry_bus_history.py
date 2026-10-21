"""One-time JSONL history boundary; delete after every retained root is carried.

The retired envelope is decoded here only, never by the new bus readers.
Applying through the installer is refused until it supplies the HDD staging path.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields, replace
from itertools import batched
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self, cast

from agent_comms.audience_manifest import FrozenAudience
from agent_comms.bus import Bus, ClaimRecord, KeyedPrivateRecord, PrivateRecord, PublicRecord
from agent_comms.bus_tables import BusRow
from agent_comms.declared_family import DeclaredFamily
from agent_comms.envelope_claim_transitions import (
    AcquiringClaim,
    ClaimIdentity,
    ClaimRelease,
    ClaimScope,
    ClaimTransition,
)
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import (
    FieldCodec,
    MalformedRecord,
    RecordMember,
    SubclassUnion,
    WireName,
    WireOmitDefault,
    WireOrder,
)
from agent_comms.historical_views import HistoryArchive, HistorySource
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile
from agent_comms.messages import BusRecord, Message
from agent_comms.private_path import FileObservation, FileRevision
from agent_comms.read_ledger import ReadLedger
from agent_comms.recorded_bus import RecordedBusIdentity
from agent_comms.registry_provenance import RegistryProvenance
from agent_comms.response_acknowledgement import KeyedResponseAcknowledgement
from agent_comms.store_files import _atomic_write_text, _store_lock
from agent_comms.typed_table import Column, DerivedTables, SharedFileTables, TypedTable
from agent_comms.wake_policy import (
    MessageAudience,
    NoWakeDecision,
    NoWakeReason,
    RecipientDecision,
    WakeDecision,
    WakePolicy,
)


@dataclass(frozen=True)
class HistoryMetadata:
    """Only the retired stream boundary is needed, never its root ID or activation."""

    last_seq: int
    claimed_after_seq: Annotated[int, WireName("admission_after_seq")]
    filename: ClassVar[str] = "bus_meta.json"

    def __post_init__(self) -> None:
        if not 0 <= self.claimed_after_seq <= self.last_seq:
            raise RelationViolationError("Retired claim boundary is outside its stream")

    @classmethod
    def read(cls, root: Path) -> HistoryMetadata:
        data = FieldCodec.parse((root / cls.filename).read_bytes())
        return FieldCodec.construct(
            cls,
            {
                name: value
                for plan in FieldCodec.field_plans(cls)
                for name, value in plan.decoded(data).items()
            },
        )


@dataclass(frozen=True)
class HistoryCatalogSource:
    """The retired archive catalogue, consumed once before its snapshots move."""

    root: str
    original_root: str
    wire_root_id: str
    bus_identity: RecordedBusIdentity
    size: int
    snapshot_bus_revision: FileObservation
    snapshot_registry_revision: FileObservation
    provenance: RegistryProvenance

    @classmethod
    def read(cls, root: Path, original_root: Path) -> tuple[HistoryCatalogSource, ...]:
        path = HistoryArchive.path_in(root)
        if not path.exists():
            return ()
        sources = FieldCodec.decode(
            tuple[HistoryCatalogSource, ...],
            FieldCodec.parse(path.read_bytes()),
        )
        for source in sources:
            source.mapped_root(root, original_root)
            source.require_snapshot()
        return sources

    @classmethod
    def publish(
        cls,
        root: Path,
        original_root: Path,
        sources: tuple[HistoryCatalogSource, ...],
        bindings: Mapping[RecordedBusIdentity, RecordedBusIdentity],
    ) -> Carried:
        path = HistoryArchive.path_in(root)
        if not path.exists():
            return Carried(0)
        converted = tuple(source.converted(root, original_root, bindings) for source in sources)
        # BusHistory holds the catalog lock from the old read through this publication.
        _atomic_write_text(path, json.dumps(FieldCodec.encode(converted)))
        return Carried(len(converted), rewritten=(HistoryArchive.filename,))

    def mapped_root(self, root: Path, original_root: Path) -> Path:
        original = Path(self.root)
        Carry.require_confined(original_root, original)
        mapped = root / original.relative_to(original_root)
        Carry.require_confined(root, mapped)
        return mapped

    def require_snapshot(self) -> None:
        original = Path(self.root)
        if (
            FileRevision.capture(original / BusHistory.filename) != self.snapshot_bus_revision
            or FileRevision.capture(RegistryFile.path_in(original))
            != self.snapshot_registry_revision
        ):
            raise RelationViolationError(f"Retired archive changed before carrying: {original}")

    def converted(
        self,
        root: Path,
        original_root: Path,
        bindings: Mapping[RecordedBusIdentity, RecordedBusIdentity],
    ) -> HistorySource:
        mapped = self.mapped_root(root, original_root)
        bus = Bus(Bus.path_in(mapped))
        source = HistorySource(
            str(mapped),
            self.original_root,
            bindings.get(self.bus_identity, self.bus_identity),
            RecordedBusIdentity.capture(bus.path),
            bus.revision(),
            FileRevision.capture(RegistryFile.path_in(mapped)),
            self.provenance,
        )
        source.validate()
        return source


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
            self,
            audience=record.audience,
            control=record.control,
            decisions=tuple(HistoryDecision.from_wire(item.to_wire()) for item in record.decisions),
        )

    def capabilities(self) -> dict[str, object]:
        return dict(
            audience=self.audience,
            control=self.control,
            decisions=tuple(item.decision() for item in self.decisions),
        )


@dataclass(frozen=True, kw_only=True)
class HistorySideband[Record: PrivateRecord = PrivateRecord](
    DeclaredFamily,
    ABC,
    affix="HistorySideband",
):
    version: Literal[1]
    initial: HistoryManifest

    @abstractmethod
    def record(self, message: Message) -> Record: ...

    def restored(self, record: Record) -> Self:
        return replace(self, initial=self.initial.restored(record))


class InitialHistorySideband(HistorySideband[PrivateRecord]):
    def record(self, message: Message) -> PrivateRecord:
        return PrivateRecord.from_message(message, **self.initial.capabilities())


@dataclass(frozen=True, kw_only=True)
class ResponseHistorySideband(HistorySideband[KeyedPrivateRecord]):
    response: KeyedResponseAcknowledgement

    def record(self, message: Message) -> KeyedPrivateRecord:
        return KeyedPrivateRecord.from_message(
            message,
            acknowledgement=self.response,
            **self.initial.capabilities(),
        )

    def restored(self, record: KeyedPrivateRecord) -> ResponseHistorySideband:
        return replace(self, initial=self.initial.restored(record), response=record.acknowledgement)


class HistoryRow[Record: BusRecord = BusRecord](
    SubclassUnion,
    Message,
    ABC,
    affix="HistoryRow",
):
    """The unchanged public fields come from Message, not another field roster."""

    ascii: ClassVar[bool] = True

    @abstractmethod
    def record(self) -> Record: ...

    def with_message(self, message: Message) -> Self:
        return replace(self, **{item.name: getattr(message, item.name) for item in fields(Message)})

    def restored(self, record: Record) -> Self:
        return self.with_message(record)

    def located(
        self, number: int, offset: int, raw: bytes, metadata: HistoryMetadata
    ) -> HistoryLine:
        return StoredHistoryLine(number, offset, raw, self)

    def line(self) -> bytes:
        # The old writer put the two envelope identities before the declared fields.
        return (
            json.dumps(
                {"seq": self.seq, "id": self.message_id, **FieldCodec.encode(self)},
                allow_nan=False,
                ensure_ascii=self.ascii,
            ).encode("utf-8")
            + b"\n"
        )

    @classmethod
    def read(cls, raw: bytes, line: int) -> Self:
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
class PublicHistoryRow(RecordMember, HistoryRow[PublicRecord]):
    # The old initializer wrote selected history with ensure_ascii=False.
    ascii: ClassVar[bool] = False

    def record(self) -> PublicRecord:
        return PublicRecord.from_message(self)


class OrderedHistoryMember(RecordMember):
    @classmethod
    def from_wire(cls, data: object) -> Self:
        row = FieldCodec.decode_record(cls, data)
        # Decode proved the mapping and required fields. Its compiled declaration
        # owns order; re-encoding every nested value merely to obtain keys is waste.
        source = cast(Mapping[str, object], data)
        declared = tuple(key for _, key in FieldCodec.record_fields(cls) if key in source)
        if tuple(source) != declared:
            raise MalformedRecord("Field order differs from this retired declaration")
        return row


class HistoryScope(SubclassUnion, ABC, affix="HistoryScope"):
    def restored(self, scope: ClaimScope) -> Self:
        return FieldCodec.decode_record(type(self), FieldCodec.encode(scope))


@dataclass(frozen=True, kw_only=True)
class ScopeOwner:
    owner_generation: Annotated[int, WireName("owner_admission_generation")]


@dataclass(frozen=True, kw_only=True)
class ScopeOperation:
    operation_id: str


@dataclass(frozen=True, kw_only=True)
class ScopeTurn:
    turn_id: str


@dataclass(frozen=True, kw_only=True)
class ScopeSource:
    wire_root_id: str
    source_seq: int
    source_message_id: str
    wake_claim_id: str
    wake_revision: int
    recipient_lookup: str
    execution_id: str


@dataclass(frozen=True, kw_only=True)
class ScopeAttempt:
    participant_generation: int
    attempt_ordinal: int
    version: Literal[1]


@dataclass(frozen=True, kw_only=True)
class ScopeInput:
    input_id: str


@dataclass(frozen=True)
class EarlierWakeHistoryScope(
    ScopeAttempt,
    ScopeTurn,
    ScopeOwner,
    ScopeOperation,
    ScopeSource,
    OrderedHistoryMember,
    HistoryScope,
):
    """Original wake claims placed the operation and owner inside the wake fields."""


@dataclass(frozen=True)
class WakeHistoryScope(
    ScopeAttempt,
    ScopeSource,
    ScopeTurn,
    ScopeOperation,
    ScopeOwner,
    OrderedHistoryMember,
    HistoryScope,
):
    """Later wake claims inherit the common owner/operation/turn fields first."""


@dataclass(frozen=True)
class NativeHistoryScope(
    ScopeInput,
    ScopeTurn,
    ScopeOperation,
    ScopeOwner,
    OrderedHistoryMember,
    HistoryScope,
):
    """Native claims use the same owner/operation/turn group, then the input."""


class HistoryClaim(SubclassUnion, ClaimIdentity, ABC, affix="HistoryClaim"):
    """Retired field-group order is part of the original envelope's physical form."""

    def current(self) -> ClaimTransition:
        return ClaimTransition.from_wire(FieldCodec.encode(self))

    def restored(self, transition: ClaimTransition) -> Self:
        # The known old member owns layout; the new record supplies every value.
        return FieldCodec.decode_record(type(self), FieldCodec.encode(transition))


class OrderedHistoryClaim(OrderedHistoryMember, HistoryClaim, declared_name=None):
    pass


@dataclass(frozen=True, kw_only=True)
class ClaimedPaths:
    claims: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ReleasedPaths:
    releases: tuple[ClaimRelease, ...]


@dataclass(frozen=True, kw_only=True)
class ClaimGeneration:
    generation: str


@dataclass(frozen=True, kw_only=True)
class ScopedClaimGeneration(ClaimGeneration, HistoryClaim, declared_name=None):
    scope: Annotated[HistoryScope, WireName("admission")]

    def restored(self, transition: ClaimTransition) -> Self:
        # This retired member decoded an acquiring scope, never a release.
        acquiring = cast(AcquiringClaim, transition)
        return replace(
            super().restored(transition), scope=self.scope.restored(acquiring.claim_scope)
        )


@dataclass(frozen=True, kw_only=True)
class ReleasedGeneration:
    generation: None


@dataclass(frozen=True, kw_only=True)
class ExpiredPaths:
    expired: Annotated[tuple[str, ...], WireOmitDefault()] = field(default=())


@dataclass(frozen=True)
class EarlierScopedHistoryClaim(
    ScopedClaimGeneration,
    ReleasedPaths,
    ClaimedPaths,
    ExpiredPaths,
    OrderedHistoryClaim,
):
    """The older writer placed acquired/released paths before their generation."""


@dataclass(frozen=True)
class ScopedHistoryClaim(
    ReleasedPaths,
    ClaimedPaths,
    ExpiredPaths,
    ScopedClaimGeneration,
    OrderedHistoryClaim,
):
    """The later writer factored the scoped generation into its nominal parent."""


@dataclass(frozen=True)
class UnscopedHistoryClaim(
    ClaimGeneration,
    ReleasedPaths,
    ClaimedPaths,
    ExpiredPaths,
    OrderedHistoryClaim,
):
    """Original tool claims before a scope was recorded."""


@dataclass(frozen=True)
class EarlierReleasedHistoryClaim(
    ReleasedGeneration,
    ReleasedPaths,
    ClaimedPaths,
    ExpiredPaths,
    OrderedHistoryClaim,
):
    """Earlier release originals encoded the empty claim side first."""


@dataclass(frozen=True)
class ReleasedHistoryClaim(
    ReleasedGeneration,
    ClaimedPaths,
    ReleasedPaths,
    ExpiredPaths,
    OrderedHistoryClaim,
):
    """Release originals keep their release side first and mint no generation."""


@dataclass(frozen=True, kw_only=True)
class ClaimHistoryRow(RecordMember, HistoryRow[ClaimRecord]):
    claim_transition: Annotated[HistoryClaim, WireOrder(11)]

    def record(self) -> ClaimRecord:
        return ClaimRecord.of(self, self.claim_transition.current())

    def located(
        self, number: int, offset: int, raw: bytes, metadata: HistoryMetadata
    ) -> HistoryLine:
        if self.seq <= metadata.claimed_after_seq:
            return UnclaimedHistoryLine(number, offset, raw, self)
        return super().located(number, offset, raw, metadata)

    def restored(self, record: ClaimRecord) -> ClaimHistoryRow:
        return replace(
            super().restored(record),
            claim_transition=self.claim_transition.restored(record.claim_transition),
        )


@dataclass(frozen=True, kw_only=True)
class PrivateHistoryRow(RecordMember, HistoryRow[PrivateRecord]):
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
class HistoryLine[Row: Message = Message](ABC):
    number: int
    offset: int
    raw: bytes
    row: Row

    @classmethod
    def rows(cls, source: Path, metadata: HistoryMetadata) -> Iterator[HistoryLine]:
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
                yield row.located(number, stream.tell() - len(raw), raw, metadata)
                previous = row.seq
        if previous != metadata.last_seq:
            raise RelationViolationError(
                f"Bus history ends at {previous}; retired marker declares {metadata.last_seq}"
            )

    @abstractmethod
    def record(self) -> BusRecord: ...

    @abstractmethod
    def restored_line(self, record: BusRecord) -> bytes: ...

    def verify(self, record: BusRecord) -> None:
        if record != self.record():
            raise RelationViolationError(
                f"Bus history seq {self.row.seq}: record differs from its declared template"
            )
        if self.restored_line(record) != self.raw:
            raise RelationViolationError(
                f"Bus history seq {self.row.seq}: record round trip differs"
            )


class StoredHistoryLine[Record: BusRecord](HistoryLine[HistoryRow[Record]]):
    """The template's declared record kind also owns the typed readback."""

    def record(self) -> Record:
        try:
            return self.row.record()
        except ValueError as error:
            raise RelationViolationError(f"Bus history line {self.number}: {error}") from error

    def restored_line(self, record: BusRecord) -> bytes:
        # verify compared the nominal value supplied by this template's factory.
        return self.row.restored(cast(Record, record)).line()


class UnclaimedHistoryLine(HistoryLine[ClaimHistoryRow]):
    """The retired boundary marks this claim as display history, not attestation."""

    def record(self) -> PublicRecord:
        return PublicRecord.from_message(self.row)

    def restored_line(self, record: BusRecord) -> bytes:
        # The discarded attestation is only the old encoding template. Every
        # retained Message value must still come back from SQLite unchanged.
        return self.row.with_message(record).line()


@dataclass(frozen=True)
class HistoryInput:
    """One captured source keeps its marker and original read binding across the move."""

    bus: Bus
    source: Path
    metadata: HistoryMetadata
    original_identity: RecordedBusIdentity

    @classmethod
    def capture(cls, root: Path, original_root: Path) -> tuple[HistoryInput, ...]:
        source = root / BusHistory.filename
        Carry.require_confined(root, source)
        original = original_root / BusHistory.filename
        Carry.require_confined(original_root, original)
        bus = Bus(Bus.path_in(root))
        if not source.is_file() and not original.is_file():
            # A fresh SQLite bus has no retired history. An interrupted cutover
            # also lacks JSONL here, but its offset table proves input was moved.
            if bus.path.is_file():
                with bus.observing() as db:
                    if HistoryOffset.present(db):
                        raise RelationViolationError(
                            "History input was already staged; restore its backup "
                            f"before retry: {source}"
                        )
            return ()
        if not source.is_file() or not original.is_file():
            raise RelationViolationError(f"History source or its original is absent: {source}")
        return (
            cls(bus, source, HistoryMetadata.read(root), RecordedBusIdentity.capture(original)),
        )

    def moved(self, root: Path, spill: Path) -> HistoryInput:
        target = spill / self.source.relative_to(root)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        Carry.require_confined(spill, target)
        for source in (self.source, self.source.parent / HistoryMetadata.filename):
            source.chmod(0o400)
            shutil.move(source, target.parent / source.name)
        return replace(self, source=target)


class BusHistory(Carry):
    """Validate and stream the retired history through the bus's one insert path."""

    filename = "bus.jsonl"

    def backup_files(self) -> tuple[Path, ...]:
        root = self.root
        sources = HistoryCatalogSource.read(root, self.original_root)
        roots = (root, *(source.mapped_root(root, self.original_root) for source in sources))
        paths = {HistoryArchive.path_in(root)}
        for directory in roots:
            paths.update(
                (
                    directory / self.filename,
                    directory / HistoryMetadata.filename,
                    ReadLedger.path_in(directory),
                )
            )
        for path in paths:
            self.require_confined(root, path)
        return tuple(sorted(paths))

    @staticmethod
    def rebind_reads(root: Path, original: RecordedBusIdentity) -> Carried:
        """Replace one matching JSONL binding; ReadLedger owns every retained row."""
        ledger = ReadLedger.at(root)
        if not ledger.path.exists():
            return Carried(0)
        destination = Bus.path_in(root)
        if not destination.is_file():
            raise RelationViolationError("Read binding needs the verified SQLite bus")
        converted = RecordedBusIdentity.capture(destination)
        with ledger.locked():
            document = ledger._read_unlocked()
            if not document.bus_identity.matches(original):
                return Carried(0)
            rebound = replace(document, bus_identity=converted)
            if rebound == document:
                return Carried(0)
            ledger._publish_unlocked(rebound)
        return Carried(
            1,
            detail={
                "read_viewers_before": len(document.messages),
                "read_viewers_after": len(rebound.messages),
                "read_conversations_before": sum(
                    len(c.conversations) for c in document.messages.values()
                ),
                "read_conversations_after": sum(
                    len(c.conversations) for c in rebound.messages.values()
                ),
                "read_transcripts_before": len(document.transcripts),
                "read_transcripts_after": len(rebound.transcripts),
            },
            rewritten=(ledger.filename,),
        )

    def carry(self) -> Carried:
        with _store_lock(HistoryArchive.path_in(self.root)):
            catalog = HistoryCatalogSource.read(self.root, self.original_root)
            roots = (
                self.root,
                *(source.mapped_root(self.root, self.original_root) for source in catalog),
            )
            inputs = tuple(
                source
                for root in roots
                for source in HistoryInput.capture(
                    root, self.original_root / root.relative_to(self.root)
                )
            )
            if not inputs:
                return Carried(0)
            if not self.apply:
                count = 0
                for source in inputs:
                    for line in HistoryLine.rows(source.source, source.metadata):
                        line.verify(line.record())
                        count += 1
                return Carried(0, detail=dict(rows=count, continuous=count, round_tripped=count))
            spill = self.spill_dir.resolve(strict=True)
            if spill.is_relative_to(self.root):
                raise RelationViolationError("History spill must be outside the bus")
            stage = Path(tempfile.mkdtemp(prefix=f"{self.name()}-", dir=spill))
            # Every JSONL leaves the desktop before the first SQLite file is created.
            staged = tuple(source.moved(self.root, stage) for source in inputs)
            results = []
            read_results = []
            bindings = {}
            rewritten: set[str] = set()
            for source in staged:
                result = self.transfer(source.source, source.bus, source.metadata)
                read = self.rebind_reads(source.bus.path.parent, source.original_identity)
                results.append(result)
                read_results.append(read)
                bindings[source.original_identity] = RecordedBusIdentity.capture(source.bus.path)
                directory = source.bus.path.parent.relative_to(self.root)
                rewritten.update(
                    str(directory / name)
                    for name in (
                        self.filename,
                        HistoryMetadata.filename,
                        *result.rewritten,
                        *read.rewritten,
                    )
                )
            catalog_result = HistoryCatalogSource.publish(
                self.root, self.original_root, catalog, bindings
            )
            rewritten.update(catalog_result.rewritten)
            totals: dict[str, int] = {}
            for result in (*results, *read_results):
                for key, value in result.detail.items():
                    totals[key] = totals.get(key, 0) + value
            return Carried(
                sum(result.changed for result in results),
                detail=totals,
                rewritten=tuple(sorted(rewritten)),
            )

    @classmethod
    def transfer(cls, source: Path, bus: Bus, metadata: HistoryMetadata) -> Carried:
        """The staged source stays immutable; interrupted batches resume by original seq.

        The installer stages every family source before this path runs. B2 owns the
        scoped schema/read lifetime; Claimed is written only above the retired floor.
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
        for batch in batched(HistoryLine.rows(source, metadata), 1000, strict=False):
            records = tuple(line.record() for line in batch)
            with bus.reading() as reader:
                for line in batch:
                    if line.row.seq <= committed:
                        line.verify(reader.lookup(line.row.seq))
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
            with bus.reading() as reader:
                for line in batch:
                    line.verify(reader.lookup(line.row.seq))
                    count = line.number
        with bus.observing() as db:
            if BusRow.count(db) != count or HistoryOffset.count(db) != count:
                raise RelationViolationError(
                    f"Bus history line count {count} differs from stored rows/offsets"
                )
        return Carried(
            changed,
            detail=dict(rows=count, continuous=count, round_tripped=count),
            rewritten=(bus.path.name,),
        )
