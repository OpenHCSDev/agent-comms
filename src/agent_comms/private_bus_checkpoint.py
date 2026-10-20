"""Keep a checked SQLite index of the private bus bytes written so far.

The JSONL file is the record of messages. Only WireLog's private writer may
advance the index after flushing the bus and its directory under the bus lock.
The saved bus marker binds the SQLite file identity and checked byte range;
a matching bus revision alone does not make the SQL rows trustworthy.
An unfinished write requires rebuilding the index. If only the bus has grown,
hash the saved prefix before indexing the added bytes. Reject unexpected changes
to the index file instead of silently rebuilding. Reading this index neither
submits native input nor marks a message handled.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from abc import ABC, abstractmethod
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, BinaryIO, ClassVar, Generic, Literal, TypeVar

from .bus_source_page import AddressedPage
from .checkpoint_seals import FinalSeal, PendingSeal, PrefixSeal, PrefixSource
from .coordination_database import CoordinationStore
from .coordination_errors import IdentityConflict
from .envelope_claim_transitions import (
    ClaimOwner, ClaimProjection, ClaimTransition, ClaimTransitionError,
)
from .coordination_tables.publications import PublicationIntents
from .errors import CheckpointNeedsRepairError, HumanClaimBlockedError, RelationViolationError
from .private_path import FileIdentity, FileRevision
from .store_files import ExclusiveLock, LockMode, SharedLock
from .field_codec import FieldCodec, MalformedRecord
from .typed_table import Column, ForeignKey, Index, SQLiteSchemaObject, SqlStorage, SqlValue, TypedTable
from .wire_metadata import WireMetadata
from .wire_record import WireRecord
from .thread_identity import ThreadIncarnation
from .turn_context import ContextEntryManifest, RecordedContextManifest

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .wire_log import WireLog
    from .messages import Message
    from .retained_task_facts import ExactTaskFact
    from .registration import Registration
    from .turn_context import ContextEntryReference, ContextManifest, SegmentManifest

OriginalSource = TypeVar("OriginalSource")

_SEED = hashlib.sha256(b"agent-comms:private-bus-prefix:v1\0").digest()
_TAIL_BYTES = 4096


@dataclass(frozen=True)
class CapturedWireSource[OriginalSource, Value = OriginalSource]:
    """One inspection's original bytes and decoded value; no open store or permit."""

    source: PrefixSource
    pointer: WireSourcePointer[OriginalSource]
    raw: bytes
    value: Value


@dataclass(frozen=True)
class PrefixWitness(PrefixSeal):
    # The high-water is a derived page fact, not a persistent seal field.
    latest_source_seq: int = field(default=0, compare=False, metadata={"seal_exclude": True})

    @property
    def device(self) -> int:
        return self.revision.identity.device

    @property
    def inode(self) -> int:
        return self.revision.identity.inode

    @property
    def offset(self) -> int:
        return self.revision.size

    def seal(self) -> PrefixSeal:
        return FieldCodec.decode(PrefixSeal, FieldCodec.project(self, "seal"))

    def require_marker(self, marker: WireMetadata) -> None:
        if self.root_id != marker.root_id or self.through_seq != marker.last_seq:
            raise RelationViolationError("Checked bus prefix differs from its stored marker.")

    def require_source(self, marker: WireMetadata, info: os.stat_result, stream: BinaryIO) -> None:
        """Validate the logical prefix independently of its physical file identity."""
        if self.root_id != marker.root_id or info.st_size < self.offset:
            raise RelationViolationError("Private bus checkpoint root/size changed.")
        if _tail(stream, self.offset) != self.tail:
            raise RelationViolationError("Private bus checkpoint prefix tail changed.")
        if self.through_seq > marker.last_seq:
            raise RelationViolationError("Private bus checkpoint exceeds the durable sequence marker.")

    def rebind_source(self, bus: WireLog, marker: WireMetadata, db: sqlite3.Connection,
                      info: os.stat_result) -> PrefixWitness:
        """Publish the unchanged prefix's new revision under the exclusive bus lock."""
        rebound = replace(self, revision=replace(FileRevision.from_stat(info), size=self.offset))
        return CheckedPrefix.commit_tail(bus, marker, db, self, rebound, [])

    def index_tail(self, bus: WireLog, marker: WireMetadata, db: sqlite3.Connection,
                   path: Path, info: os.stat_result) -> PrefixWitness:
        """Continue the proved cut, committing bounded chunks without a prefix rescan."""
        saved = self
        if saved.offset == 0 and saved.digest != _SEED.hex():
            raise RelationViolationError("Private bus has no proved empty prefix.")
        digest = bytes.fromhex(saved.digest)
        last_seq = saved.through_seq
        additions: list[tuple[int, bytes, WireRecord]] = []
        size = 0
        end = marker.seal.recovery_end(info)
        revision = FileRevision.from_stat(info)
        with bus.path.open("rb") as stream:
            def commit(cut: int) -> None:
                nonlocal saved, additions, size
                if (FileRevision.from_file(stream) != revision
                        or FileRevision.from_stat(bus.path.stat()) != revision):
                    raise RelationViolationError("Private bus changed during tail recovery.")
                expected = replace(
                    saved, revision=replace(revision, size=cut), through_seq=last_seq,
                    digest=digest.hex(), tail=_tail(stream, cut))
                saved = CheckedPrefix.commit_tail(bus, marker, db, saved, expected, additions)
                additions, size = [], 0

            def collect(offset: int, raw: bytes, record: WireRecord) -> None:
                nonlocal digest, last_seq, size
                digest = _chain(digest, raw)
                last_seq = record.sequence_after(last_seq)
                if last_seq > marker.last_seq:
                    raise RelationViolationError("Private bus tail exceeds its reserved sequence.")
                additions.append((offset, raw, record))
                size += len(raw)
                cut = offset + len(raw)
                if marker.seal.chunk_complete(cut, size, len(additions)):
                    commit(cut)

            for _ in bus.verified_records_unlocked(
                marker, on_row=collect, offset=saved.offset,
                previous_sequence=saved.through_seq, end=end,
            ):
                pass
            if additions or saved.revision != replace(revision, size=end):
                commit(end)
            else:
                marker.seal.require_rebuilt(saved, saved)
                marker.seal_with(FinalSeal.capture(saved, path))
                bus.write_metadata_unlocked(marker)
        return marker.seal.recover(bus, marker, db, path, saved, info)

    def require_append(self, marker: WireMetadata, info: os.stat_result,
                       offset: int, sequence: int) -> None:
        if self.source_identity != PrefixSource(marker.root_id, info.st_dev, info.st_ino):
            raise RelationViolationError("Private bus checkpoint append lost its source fence.")
        if offset != self.offset or sequence < self.through_seq:
            raise RelationViolationError("Private bus checkpoint append lost its prefix fence.")

    def require_read_window(self, selected: PrefixWitness) -> None:
        """Require the selected read to stay within this bus's checked byte range."""
        if self.source_identity != selected.source_identity:
            raise RelationViolationError("Addressed window changed its original source.")
        if selected.offset > self.offset or selected.through_seq > self.through_seq:
            raise RelationViolationError("Addressed window exceeds its committed source cut.")


@dataclass(frozen=True)
class CheckedRead:
    """Bus bytes and their index, opened and checked under the same lock."""

    path: Path
    marker: WireMetadata
    connection: sqlite3.Connection
    stream: BinaryIO
    witness: PrefixWitness

    def require_marker(self, marker: WireMetadata) -> None:
        if self.marker != marker:
            raise RelationViolationError("Original source marker changed within its lock.")

    def require_current(self) -> None:
        self.require_open_prefix()
        self.witness.require_marker(self.marker)

    def require_open_prefix(self) -> None:
        """Validate the acquired committed bytes independently of reservations.

        Acquisition already checked root and that the sealed sequence does not
        exceed the marker. A marker may reserve a later sequence before append;
        that reservation cannot change this original committed prefix.
        """
        if self.stream.closed:
            raise RelationViolationError("checked source read has left its lock lifetime.")
        self.marker.seal.check_final(self.witness, _path(self.path))
        if (
            FileRevision.from_stat(os.fstat(self.stream.fileno())) != self.witness.revision
            or FileRevision.from_stat(self.path.stat()) != self.witness.revision
        ):
            raise RelationViolationError("Bus bytes changed after this read was checked.")

    def install_at(self, bus: WireLog) -> PrefixWitness:
        """Install checked replacement history while holding the destination's bus lock.

        Maintenance first checks that the replacement preserves the saved history.
        Both buses must have current checked indexes and matching marker fields
        apart from their index checks. Copy rather than move the replacement
        bytes, so they remain available if publication fails. Use the current
        decoder and index-checking code rather than a separate upgrade reader.
        """
        self.require_current()
        if bus.path.resolve() == self.path.resolve():
            raise RelationViolationError("Carry destination must differ from its source")
        expected = replace(self.marker, checkpoint_version=None, checkpoint_seal=None)
        with bus.locked() as lock:
            prior = lock.checked_read
            prior.require_current()
            prior.require_marker(bus.private_marker_unlocked())
            if replace(prior.marker, checkpoint_version=None, checkpoint_seal=None) != expected:
                raise RelationViolationError("Carry changed destination marker facts")
            with tempfile.TemporaryDirectory(prefix=".checkpoint-carry-", dir=bus.path.parent) as stage:
                staged = Path(stage) / bus.path.name
                fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                digest = _SEED
                self.stream.seek(0)
                with os.fdopen(fd, "wb") as output:
                    for _, raw in WireRecord.rows(self.stream):
                        output.write(raw)
                        digest = _chain(digest, raw)
                    output.flush()
                    os.fsync(output.fileno())
                if digest.hex() != self.witness.digest:
                    raise RelationViolationError("Carried bytes differ from their prefix record")
                self.require_current()
                prior.require_current()
                prior.require_marker(bus.private_marker_unlocked())
                try:
                    # Keep the required old seal and index until the shared
                    # installer publishes the new pair. Any failed replacement
                    # remains sealed-but-inconsistent, so readers must refuse.
                    os.replace(staged, bus.path)
                    _directory_sync(bus.path)
                    witness = CheckedPrefix._install(bus, prior.marker)
                    self.require_current()
                    return witness
                except (OSError, sqlite3.Error, RelationViolationError) as error:
                    raise RelationViolationError(
                        "Carried checkpoint installation outcome UNKNOWN; preserve both roots"
                    ) from error

    def committed_sequence(self) -> int:
        """Global committed high-water; silent observations allocate no sequence."""
        self.require_open_prefix()
        return self.witness.through_seq

    def require_new_human_message(self, sender: str, message_id: str) -> None:
        """Check publication against the sealed index, without reading history.

        A reserved sequence without a committed row is still UNKNOWN. A later
        sender skipping that sequence leaves a permanent indexed gap; it must
        not make the unresolved human input disappear.
        """
        self.require_open_prefix()
        PublicMessages.require_contiguous(self.connection)
        if self.marker.last_seq != self.witness.through_seq:
            raise HumanClaimBlockedError(
                "Private bus sequence reservation has UNKNOWN outcome; "
                "human send blocked, do not retry."
            )
        PublicMessages.require_unused(self.connection, sender, message_id)

    def claim(self, resource: str) -> ClaimOwner | None:
        """Look up the current claim for a resource in the checked index."""
        self.require_open_prefix()
        return ResourceClaims.owner_for(self.connection, resource)

    def claim_operation_used(self, operation_id: str) -> bool:
        self.require_open_prefix()
        return ClaimCommits.operation_used(self.connection, operation_id)

    def prepare_claim(self, transition: ClaimTransition) -> None:
        self.require_open_prefix()
        ClaimCommits.prepare(self.connection, transition)

    def claim_projection(self) -> ClaimProjection:
        """Explicit current-claim inspection; no historical bus rows are decoded."""
        self.require_open_prefix()
        return ResourceClaims.all_claims(self.connection)

    def capture_sources(self, rows: tuple[WireSourcePointer[OriginalSource], ...]) -> Iterator[OriginalSource]:
        """Copy selected row bytes without keeping the lock or database open to decode them.

        The iterator holds only row locations, copied bytes and the checked bus
        identity. It cannot grant permission to write or start a turn. Copy the
        bytes under the locks; decode them after releasing those locks.
        """
        return (resource.value for resource in self.capture_resources(rows))

    def capture_resources(
        self, rows: tuple[WireSourcePointer[OriginalSource], ...], *,
        previous: tuple[CapturedWireSource[OriginalSource], ...] = (),
    ) -> Iterator[CapturedWireSource[OriginalSource]]:
        """Check selected rows again and decode only those whose bytes changed.

        Compare row bytes directly to decide reuse, rather than a shared revision
        or calculated signature. Check the bus and select row locations on every
        read. Decoding uses copied bytes without needing a database or bus lock.
        """
        self.require_current()
        captured = tuple((row, row.read_bytes(self.stream)) for row in rows)
        source = self.witness.source_identity
        self.require_current()
        retained = {item.pointer: item for item in previous if item.source == source}

        def decoded():
            for row, raw in captured:
                prior = retained.get(row)
                if prior is not None and prior.raw == raw:
                    yield prior
                else:
                    yield CapturedWireSource(source, row, raw,
                                             row.decode_bytes(raw, source.root_id))

        return decoded()

    def references(self, references) -> Iterator[CommittedDelivery]:
        """Capture exact seq/id originals through the existing sealed pointers."""
        marks = ",".join("?" for _ in references)
        self.require_current()
        rows = DeliverySources.select(
            self.connection, where=f"seq IN ({marks})",
            parameters=tuple(ref.seq for ref in references),
        )
        by_sequence = {row.seq: row for row in rows}
        ordered = []
        for reference in references:
            row = by_sequence.get(reference.seq)
            if row is None or row.reference != reference:
                raise RelationViolationError("Notification reference is not its original source")
            ordered.append(row)
        return self.capture_sources(tuple(ordered))

    def replies(self, references) -> Iterator[CommittedDelivery]:
        """Capture the deliveries that name any of these originals as answered.

        The index selects by sequence only; each decoded reply still carries
        its own reference, and its consumer checks it against the original.
        """
        marks = ",".join("?" for _ in references)
        self.require_current()
        rows = DeliverySources.read(self.connection.execute(
            f"SELECT w.* FROM {DeliverySources.declared_name} w "
            f"JOIN {Replies.declared_name} r ON r.seq=w.seq "
            f"WHERE r.answered_seq IN ({marks}) ORDER BY w.seq",
            tuple(reference.seq for reference in references),
        ))
        return self.capture_sources(tuple(rows))

    def delivery(self, seq: int) -> CommittedDelivery:
        """Resolve an exact source sequence through this original opened proof."""
        self.require_current()
        row = DeliverySources.one(self.connection, seq=seq)
        if row is None:
            raise RelationViolationError("No committed initial sideband for this wire sequence.")
        original, = self.capture_sources((row,))
        self.require_current()
        return original

    def _addressed_after(self, bus: WireLog, lookup: str, after_seq: int) -> sqlite3.Cursor:
        """Select a recipient's newer message sequences from the checked address index."""
        self.require_current()
        self.require_marker(bus.private_marker_unlocked())
        try:
            return self.connection.execute(
                f"SELECT a.seq FROM {Addressed.declared_name} a "
                "WHERE a.lookup=? AND a.seq>? AND a.seq<=? ORDER BY a.seq",
                (lookup, after_seq, self.witness.through_seq),
            )
        except sqlite3.Error as error:
            raise RelationViolationError("checked addressed index is unavailable.") from error

    def addressed_sequences(self, bus: WireLog, lookup: str, after_seq: int) -> tuple[int, ...]:
        """Return committed message sequences for a recipient without reading message bytes.

        Callers use the index to select rows, then read and decode only those rows.
        """
        rows = SqlValue.for_annotation(int).read(self._addressed_after(bus, lookup, after_seq))
        self.require_current()
        return tuple(rows)

    def addressed_page(
        self, bus: WireLog, request: AddressedPage, *, prefix: PrefixWitness | None = None,
    ):
        """Read a limited page of a recipient's messages from the checked index.

        Reading neither submits input nor marks messages handled. The page and
        last addressed sequence come from the same checked bus state. Return
        only the selected bytes, without leaving a connection or file open.
        """
        self.require_current()
        self.require_marker(bus.private_marker_unlocked())
        db, witness = self.connection, self.witness
        window = witness if prefix is None else prefix
        witness.require_read_window(window)
        try:
            rows = DeliverySources.read(db.execute(
                f"SELECT {','.join('i.' + column for column in DeliverySources.columns())} "
                f"FROM {Addressed.declared_name} a JOIN {DeliverySources.declared_name} i ON i.seq=a.seq "
                "WHERE a.lookup=? AND i.seq>? AND i.seq<=? ORDER BY i.seq LIMIT ?",
                (request.lookup, request.after_seq, window.through_seq, request.limit + 1),
            ))
            last = DeliverySources.read(db.execute(
                f"SELECT * FROM {DeliverySources.declared_name} ORDER BY seq DESC LIMIT 1"))
            latest = last[0].seq if last else 0
            if not 0 <= latest <= witness.through_seq:
                raise RelationViolationError("checked initial high-water is invalid.")
            captured = self.capture_sources(tuple(rows[:request.limit]))
            self.require_current()
            self.require_marker(bus.private_marker_unlocked())

            def decoded():
                for original in captured:
                    if not any(r.recipient_lookup == request.lookup for r in original.audience.recipients):
                        raise RelationViolationError("checked initial lookup differs from bus row.")
                    yield original

            return replace(witness, latest_source_seq=latest), decoded(), len(rows) > request.limit
        except (sqlite3.Error, OSError, MalformedRecord) as error:
            raise RelationViolationError("checked initial page is unavailable.") from error

    def conversation_frontier(self, lookup: str) -> int:
        """Latest original sender/recipient sequence without reconstructing a delivery."""
        self.require_current()
        rows = SqlValue.for_annotation(int).read(self.connection.execute(
            f"SELECT coalesce(max(seq), 0) FROM ("
            f"SELECT max(seq) AS seq FROM {DeliverySources.declared_name} "
            "WHERE sender_lookup=? UNION ALL "
            f"SELECT max(seq) AS seq FROM {Addressed.declared_name} WHERE lookup=?"
            ")",
            (lookup, lookup),
        ))
        return rows[0]

    def conversation_sources(
        self,
        lookup: str,
        predicate: str,
        parameters: tuple,
        *,
        limit: int,
        ascending: bool,
        sequences: tuple[int, ...] = (),
    ):
        """Read a limited conversation page from the existing checked index.

        Use the sender and recipients recorded when each message was sent, not
        today's registrations. Copy the selected bytes under the bus lock and
        decode that copy. Reading cannot install or repair the index, deliver
        messages, or advance a native session's input position.
        """
        self.require_current()
        if sequences:
            predicate = f"({predicate}) AND w.seq IN ({','.join('?' for _ in sequences)})"
            parameters = (*parameters, *sequences)
        db = self.connection
        rows = DeliverySources.read(
            db.execute(
                f"SELECT w.* FROM {DeliverySources.declared_name} w JOIN ("
                f"SELECT seq FROM (SELECT w.seq FROM {DeliverySources.declared_name} w "
                f"WHERE w.sender_lookup=? AND ({predicate}) "
                f"ORDER BY w.seq {'ASC' if ascending else 'DESC'} LIMIT ?) UNION "
                f"SELECT seq FROM (SELECT a.seq FROM {Addressed.declared_name} a "
                f"JOIN {DeliverySources.declared_name} w ON w.seq=a.seq "
                f"WHERE a.lookup=? AND ({predicate}) "
                f"ORDER BY a.seq {'ASC' if ascending else 'DESC'} LIMIT ?)"
                ") membership ON membership.seq=w.seq "
                f"ORDER BY w.seq {'ASC' if ascending else 'DESC'} LIMIT ?",
                (lookup, *parameters, limit, lookup, *parameters, limit, limit),
            )
        )
        captured = self.capture_sources(tuple(rows))

        def decoded():
            for original in captured:
                if original.audience.sender_lookup != lookup and not any(
                    recipient.recipient_lookup == lookup for recipient in original.audience.recipients
                ):
                    raise RelationViolationError("Conversation index differs from frozen membership.")
                yield original

        return decoded()

    def retained_task_facts(self, incarnation: ThreadIncarnation) -> Iterator[ExactTaskFact]:
        """Capture this owner's originals; their declarations own applicability.

        SQLite's negative LIMIT requests the complete sender/addressed relation,
        rather than a display page. The existing pointer reader captures bytes
        now; its decoder may run after releasing the bus locks. No unrelated
        record is decoded and no fact is stored in the checkpoint.
        """
        lookup = incarnation.lookup
        originals = self.conversation_sources(
            lookup, "1", (), limit=-1, ascending=True,
        )
        return (
            fact
            for original in originals
            for message in original.compaction_messages_for(lookup)
            for fact in message.retained_task_facts()
        )

    def context_entry_rows(self, references):
        """Look up only the candidate entry identities for this turn."""
        requested = tuple(dict.fromkeys(references))
        rows = []
        for start in range(0, len(requested), 200):
            batch = requested[start:start + 200]
            where = "(session_id,session_file,entry_id) IN (" + ",".join(
                "(?,?,?)" for _ in batch
            ) + ")"
            parameters = tuple(
                value
                for reference in batch
                for value in (
                    reference.identity.session_id,
                    reference.identity.session_file,
                    reference.entry_id,
                )
            )
            rows.extend(ContextEntrySources.select(
                self.connection, where=where, parameters=parameters
            ))
        return tuple(rows)

    def context_entry_references(self, references):
        """Find which candidate context entries are already in the checked index."""
        self.require_current()
        return frozenset(row.reference for row in self.context_entry_rows(references))

    def context_records(self, manifest, previous=None):
        """Choose context entries not yet stored and a manifest for this turn."""
        from .wire_record import (
            AuthoredObservationWireRecord,
            ContextEntryManifestWireObservation,
            RecordedContextManifestWireObservation,
        )

        if previous is not None and previous.source != self.witness.source_identity:
            raise RelationViolationError("Context predecessor belongs to another source")
        recorded, entries = RecordedContextManifest.capture(manifest, previous)
        known = self.context_entry_references(
            tuple(entry.reference for entry in entries)
        )
        new_entries = tuple(entry for entry in entries if entry.reference not in known)
        records = (
            (AuthoredObservationWireRecord(ContextEntryManifestWireObservation(new_entries)),)
            if new_entries else ()
        )
        return (*records, AuthoredObservationWireRecord(RecordedContextManifestWireObservation(recorded)))

    def keyed_receipt(self, intent: PublicationIntents) -> Message | None:
        """The sealed key index filters absence; an original row proves presence."""
        self.require_current()
        if ResponseKeys.one(self.connection, key=intent.publication_key) is None:
            return None
        # The original intent already owns its Message-derived identity. Use
        # that exact pointer relation; never decode unrelated history while
        # the publication transaction holds its current source fence. Message
        # IDs are not assumed unique: the original acknowledgement still proves key,
        # execution and complete publication content for every candidate.
        cursor = self.connection.execute(
            f"SELECT * FROM {DeliverySources.declared_name} WHERE message_id=? ORDER BY seq DESC",
            (intent.expected_message_id,),
        )
        for row in DeliverySources.iterate(cursor):
            original = row.delivery(self.stream, self.witness.root_id)
            acknowledgement = original.acknowledgement
            if acknowledgement is not None and acknowledgement.publication_key == intent.publication_key:
                if acknowledgement.execution_id != intent.execution_id:
                    raise RelationViolationError("Response publication identity conflicts.")
                if not intent.matches_publication(original.message):
                    raise RelationViolationError("Response publication intent conflicts.")
                self.require_current()
                return original.message
        raise RelationViolationError("checked response key has no original source row.")


class CheckpointTable:
    """Source-derived rows bound by the canonical writer's durable seal."""


@dataclass(frozen=True)
class ResponseKeys(CheckpointTable, TypedTable):
    key: str = field(metadata={"sql": Column(primary_key=True)})


@dataclass(frozen=True)
class ClaimCommits(CheckpointTable, TypedTable):
    """Original claim facts, including operation IDs consumed before release.

    Generated columns belong to the typed transition, not another authored
    encoding. One authorized envelope may acquire several normalized resources.
    """

    transition: ClaimTransition
    seq: int | None = field(default=None, init=False, compare=False, metadata={
        "sql": Column(unique=True, generated="json_extract(transition,'$.seq')"),
    })
    operation_id: str | None = field(default=None, init=False, compare=False, metadata={
        "sql": Column(unique=True, generated=f"json_extract(transition,'$.{FieldCodec.record_key(ClaimOwner, 'claim_scope')}.operation_id')"),
    })

    @classmethod
    def latest_sequence(cls, db: sqlite3.Connection) -> int:
        original = cls.one(db, where=f'"seq"=(SELECT MAX("seq") FROM "{cls.declared_name}")')
        return original.transition.seq if original is not None else 0

    @classmethod
    def operation_used(cls, db: sqlite3.Connection, operation_id: str) -> bool:
        return cls.one(db, operation_id=operation_id) is not None

    @classmethod
    def prepare(cls, db: sqlite3.Connection, transition: ClaimTransition) -> ClaimProjection:
        projected = transition.apply(ResourceClaims.claims_for(db, transition.resources))
        transition.require_new_operation(lambda operation: cls.operation_used(db, operation))
        return projected

    def insert(self, db: sqlite3.Connection) -> sqlite3.Cursor:
        """Apply this original in the caller's existing prefix transaction."""
        try:
            projected = self.prepare(db, self.transition)
        except (ClaimTransitionError, IdentityConflict) as error:
            raise RelationViolationError("Committed claim transition is inconsistent.") from error
        inserted = super().insert(db)
        marks = ",".join("?" for _ in self.transition.resources)
        ResourceClaims.delete(db, where=f'"resource" IN ({marks})',
                              parameters=self.transition.resources)
        for owner in projected.values():
            ResourceClaims(owner.resource, owner.seq).insert(db)
        return inserted


@dataclass(frozen=True)
class ResourceClaims(CheckpointTable, TypedTable):
    """Current membership references its original typed acquisition, never a copy."""

    resource: str = field(metadata={"sql": Column(primary_key=True)})
    claim_seq: int
    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(("claim_seq",), ClaimCommits, ("seq",),
                           deferred=True, on_delete="CASCADE"),)

    @classmethod
    def owner_for(cls, db: sqlite3.Connection, resource: str) -> ClaimOwner | None:
        original = ClaimCommits.one(
            db, where=f'"seq"=(SELECT "claim_seq" FROM "{cls.declared_name}" WHERE "resource"=?)',
            parameters=(resource,),
        )
        return original.transition.claimed(resource) if original is not None else None

    @classmethod
    def all_claims(cls, db: sqlite3.Connection) -> ClaimProjection:
        return cls._project(db, cls.select(db))

    @classmethod
    def claims_for(cls, db: sqlite3.Connection, resources: tuple[str, ...]) -> ClaimProjection:
        marks = ",".join("?" for _ in resources)
        current = cls.select(db, where=f'"resource" IN ({marks})', parameters=resources)
        return cls._project(db, current)

    @classmethod
    def _project(cls, db: sqlite3.Connection, current: list[ResourceClaims]) -> ClaimProjection:
        owners = {}
        if current:
            sequences = tuple(sorted({row.claim_seq for row in current}))
            marks = ",".join("?" for _ in sequences)
            originals = ClaimCommits.select(db, where=f'"seq" IN ({marks})', parameters=sequences)
            transitions = {row.transition.seq: row.transition for row in originals}
            owners = {row.resource: transitions[row.claim_seq].claimed(row.resource) for row in current}
        return ClaimProjection(ClaimCommits.latest_sequence(db), owners)


@dataclass(frozen=True, kw_only=True)
class WireSourcePointer(Generic[OriginalSource], ABC):
    """Locate and read a stored row by its byte offset and length."""

    offset: int = field(metadata={"sql": Column(check="offset>=0")})
    length: int = field(metadata={"sql": Column(check="length>0")})

    @abstractmethod
    def decode_bytes(self, raw: bytes, root_id: str) -> OriginalSource: ...

    def read_bytes(self, stream) -> bytes:
        """Read this row from the open bus file and check its length and newline."""
        try:
            stream.seek(self.offset)
            raw = stream.read(self.length)
        except OSError as error:
            raise RelationViolationError("checked source bytes are unavailable.") from error
        if len(raw) != self.length or not raw.endswith(b"\n"):
            raise RelationViolationError("checked source row changed.")
        return raw


@dataclass(frozen=True)
class PublicMessages(CheckpointTable, TypedTable):
    """Every public identity and its preceding committed message sequence.

    MessageWireRecord supplies this relation for retained and delivered rows.
    Silent observations do not allocate sequences. The partial index contains
    only gaps, so checking continuity never walks an intact message history.
    """

    seq: int = field(metadata={"sql": Column(primary_key=True, check="seq>0")})
    previous_seq: int = field(metadata={"sql": Column(check="previous_seq>=0 AND previous_seq<seq")})
    sender: str
    message_id: str
    gap: ClassVar[str] = "seq != previous_seq + 1"
    indexes = (Index(("sender", "message_id")), Index(("seq",), where=gap))

    @classmethod
    def require_contiguous(cls, db: sqlite3.Connection) -> None:
        if cls.exists(db, where=cls.gap):
            raise HumanClaimBlockedError(
                "Private bus sequence gap has UNKNOWN outcome; "
                "human send blocked, do not retry."
            )

    @classmethod
    def require_unused(cls, db: sqlite3.Connection, sender: str, message_id: str) -> None:
        if cls.exists(db, where="sender=? AND message_id=?", parameters=(sender, message_id)):
            raise RelationViolationError(
                "Human initial ID already exists; inspect its acknowledgement, do not retry."
            )


@dataclass(frozen=True)
class DeliverySources(WireSourcePointer["CommittedDelivery"], CheckpointTable, TypedTable):
    seq: int = field(metadata={"sql": Column(primary_key=True, check="seq>0")})
    message_id: str
    sender_lookup: str
    indexes = (Index(("sender_lookup", "seq")),)

    def delivery(self, stream, root_id: str) -> CommittedDelivery:
        """A pointer has no message authority: resolve its original frozen row."""
        return self.decode_bytes(self.read_bytes(stream), root_id)

    def decode_bytes(self, raw: bytes, root_id: str) -> CommittedDelivery:
        """One frozen seq/id/audience relation for locked and snapshot readers."""
        from .bus_publication import CommittedDelivery

        try:
            record = FieldCodec.parse(raw)
            if not isinstance(record, Mapping):
                raise MalformedRecord("checked initial JSON must be an object.")
            original = CommittedDelivery.recorded(record, root_id)
        except MalformedRecord as error:
            raise RelationViolationError("checked initial row is invalid.") from error
        if (
            original.message.reference != self.reference
            or original.audience.sender_lookup != self.sender_lookup
        ):
            raise RelationViolationError("checked source differs from its frozen wire row.")
        return original

    @property
    def reference(self):
        from .message_reference import MessageReference

        return MessageReference(self.seq, self.message_id)


@dataclass(frozen=True)
class ContextEntrySources(WireSourcePointer[ContextEntryManifest], CheckpointTable, TypedTable):
    session_id: str = field(metadata={"sql": Column(primary_key=True)})
    session_file: str = field(metadata={"sql": Column(primary_key=True)})
    entry_id: str = field(metadata={"sql": Column(primary_key=True)})

    @classmethod
    def for_reference(cls, reference, *, offset: int, length: int):
        return cls(
            reference.identity.session_id,
            reference.identity.session_file,
            reference.entry_id,
            offset=offset,
            length=length,
        )

    @property
    def reference(self):
        from .native_session_reopen import NativeSessionIdentity
        from .turn_context import ContextEntryReference

        return ContextEntryReference(
            NativeSessionIdentity(self.session_id, self.session_file), self.entry_id
        )

    def decode_bytes(self, raw: bytes, root_id: str):
        from .wire_record import WireRecord

        record = WireRecord.from_wire(json.loads(raw), root_id)
        entries = record.context_entries()
        matches = tuple(entry for entry in entries if entry.reference == self.reference)
        if len(matches) != 1:
            raise RelationViolationError("Context entry pointer differs from original observation")
        return matches[0]


@dataclass(frozen=True)
class ContextManifestCursor(ABC):
    """Locate a context manifest in one bus; subclasses choose the read direction."""

    source: PrefixSource
    offset: int
    @property
    @abstractmethod
    def comparison(self) -> str: ...

    @property
    @abstractmethod
    def descending(self) -> bool: ...

    def available(self, connection, owners):
        exists = RecordedContextManifestSources.exists(connection,
            where='thread IN (' + ','.join('?' for _ in owners) + ') '
            f'AND offset {self.comparison} ?', parameters=(*owners, self.offset))
        return self if exists else None


class BeforeContextManifest(ContextManifestCursor):
    comparison = "<"
    descending = True


class AfterContextManifest(ContextManifestCursor):
    comparison = ">"
    descending = False


@dataclass(frozen=True)
class ContextManifestReference:
    """An exact recorded request, not a turn-name search over all history."""

    source: PrefixSource
    offset: int

    def read(self, log, name, registry):
        page = RecordedContextManifestSources.for_owner(log, name, registry,
            cursor=BeforeContextManifest(self.source, self.offset + 1), limit=1)
        if len(page.resources) != 1 or page.resources[0].pointer.offset != self.offset:
            raise MalformedRecord("Recorded context reference is absent from this owner")
        return page.resources[0].value


@dataclass(frozen=True)
class ContextManifestPage:
    """One page of original resources and source-bound cuts in either direction."""

    resources: tuple[CapturedWireSource[RecordedContextManifest, ContextManifest], ...]
    older: BeforeContextManifest | None
    newer: AfterContextManifest | None

    def has_older(self) -> bool:
        return self.older is not None

    def has_newer(self) -> bool:
        return self.newer is not None

    def reference(self, manifest: ContextManifest) -> ContextManifestReference:
        for resource in self.resources:
            if resource.value is manifest:
                return ContextManifestReference(resource.source, resource.pointer.offset)
        raise ValueError("Recorded context is outside the selected page")


@dataclass(frozen=True)
class RecordedContextManifestSources(
    WireSourcePointer[RecordedContextManifest], CheckpointTable, TypedTable
):
    thread: ThreadIncarnation
    indexes = (Index(("thread", "offset")), Index(("offset",), unique=True))
    max_page_rows: ClassVar[int] = 8
    max_page_bytes: ClassVar[int] = 512 * 1024

    @classmethod
    def latest(
        cls, log: WireLog, thread: ThreadIncarnation,
    ) -> CapturedWireSource[RecordedContextManifest, ContextManifest] | None:
        with log.checked_read() as source:
            prefix = source.witness
            rows = cls.select(source.connection, thread=thread, order_by=("offset",),
                              descending=True, limit=1)
            captured = tuple((row, row.read_bytes(source.stream)) for row in rows)
        resolved = cls.resolve(log, captured, prefix)
        return next(iter(resolved.values()), None)

    @classmethod
    def resolve(
        cls, log: WireLog, captured: tuple[tuple[RecordedContextManifestSources, bytes], ...],
        prefix: PrefixWitness,
    ) -> dict[RecordedContextManifestSources, CapturedWireSource[
        RecordedContextManifest, ContextManifest]]:
        """Resolve only referenced predecessors and entries, decoding outside locks."""
        recorded = {row.offset: row.decode_bytes(value, prefix.root_id)
                    for row, value in captured}
        dependencies = {(predecessor, position)
                        for row, _ in captured
                        for position, segment in enumerate(recorded[row.offset].segments)
                        for predecessor in segment.predecessors(row.offset)}
        references = {offset for offset, _ in dependencies} - recorded.keys()
        if references:
            with log.checked_read() as source:
                source.witness.require_read_window(prefix)
                captured_prior = tuple((row, row.read_bytes(source.stream))
                                       for offset in references
                                       for row in cls.select(source.connection, offset=offset))
            if {row.offset for row, _ in captured_prior} != references:
                raise RelationViolationError("Recorded context predecessors are incomplete")
            for row, value in captured_prior:
                if row.offset + row.length > prefix.offset:
                    raise RelationViolationError("Context predecessor exceeds its captured prefix")
                recorded[row.offset] = row.decode_bytes(value, prefix.root_id)
        segments = tuple(segment for row, _ in captured for segment in recorded[row.offset].segments)
        segments += tuple(recorded[offset].segments[position] for offset, position in dependencies)
        references = {reference for segment in segments for reference in segment.entries}
        entries: dict[ContextEntryReference, SegmentManifest] = {}
        if references:
            with log.checked_read() as source:
                source.witness.require_read_window(prefix)
                entry_rows = source.context_entry_rows(references)
                if any(row.offset + row.length > prefix.offset for row in entry_rows):
                    raise RelationViolationError("Context entry exceeds its captured prefix")
                geometries = {(row.offset, row.length): row for row in entry_rows}
                entry_bytes = tuple(row.read_bytes(source.stream) for row in geometries.values())
            for value in entry_bytes:
                record = WireRecord.from_wire(json.loads(value), prefix.root_id)
                for entry in record.context_entries():
                    if entry.reference in references:
                        entries[entry.reference] = entry.manifest
            if entries.keys() != references:
                raise RelationViolationError("Recorded context entries are incomplete")
        for row, _ in captured:
            manifest = recorded[row.offset]
            if any(recorded[predecessor].thread != manifest.thread
                   for segment in manifest.segments
                   for predecessor in segment.predecessors(row.offset)):
                raise RelationViolationError("Context predecessor belongs to another owner")
        return {row: CapturedWireSource(prefix.source_identity, row, value,
                                       recorded[row.offset].restore(entries, recorded))
                for row, value in captured}

    @classmethod
    def for_owner(
        cls, log: WireLog, name: str, registry: Registration, *,
        cursor: ContextManifestCursor | None = None,
        previous: tuple[CapturedWireSource[RecordedContextManifest, ContextManifest], ...] = (),
        limit: int | None = None,
    ) -> ContextManifestPage:
        """Acquire one bounded page and its entries; decode outside both locks.

        The budget bounds turn rows. Referenced entry rows are indivisible
        dependencies of those requests, never a reason to scan other turns.
        Entry references come from the captured turn records. The second read
        must contain that same prefix, and every entry must belong to it. Later
        appends therefore cannot change this observation or fill a missing entry.
        """
        from .store_files import _store_lock

        limit = cls.max_page_rows if limit is None else limit
        if not 1 <= limit <= cls.max_page_rows:
            raise ValueError("Context page row limit exceeds its source budget")
        with _store_lock(log.path.parent / "wire", mode=SharedLock()), log.checked_read() as source:
            snapshot = registry.snapshot()
            incarnation = snapshot.require(name).incarnation
            storage = SqlStorage.for_type(ThreadIncarnation)
            owners = tuple(storage.to_sql(FieldCodec.encode(recorded))
                           for recorded in incarnation.recorded_names(snapshot))
            where = "thread IN (" + ",".join("?" for _ in owners) + ")"
            parameters = owners
            prefix = source.witness
            if cursor is not None:
                if cursor.source != prefix.source_identity:
                    raise ValueError("Recorded context page changed its source")
                where += f" AND offset {cursor.comparison} ?"
                parameters += (cursor.offset,)
            descending = cursor is None or cursor.descending
            candidates = cls.iterate(source.connection.execute(
                f'SELECT * FROM "{cls.declared_name}" WHERE {where} '
                f'ORDER BY offset {"DESC" if descending else "ASC"} LIMIT ?',
                (*parameters, limit),
            ))
            rows, size = [], 0
            for row in candidates:
                # A complete first request always makes progress, even when
                # its indivisible record exceeds the ordinary page budget.
                if rows and size + row.length > cls.max_page_bytes:
                    break
                rows.append(row)
                size += row.length
            candidates.close()
            rows = tuple(sorted(rows, key=lambda row: row.offset))
            older = newer = None
            if rows:
                older = BeforeContextManifest(prefix.source_identity, rows[0].offset).available(
                    source.connection, owners)
                newer = AfterContextManifest(prefix.source_identity, rows[-1].offset).available(
                    source.connection, owners)
            # The saved check covers the bus bytes read so far; moving an archive
            # replaces the file. Rows from the same unchanged file can be reused,
            # so a refresh needs to read only newly recorded turns.
            retained = {item.pointer: item for item in previous
                        if item.source == prefix.source_identity}
            captured = tuple((row, row.read_bytes(source.stream)) for row in rows
                             if row not in retained)

        resources = {row: retained[row] for row in rows if row in retained}
        changed = cls.resolve(log, captured, prefix)
        for row, resource in changed.items():
            if resource.value.thread.resolved(snapshot) != incarnation:
                raise RelationViolationError(
                    "Context source differs from recorded owner.")
            resources[row] = resource
        return ContextManifestPage(tuple(resources[row] for row in rows), older, newer)

    def decode_bytes(self, raw: bytes, root_id: str):
        from .wire_record import WireRecord

        record = WireRecord.from_wire(json.loads(raw), root_id)
        manifests = record.recorded_context_manifests()
        if len(manifests) != 1 or manifests[0].thread != self.thread:
            raise RelationViolationError("Recorded context pointer differs from original observation")
        return manifests[0]


@dataclass(frozen=True)
class Addressed(CheckpointTable, TypedTable):
    lookup: str = field(metadata={"sql": Column(primary_key=True)})
    seq: int = field(metadata={"sql": Column(True, references=(DeliverySources, "seq"))})

    @classmethod
    def observed_after(
        cls, bus: WireLog, revision: FileRevision, source: FileRevision,
        marker_revision: FileRevision, lookup: str, after_seq: int,
    ) -> tuple[WireMetadata, bool]:
        """Read one recipient's indexed aggregate only at the captured revision.

        This observation grants no delivery authority. Concurrent publication
        makes it stale instead of making an absent row authoritative.
        """
        path = _path(bus.path)

        def require_captured_sources() -> None:
            if (
                FileRevision.capture(bus.path) != source
                or FileRevision.capture(bus.metadata_path) != marker_revision
                or FileRevision.capture(path) != revision
            ):
                raise RelationViolationError("Address observation changed during its read")

        require_captured_sources()
        marker = bus.private_marker_unlocked()
        with closing(_connect(path, readonly=True)) as db:
            witness = CheckedPrefix.read_witness(db)
            witness.require_marker(marker)
            if witness.revision != source:
                raise RelationViolationError("Address index does not cover the observed wire")
            marker.seal.check_final(witness, path)
            found = cls.exists(db, where="lookup=? AND seq>?", parameters=(lookup, after_seq))
        require_captured_sources()
        return marker, found


@dataclass(frozen=True)
class Replies(CheckpointTable, TypedTable):
    """A committed delivery that names an earlier message as the one it answers."""

    answered_seq: int = field(metadata={"sql": Column(primary_key=True)})
    seq: int = field(metadata={"sql": Column(True, references=(DeliverySources, "seq"))})


@dataclass(frozen=True)
class CheckedPrefix(CheckpointTable, TypedTable, declared_name="prefix_certificate"):
    """The existing SQLite row, decoded once at its persistence boundary."""

    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True)})
    version: Literal[2]
    root_id: str
    device: int
    inode: int
    offset: int
    through_seq: int
    digest: str
    tail: str
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def _install(cls, bus: WireLog, marker: WireMetadata) -> PrefixWitness:
        """Build and publish one complete index under the caller's bus lock.

        Fresh installation and verified carry share this algorithm. Retaining
        an old required seal throughout carry makes an interrupted replacement
        unreadable instead of allowing an unsealed full-scan fallback.
        """
        path = _path(bus.path)
        _directory_sync(bus.path)
        with tempfile.TemporaryDirectory(prefix=".checkpoint-install-", dir=path.parent) as stage:
            staged = Path(stage) / path.name
            fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            with closing(_connect(staged)) as db:
                for table in TypedTable.members_with(CheckpointTable):
                    table.create(db)
                with bus.path.open("rb") as stream:
                    info = os.fstat(stream.fileno())
                    digest = _SEED
                    through_seq = 0

                    def collect(offset, raw, record):
                        nonlocal digest, through_seq
                        digest = _chain(digest, raw)
                        through_seq = _index_row(db, offset, raw, record, through_seq,
                                                 marker.claimed_after_seq)

                    with db:
                        for _ in bus.verified_records_unlocked(marker, on_row=collect):
                            pass
                        if through_seq != marker.last_seq:
                            raise RelationViolationError("Private bus has an unsettled publication sequence.")
                        if (
                            FileRevision.from_stat(os.fstat(stream.fileno()))
                            != FileRevision.from_stat(info)
                            or FileRevision.from_stat(bus.path.stat())
                            != FileRevision.from_stat(info)
                        ):
                            raise RelationViolationError("Private bus changed during checkpoint installation.")
                        cls.capture(
                            marker.root_id, FileRevision.from_stat(info), through_seq,
                            digest, _tail(stream, info.st_size)).insert(db)
            _directory_sync(staged)
            with closing(_connect(staged, readonly=True)) as db:
                witness = _saved(db)
            os.replace(staged, path)
            _directory_sync(path)
            published = replace(marker)
            published.seal_with(FinalSeal.capture(witness, path))
            bus.write_metadata_unlocked(published)
            return witness

    @classmethod
    def read_witness(cls, db: sqlite3.Connection) -> PrefixWitness:
        """Check the saved SQL schema and decode the record of checked bus bytes.

        Only the writer changes which rows are indexed. A read-only archive
        keeps the same saved check and the index file it names.
        """
        actual = SQLiteSchemaObject.read(db.execute(
            "SELECT name,sql FROM sqlite_master WHERE name=?", (FieldCodec.encode(cls),),
        ))
        if SQLiteSchemaObject.differences(actual, cls.schema_objects()):
            raise RelationViolationError("Private bus checked prefix record schema is unavailable.")
        try:
            row = cls.one(db, singleton=1)
            if row is None:
                raise RelationViolationError("Private bus checkpoint prefix record is missing.")
            return row.witness()
        except MalformedRecord as error:
            raise RelationViolationError("Private bus checkpoint identity is malformed.") from error

    def witness(self) -> PrefixWitness:
        with MalformedRecord.refusing("Invalid checked prefix record"):
            return PrefixWitness(
                self.root_id,
                FileRevision(
                    FileIdentity(self.device, self.inode),
                    self.offset,
                    self.mtime_ns,
                    self.ctime_ns,
                ),
                self.through_seq,
                self.digest,
                self.tail,
            )

    @classmethod
    def capture(
        cls, root_id: str, revision: FileRevision, seq: int, digest: bytes, tail: str
    ) -> CheckedPrefix:
        return cls.from_witness(PrefixWitness(root_id, revision, seq, digest.hex(), tail))

    @classmethod
    def from_witness(cls, witness: PrefixWitness) -> CheckedPrefix:
        revision = witness.revision
        return cls(1, 2, witness.root_id, revision.identity.device, revision.identity.inode,
                   witness.offset, witness.through_seq, witness.digest, witness.tail,
                   revision.mtime_ns, revision.ctime_ns)

    @classmethod
    def commit_tail(cls, bus: WireLog, marker: WireMetadata, db: sqlite3.Connection,
                    prior: PrefixWitness, expected: PrefixWitness,
                    additions: list[tuple[int, bytes, WireRecord]]) -> PrefixWitness:
        """Commit one chunk through the same intent as an ordinary append."""
        path = _path(bus.path)
        marker.seal.require_rebuilt(prior, expected)
        marker.seal_with(PendingSeal.capture(prior, expected, path))
        bus.write_metadata_unlocked(marker)
        with db:
            sequence = prior.through_seq
            for offset, raw, record in additions:
                sequence = _index_row(db, offset, raw, record, sequence,
                                      marker.claimed_after_seq)
            if sequence != expected.through_seq:
                raise RelationViolationError("Indexed tail differs from its sealed sequence.")
            cls.from_witness(expected).upsert(db)
        _directory_sync(path)
        marker.seal_with(FinalSeal.capture(expected, path))
        bus.write_metadata_unlocked(marker)
        return expected


def _path(bus_path: Path) -> Path:
    return bus_path.with_name("private_bus_checkpoint.sqlite3")


def _tail(stream, offset: int) -> str:
    stream.seek(max(0, offset - _TAIL_BYTES))
    return hashlib.sha256(stream.read(min(_TAIL_BYTES, offset))).hexdigest()


def _chain(previous: bytes, raw: bytes) -> bytes:
    return hashlib.sha256(previous + len(raw).to_bytes(8, "big") + raw).digest()


def _directory_sync(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if path.is_symlink() or not path.exists():
        raise RelationViolationError("Private bus checkpoint is missing or redirected.")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise RelationViolationError("Private bus checkpoint is not owner-only.")
    if readonly:
        db = CoordinationStore.read_snapshot(path)
    else:
        uri = f"{path.absolute().as_uri()}?mode=rw"
        db = CoordinationStore.writer(uri, uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
    return db


def _saved(db: sqlite3.Connection) -> PrefixWitness:
    schema = {
        name: sql
        for table in TypedTable.members_with(CheckpointTable)
        for name, sql in table.schema_objects().items()
    }
    actual = SQLiteSchemaObject.read(
        db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
    )
    if SQLiteSchemaObject.differences(actual, schema):
        raise RelationViolationError("Private bus checkpoint schema is unavailable.")
    return CheckedPrefix.read_witness(db)


def _index_row(db: sqlite3.Connection, offset: int, raw: bytes,
               record: WireRecord, previous_sequence: int, claimed_after_seq: int) -> int:
    sequence = record.sequence_after(previous_sequence)
    for row in record.checkpoint_rows(offset, len(raw), previous_sequence, claimed_after_seq):
        row.insert(db)
    return sequence


def install_private_bus_checkpoint(bus: WireLog, *, _bus_locked: bool = False) -> PrefixWitness:
    """Check the whole private bus and install its index without rewriting messages.

    Hold the writer lock to prevent appends during the scan. Build the index
    in a separate file, then publish it and save the bus marker naming that file.
    Failure before publication leaves no active index. If saving the marker
    has an uncertain outcome after publication, keep the index file but refuse
    reads until the marker and index have been checked together.
    """
    if bus.path.name != "bus.jsonl":
        raise ValueError("Canonical private WireLog required")
    with nullcontext() if _bus_locked else bus.locked():
        marker = bus.private_marker_unlocked()
        if not marker.claims:
            raise RelationViolationError(
                "Checkpoint installation needs a claim-enabled private root."
            )
        path = _path(bus.path)
        if path.exists() or path.is_symlink() or marker.checkpoint_seal is not None:
            raise RelationViolationError("Private bus checkpoint is already installed.")
        if not bus.path.exists():
            if marker.last_seq != 0:
                raise RelationViolationError("Private bus is missing its reserved publication.")
            fd = os.open(
                bus.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return CheckedPrefix._install(bus, marker)


def checked_prefix_exists(bus_path: Path) -> bool:
    path = _path(bus_path)
    return path.exists() or path.is_symlink()


def _check_final_checkpoint_read_only(bus, marker, db, stream, path) -> PrefixWitness:
    """Check a final, up-to-date checkpoint under the shared bus lock; never writes."""
    saved = _saved(db)
    info = os.fstat(stream.fileno())
    saved.require_source(marker, info, stream)
    if FileRevision.from_stat(info) != saved.revision:
        raise CheckpointNeedsRepairError("Bus revision requires exclusive checkpoint validation.")
    marker.seal.require_final(saved, path)
    return saved


@dataclass(frozen=True)
class WritableCheckpoint:
    """A writable bus's checkpoint; the lock mode chooses how its holder reads it."""

    path: Path

    def open_read_only(self) -> sqlite3.Connection:
        return _connect(self.path, readonly=True)

    def open_for_repair(self) -> sqlite3.Connection:
        return _connect(self.path)

    def check_final(self, bus: WireLog, marker: WireMetadata, db: sqlite3.Connection,
                    stream: BinaryIO) -> PrefixWitness:
        return _check_final_checkpoint_read_only(bus, marker, db, stream, self.path)

    def verify_with_repair(self, bus: WireLog, marker: WireMetadata, db: sqlite3.Connection,
                           stream: BinaryIO) -> PrefixWitness:
        return _verify_open_checkpoint_unlocked(bus, marker, db, stream, self.path)


def _verify_open_checkpoint_unlocked(bus, marker, db, stream, path) -> PrefixWitness:
    """Flush the open bus and check its index against the saved marker."""
    # Exclusive recovery may adopt bytes left visible by an interrupted append.
    # Flush those bytes before publishing a repaired index check.
    os.fsync(stream.fileno())
    _directory_sync(bus.path)
    saved = _saved(db)
    info = os.fstat(stream.fileno())
    saved.require_source(marker, info, stream)
    return marker.seal.recover(bus, marker, db, path, saved, info)


@contextmanager
def opened_private_checkpoint_unlocked(bus: WireLog, marker: WireMetadata, *,
                                       mode: LockMode = ExclusiveLock()):
    """Keep only this canonical lock's verified source/index resources open.

    Under the shared bus lock other readers run concurrently, so the check is
    read-only and raises CheckpointNeedsRepairError where an exclusive holder would
    repair the checkpoint.
    """
    path = _path(bus.path)
    with ExitStack() as resources:
        try:
            db = resources.enter_context(closing(marker.access.open_checkpoint(path, mode=mode)))
            stream = resources.enter_context(bus.path.open("rb"))
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RelationViolationError("Claim bus is not a regular file.")
            saved = marker.access.verify_checkpoint(bus, marker, db, stream, path, mode=mode)
            db.execute("PRAGMA query_only=ON")
        except (sqlite3.Error, OSError) as error:
            raise RelationViolationError(
                "Private bus checkpoint verification is unavailable; durability is UNKNOWN."
            ) from error
        yield CheckedRead(bus.path, marker, db, stream, saved)


def verify_private_bus_checkpoint_unlocked(bus: WireLog, marker: WireMetadata) -> PrefixWitness:
    """Verify a standalone native/delivery/publication source witness."""
    with opened_private_checkpoint_unlocked(bus, marker) as source:
        return source.witness


def append_private_bus_checkpoint_unlocked(
    bus: WireLog,
    marker: WireMetadata,
    raw: bytes,
    record: WireRecord,
) -> PrefixWitness:
    """Index one appended row only after flushing the bus and its parent directory."""
    path = _path(bus.path)
    # WireLog owns UNKNOWN classification: it performed the bus fsync and is
    # this operation's sole production caller. Preserve the actual failure here.
    with closing(_connect(path)) as db, bus.path.open("rb") as stream:
        saved = _saved(db)
        info = os.fstat(stream.fileno())
        offset = info.st_size - len(raw)
        sequence = record.sequence_after(saved.through_seq)
        saved.require_append(marker, info, offset, sequence)
        marker.seal.check_final(saved, path)
        stream.seek(offset)
        if stream.read(len(raw)) != raw or _tail(stream, offset) != saved.tail:
            raise RelationViolationError("Private bus checkpoint append bytes differ.")
        digest = _chain(bytes.fromhex(saved.digest), raw)
        expected = PrefixWitness(
            saved.root_id,
            FileRevision.from_stat(info),
            sequence,
            digest.hex(),
            _tail(stream, info.st_size),
        )
        return CheckedPrefix.commit_tail(bus, marker, db, saved, expected, [(offset, raw, record)])



def addressed_source_pointers_unlocked(
    bus: WireLog, marker: WireMetadata, lookup: str, *, limit: int = 4
) -> tuple[DeliverySources, ...]:
    """Latest source pointers for natural-turn awareness, and the observation
    tick's wake answer: an empty result under the current seal means the owner
    has no unauthorized addressed rows.

    Caller holds the bus lock. Read only the current sealed index: no payload
    decode, historical scan, index repair, recovery, or native cursor advancement.
    The returned pointers are never delivery, read or acceptance evidence; the
    seal-checked emptiness is the address fact the tick consumes.
    Frozen audience membership includes unmentioned NoWake observers.
    """
    path = _path(bus.path)
    with closing(_connect(path, readonly=True)) as db:
        saved = _saved(db)
        marker.seal.check_final(saved, path)
        if (
            saved.root_id != marker.root_id
            or saved.through_seq != marker.last_seq
            or FileRevision.from_stat(bus.path.stat()) != saved.revision
        ):
            raise RelationViolationError("Current source pointers require an unchanged checkpoint.")
        rows = DeliverySources.read(
            db.execute(
                f"SELECT {','.join('i.' + column for column in DeliverySources.columns())} FROM {Addressed.declared_name} a "
                f"JOIN {DeliverySources.declared_name} i ON i.seq=a.seq "
                "WHERE a.lookup=? AND i.seq>? ORDER BY i.seq DESC LIMIT ?",
                (lookup, marker.claimed_after_seq, limit),
            )
        )
        marker.seal.check_final(saved, path)
        if FileRevision.from_stat(bus.path.stat()) != saved.revision:
            raise RelationViolationError("Source changed during awareness read.")
        return tuple(reversed(rows))
