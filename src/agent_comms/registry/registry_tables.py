"""The registry as rows: one thread, status, alias or generation per row, so a change writes the
rows it changes and nothing else (plans/REGISTRY-ROWS-PLAN.md).

The whole-document registry rewrote every row on every change: 5.6 MB and 2,933 threads on the
live bus to change one. Each mapping section of the document is one table of (name, value) rows,
the value in its wire form, so a reader keeps decoding only the rows it reads (ThreadRows); the
header row holds the rest of the document. A commit stamps the rows it writes with its revision,
and a reader holding revision r reads only what changed after r.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Annotated, ClassVar, Self, cast

from pydantic import JsonValue

from agent_comms.coordination.errors import StaleFence
from agent_comms.errors import RelationViolationError
from agent_comms.execution.turn_registry import TurnRows
from agent_comms.nominal.field_codec import FieldCodec, RootId, ThreadLookup, WireValue
from agent_comms.registry.presence import PresenceCapture, PresenceChange, PresenceRevision
from agent_comms.registry.registry_document import RegistryDocument
from agent_comms.registry.thread_identity import TurnIdentity
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.private_path import FileIdentity, FileRevision, MissingFile
from agent_comms.storage.row_mapping import RowMapping
from agent_comms.storage.typed_table import (
    Column,
    DurableStore,
    DurableTables,
    FrozenColumn,
    Index,
    SharedFileTables,
    StoreRows,
    TableSchema,
    TypedTable,
)

type RawDocument = Mapping[str, object]
"""An in-memory encoded document; mapping sections retain immutable row roots.
RegistryCapture.wire_document materializes JSON at an explicit export boundary.
"""


@dataclass(frozen=True, slots=True)
class StoredWire(WireValue):
    """A section entry kept in its stored wire form. The document's own decoder validates it
    when something reads it (ThreadRows decodes a thread only on access), so a cold read never
    walks every row's keys a second time."""

    wire: JsonValue

    def to_wire(self) -> JsonValue:
        return self.wire

    @classmethod
    def from_wire(cls, data: JsonValue) -> Self:
        return cls(data)


class RegistryRows(StoreRows):
    """The registry store discovers its rows through this membership declaration."""


class RegistryRow(RegistryRows, TypedTable, declared_name=None):
    """A row of the registry."""


@dataclass(frozen=True)
class ChangeStamped(TypedTable, declared_name=None):
    """A row stamped with the revision that last wrote it, so a reader holding revision r reads
    only the rows with changed_at > r."""

    changed_at: Annotated[int, Column(check="changed_at > 0")]
    indexes = (Index(("changed_at",), unique=False, where=None),)


@dataclass(frozen=True)
class RevisionStamped(ChangeStamped, RegistryRow, declared_name=None):
    """A registry row stamped with the registry revision that last wrote it."""


@dataclass(frozen=True)
class RegistryNamedRow(RevisionStamped, declared_name=None):
    """One entry of a mapping section of the registry document, under its key; each family
    declares its section's place in the document and its value's wire type."""

    path: ClassVar[tuple[str, ...]]
    """The document attributes leading to this section's mapping."""
    name: Annotated[str, FrozenColumn(primary_key=True)]

    @classmethod
    @cache
    def section(cls) -> tuple[str, ...]:
        """The wire keys of ``path``, as the codec spells them."""
        keys, value = [], RegistryDocument()
        for name in cls.path:
            keys.append(FieldCodec.record_key(type(value), name))
            value = getattr(value, name)
        return tuple(keys)

    @classmethod
    def entries(cls, document: RegistryDocument) -> Mapping[str, object]:
        value: object = document
        for name in cls.path:
            value = getattr(value, name)
        return value

    @classmethod
    def cleared(cls, document: RegistryDocument) -> RegistryDocument:
        """``document`` with this section empty, so encoding it encodes none of its rows."""
        def emptied(value: object, path: tuple[str, ...]) -> object:
            head, *rest = path
            inner = getattr(value, head)
            return replace(value, **{head: emptied(inner, tuple(rest)) if rest else type(inner)()})
        return emptied(document, cls.path)

    @classmethod
    def changed_entries(cls, document: RegistryDocument, earlier: RegistryDocument) -> tuple[str, ...]:
        return RowMapping.owning(cls.entries(document)).changed_in_order(
            RowMapping.owning(cls.entries(earlier)))

    @classmethod
    def entry_wire(cls, document: RegistryDocument, name: str) -> JsonValue:
        return FieldCodec.encode(cls.entries(document)[name])

    @classmethod
    def section_wire(cls, document: RegistryDocument, earlier: RegistryDocument,
                     stored: Mapping[str, JsonValue]) -> RowMapping[str, JsonValue]:
        """Apply changed keys to the captured root; untouched rows are not enumerated."""
        mapping = RowMapping.owning(stored).copy()
        entries = cls.entries(document)
        for name in cls.changed_entries(document, earlier):
            if name in entries:
                mapping[name] = cls.entry_wire(document, name)
            else:
                del mapping[name]
        return mapping

    @classmethod
    def stored(cls, value: JsonValue) -> object:
        """The column value for this entry's wire form: a plain value is its own."""
        return value

    @property
    def wire(self) -> JsonValue:
        return self.value

    @classmethod
    def families(cls) -> tuple[type[RegistryNamedRow], ...]:
        return tuple(member for member in TypedTable.members_with(RegistryRows)
                     if issubclass(member, RegistryNamedRow))

    @classmethod
    def mapping_in(cls, raw: Mapping[str, object]) -> Mapping[str, JsonValue]:
        value: object = raw
        for key in cls.section():
            value = cast(Mapping[str, object], value)[key]
        return cast(Mapping[str, JsonValue], value)

    @classmethod
    def placed(cls, raw: RawDocument, mapping: Mapping[str, JsonValue]) -> RawDocument:
        """``raw`` with this section replaced by ``mapping``, copying only the dicts on its path."""
        return cls._placed(raw, cls.section(), mapping)

    @staticmethod
    def _placed(raw: RawDocument, path: tuple[str, ...], mapping: Mapping[str, JsonValue]) -> RawDocument:
        head, *rest = path
        return {**raw, head: RegistryNamedRow._placed(cast(RawDocument, raw[head]), tuple(rest), mapping)
                if rest else mapping}


class WireStoredRow(RegistryNamedRow, declared_name=None):
    """A row whose value is a record kept in its stored wire form (StoredWire)."""

    @classmethod
    def stored(cls, value: JsonValue) -> StoredWire:
        return StoredWire(value)

    @property
    def wire(self) -> JsonValue:
        return self.value.wire


@dataclass(frozen=True)
class RegistryHeader(RegistryRow):
    """The document without its mapping sections, and the revision that last committed it."""

    header: Annotated[int, FrozenColumn(primary_key=True, check="header = 1")]
    revision: Annotated[int, Column(check="revision > 0")]
    scalars: StoredWire


@dataclass(frozen=True)
class RegistryThreads(WireStoredRow):
    """A thread's stored declaration, under its name."""

    path = ("threads",)
    value: StoredWire

    @classmethod
    def changed_entries(cls, document: RegistryDocument, earlier: RegistryDocument) -> tuple[str, ...]:
        return document.threads.changed_in_order(earlier.threads)

    @classmethod
    def entry_wire(cls, document: RegistryDocument, name: str) -> JsonValue:
        return document.threads.wire_at(name)


@dataclass(frozen=True)
class RegistryStatuses(WireStoredRow):
    """A thread's stored status, under its name."""

    path = ("statuses",)
    value: StoredWire


@dataclass(frozen=True)
class RegistryAliases(RegistryNamedRow):
    """An alias, and the thread name it resolves to."""

    path = ("aliases",)
    value: str


@dataclass(frozen=True)
class RegistryOwnerGenerations(RegistryNamedRow):
    """A name's owner generation; deleted names keep theirs as a tombstone."""

    path = ("owners", "generations")
    value: Annotated[int, Column(check="value > 0")]


@dataclass(frozen=True)
class RegistryClaimGenerations(RegistryNamedRow):
    """A name's claim generation; deleted names keep theirs as a tombstone."""

    path = ("claims", "generations")
    value: Annotated[int, Column(check="value > 0")]


@dataclass(frozen=True)
class RegistryRemovals(RevisionStamped):
    """A row removed at a revision: incremental readers learn deletions here, since a deleted
    row can't answer ``changed_at > r`` itself. The family is the row class, not a name for it."""

    family: Annotated[type[RegistryNamedRow], FrozenColumn(primary_key=True)]
    name: Annotated[str, FrozenColumn(primary_key=True)]


@dataclass(frozen=True, slots=True)
class RegistryRevisionMark:
    """A committed registry revision: the row store's counter, never a file stat."""

    revision: int

    def successor(self) -> RegistryRevisionMark:
        return RegistryRevisionMark(self.revision + 1)


@dataclass(frozen=True, slots=True)
class RegistryRowRevision:
    """One database instance and its committed counter; copies have distinct identities."""

    identity: FileIdentity | MissingFile
    mark: RegistryRevisionMark

    @classmethod
    def empty(cls) -> Self:
        return cls(MissingFile(), RegistryRevisionMark(0))

    def extends(self, earlier: RegistryRowRevision) -> bool:
        """Only the same database at a nondecreasing counter can supply an incremental read."""
        return self.identity == earlier.identity and self.mark.revision >= earlier.mark.revision

    def accepts_write(self, earlier: RegistryRowRevision) -> bool:
        """A writer may create the empty schema it read as absent, or update that same source."""
        return self == earlier or (earlier == self.empty() and self.mark == earlier.mark)


@dataclass(frozen=True, slots=True)
class RegistryCapture:
    """One committed revision's encoded rows, sharing immutable mapping roots.

    This is an in-process capture; wire_document owns its complete JSON export.
    """

    revision: RegistryRowRevision
    raw: RawDocument

    def __post_init__(self) -> None:
        raw = self.raw
        for family in RegistryNamedRow.families():
            raw = family.placed(raw, RowMapping.owning(family.mapping_in(raw)).freeze())
        object.__setattr__(self, "raw", raw)

    def wire_document(self) -> dict[str, JsonValue]:
        """Export complete JSON only for a decoder or a caller requesting stored data."""
        raw = self.raw
        for family in RegistryNamedRow.families():
            raw = family.placed(raw, dict(family.mapping_in(raw)))
        return cast(dict[str, JsonValue], raw)

    @property
    def mark(self) -> RegistryRevisionMark:
        return self.revision.mark

    @classmethod
    def empty(cls) -> Self:
        return cls(RegistryRowRevision.empty(), cast(RawDocument, FieldCodec.encode(RegistryDocument())))

    def encoded(self, document: RegistryDocument, earlier: RegistryDocument) -> RawDocument:
        """``document``'s wire form, given that ``earlier`` was decoded from this capture: only
        what the edit replaced is encoded."""
        families = RegistryNamedRow.families()
        cleared = document
        for family in families:
            cleared = family.cleared(cleared)
        raw = cast(RawDocument, FieldCodec.encode(cleared))
        for family in families:
            raw = family.placed(raw, family.section_wire(document, earlier, family.mapping_in(self.raw)))
        return raw

    def changed(self, changes: RegistryChanges) -> RegistryCapture:
        """This revision with ``changes`` applied: removals first, then rows, so a name removed
        and declared again after this revision ends declared. Unchanged sections stay shared."""
        if changes.revision == self.revision:
            return self
        raw = cast(RawDocument, changes.header.scalars.wire)
        for family in RegistryNamedRow.families():
            removed = changes.removed(family)
            written = changes.written(family)
            mapping = family.mapping_in(self.raw)
            if removed or written:
                updated = RowMapping.owning(mapping).copy()
                for name in removed:
                    updated.pop(name, None)
                updated.update(written)
                mapping = updated
            raw = family.placed(raw, mapping)
        return RegistryCapture(changes.revision, raw)


@dataclass(frozen=True)
class RegistryChanges:
    """What changed after a revision: the header, the rows written and the rows removed."""

    revision: RegistryRowRevision
    header: RegistryHeader
    rows: Mapping[type[RegistryNamedRow], tuple[RegistryNamedRow, ...]]
    removals: tuple[RegistryRemovals, ...]

    def written(self, family: type[RegistryNamedRow]) -> dict[str, JsonValue]:
        return {row.name: row.wire for row in self.rows[family]}

    def removed(self, family: type[RegistryNamedRow]) -> frozenset[str]:
        return frozenset(removal.name for removal in self.removals if removal.family is family)


class PresenceTables(StoreRows):
    """The presence store discovers its rows through this membership declaration."""


@dataclass(frozen=True)
class PresenceRow(PresenceTables, ChangeStamped):
    """When a thread was last seen, stamped with the presence revision that wrote it.

    Being seen is not a fact about a thread's declaration: presence has its own header, so a
    heartbeat moves the presence revision and leaves the registry's alone, and an edit that
    changes both commits them in one transaction."""

    name: Annotated[str, FrozenColumn(primary_key=True)]
    seen: float


@dataclass(frozen=True)
class PresenceHeader(PresenceTables, TypedTable):
    """The presence revision that last committed."""

    header: Annotated[int, FrozenColumn(primary_key=True, check="header = 1")]
    revision: Annotated[int, Column(check="revision > 0")]


class RegistryFamily(SharedFileTables, DurableTables):
    """One table family beside the registry's rows in its file, judged apart from them and
    from each other: a change to one family's schema never refuses a reader of another, and
    a new registry file creates every member."""

    tables: ClassVar[type[StoreRows]]

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return TypedTable.members_with(cls.tables)


class PresenceSchema(RegistryFamily):
    """When each thread was last seen, under presence's own header."""

    tables = PresenceTables


class TurnTables(StoreRows):
    """The turn family's rows: the record of each leased turn generation."""


@dataclass(frozen=True)
class TurnRow(TurnTables, TypedTable):
    """One leased turn generation. The lease that names it writes it with the registry rows that
    name it; a phase change rewrites it alone; the lease that supersedes it deletes it."""

    lookup: Annotated[ThreadLookup, FrozenColumn(primary_key=True)]
    generation: Annotated[int, FrozenColumn(primary_key=True)]
    active: StoredWire

    @staticmethod
    def key(identity: TurnIdentity) -> dict[str, object]:
        return {"lookup": identity.incarnation.require_recorded().lookup,
                "generation": identity.generation}

    @classmethod
    def of(cls, identity: TurnIdentity, active: JsonValue) -> TurnRow:
        return cls(active=StoredWire(active), **TurnRow.key(identity))


class TurnSchema(RegistryFamily):
    """The record of each leased turn generation."""

    tables = TurnTables


class NamespaceTables(StoreRows):
    """The namespace family's row: which native protocol root this registry is."""


@dataclass(frozen=True)
class RegistryNamespace(NamespaceTables, TypedTable):
    """The native namespace a private root's inputs and cursors are scoped by. A public root
    has none. Written once, when the root becomes private, and never changed."""

    header: Annotated[int, FrozenColumn(primary_key=True, check="header = 1")]
    root_id: Annotated[RootId, FrozenColumn()]


class NamespaceSchema(RegistryFamily):
    """The registry's native namespace."""

    tables = NamespaceTables


@dataclass(frozen=True)
class TurnChange:
    """The turns one edit's leases begin, in their wire form, and the generations they
    supersede. A lease begun and superseded within one edit leaves no row."""

    begun: tuple[tuple[TurnIdentity, JsonValue], ...] = ()
    retired: tuple[TurnIdentity, ...] = ()

    def write_in(self, db) -> None:
        for identity, active in self.begun:
            TurnRow.of(identity, active).upsert(db)
        for identity in self.retired:
            TurnRow.delete(db, **TurnRow.key(identity))


@dataclass(frozen=True)
class EditTables:
    """What one registry edit writes beside its rows, on the transaction that commits them."""

    presence: PresenceChange
    turns: TurnChange = field(default_factory=TurnChange)

    @classmethod
    def unchanged(cls) -> Self:
        return cls(PresenceChange.unchanged())

    def write_in(self, db) -> None:
        RegistryRowStore.presence_in(db, self.presence)
        self.turns.write_in(db)


@dataclass(frozen=True, slots=True)
class RegistryRowStore(SharedFileTables, DurableStore, RegistryFile, TurnRows):
    """The registry's rows: one owner-only SQLite file beside the bus, which every installation
    carries and nothing else can rebuild.

    The file holds three table families: the registry's rows, which this store's snapshot
    judges; presence (PresenceSchema), with its own header; and the leased turns (TurnSchema).
    Each read judges only its own family, and a commit writes all three on one transaction. A
    runtime copy retires its processes before any carry reaches the newer families
    (RegistryFile.copy_group), so no family's schema change refuses a reader of another."""

    root: Path
    tables = RegistryRows

    @classmethod
    def create_schema(cls, db) -> None:
        """A new registry file holds every family its commit writes."""
        super().create_schema(db)
        for family in RegistryFamily.__subclasses__():
            if not family.stored_schema(db):
                family.create_schema(db)

    @classmethod
    def acquire_declarations(cls) -> None:
        """The family a registry row names, known before the registry's lock is taken: a
        removal row records which named-row family it removed from."""
        RegistryNamedRow.acquire_declarations()

    def mark(self) -> RegistryRevisionMark:
        with self.snapshot() as db:
            return self.mark_in(db)

    @staticmethod
    def mark_in(db) -> RegistryRevisionMark:
        """The last committed revision; 0 before the first commit."""
        return RegistryRevisionMark(
            max((header.revision for header in RegistryHeader.select(db)), default=0))

    def revision(self) -> RegistryRowRevision:
        return self.remembered("revision", self._read_revision)

    def _read_revision(self) -> RegistryRowRevision:
        with self.snapshot() as db:
            return self.revision_in(db)

    def revision_in(self, db) -> RegistryRowRevision:
        return RegistryRowRevision(FileRevision.capture(self.path).file_identity(), self.mark_in(db))

    def captured(self, earlier: RegistryCapture, current: RegistryRowRevision) -> RegistryCapture:
        """Refresh only changed rows from the same database; a replacement starts a new read.
        ``current`` is the committed revision the caller read under the guard it still holds."""
        if current == earlier.revision:
            return earlier
        with self.snapshot() as db:
            revision = self.revision_in(db)
            if revision == earlier.revision:
                return earlier
            if revision.mark.revision == 0:
                return replace(RegistryCapture.empty(), revision=revision)
            base = earlier if revision.extends(earlier.revision) else RegistryCapture.empty()
            since = (base.mark.revision,)
            return base.changed(RegistryChanges(
                revision=revision,
                header=RegistryHeader.select(db)[0],
                rows={family: tuple(family.select(db, where="changed_at > ?", parameters=since))
                      for family in RegistryNamedRow.families()},
                removals=tuple(RegistryRemovals.select(
                    db, where="changed_at > ?", parameters=since)),
            ))

    def commit(
        self, raw: RawDocument, previous: RegistryCapture, beside: EditTables,
    ) -> RegistryRevisionMark:
        """Write the rows of ``raw`` that differ from ``previous`` as its successor, and what the
        same edit wrote beside them, in one transaction; refused if another revision committed
        since ``previous``."""
        with self.writing() as db:
            if not self.revision_in(db).accepts_write(previous.revision):
                raise StaleFence("registry rows changed after this write read them")
            beside.write_in(db)
            mark = previous.mark.successor()
            scalars = raw
            for family in RegistryNamedRow.families():
                scalars = family.placed(scalars, {})
                current = RowMapping.owning(family.mapping_in(raw))
                earlier = RowMapping.owning(family.mapping_in(previous.raw))
                for name in current.changed_in_order(earlier):
                    if name in current:
                        family(changed_at=mark.revision, name=name,
                               value=family.stored(current[name])).upsert(db)
                    else:
                        family.delete(db, name=name)
                        RegistryRemovals(changed_at=mark.revision, family=family, name=name).upsert(db)
            RegistryHeader(header=1, revision=mark.revision,
                           scalars=StoredWire(cast(JsonValue, scalars))).upsert(db)
            return mark


    @contextmanager
    def family_snapshot(self, family: type[TableSchema]) -> Iterator[sqlite3.Connection]:
        """A committed read judged against one table family's declaration."""
        with self.committed_or_empty(type(self), self.reader_snapshot) as connection:
            family.require_schema(connection)
            yield connection

    def namespace(self) -> tuple[RegistryNamespace, ...]:
        """This root's native namespace, or none for a public root; read again only after a
        commit, since every privacy decision asks it."""
        return self.remembered("namespace", self._read_namespace)

    def _read_namespace(self) -> tuple[RegistryNamespace, ...]:
        with self.family_snapshot(NamespaceSchema) as db:
            return tuple(RegistryNamespace.select(db))

    def write_namespace(self, root_id: RootId) -> None:
        """Record the namespace once; a different one already recorded is refused."""
        with self.writing() as db:
            NamespaceSchema.require_schema(db)
            if any(row.root_id != root_id for row in RegistryNamespace.select(db)):
                raise RelationViolationError("Registry already belongs to another native namespace")
            RegistryNamespace(header=1, root_id=root_id).insert_if_absent(db)

    def withdraw_namespace(self) -> None:
        """A copy kept for history scopes no native inputs: it has no namespace."""
        with self.writing() as db:
            NamespaceSchema.require_schema(db)
            RegistryNamespace.delete(db)

    def turn_wires(self, identities: tuple[TurnIdentity, ...]) -> dict[TurnIdentity, JsonValue]:
        with self.family_snapshot(TurnSchema) as db:
            return {identity: rows[0].active.wire for identity in identities
                    if (rows := TurnRow.select(db, **TurnRow.key(identity)))}

    def change_turn_wire[Result](
        self, identity: TurnIdentity,
        change: Callable[[JsonValue], Iterable[tuple[JsonValue, Result]]],
    ) -> tuple[Result, ...]:
        """A phase change is no registry edit: it takes SQLite's write lock for its own row and
        publishes nothing to the registry's readers."""
        with self.writer_connection() as db, self.write_transaction(db):
            self.require_maintenance_open()
            TurnSchema.require_schema(db)
            rows = TurnRow.select(db, **TurnRow.key(identity))
            if not rows:
                raise RelationViolationError(f"Active turn record is missing: {identity}")
            original = written = rows[0].active.wire
            results = []
            for active, result in change(original):
                written = active
                results.append(result)
            if written != original:
                TurnRow.of(identity, written).upsert(db)
            return tuple(results)

    def presence_revision_in(self, db) -> PresenceRevision:
        headers = PresenceHeader.select(db, header=1)
        return PresenceRevision(FileRevision.capture(self.path).file_identity(),
                                headers[0].revision if headers else 0)

    def presence_revision(self) -> PresenceRevision:
        return self.remembered("presence_revision", self._read_presence_revision)

    def _read_presence_revision(self) -> PresenceRevision:
        with self.family_snapshot(PresenceSchema) as db:
            return self.presence_revision_in(db)

    def read_presence(self, earlier: PresenceCapture = PresenceCapture.empty()) -> PresenceCapture:
        """The presence committed now, read as the rows changed since ``earlier``."""
        if self.presence_revision() == earlier.revision:
            return earlier
        with self.family_snapshot(PresenceSchema) as db:
            revision = self.presence_revision_in(db)
            if revision == earlier.revision:
                return earlier
            base = earlier if revision.extends(earlier.revision) else PresenceCapture.empty()
            seen = base.seen.copy()
            for row in PresenceRow.select(
                    db, where="changed_at > ?", parameters=(base.revision.counter,)):
                seen[row.name] = row.seen
            return PresenceCapture(revision, seen.freeze())

    def write_presence(self, presence: PresenceChange) -> None:
        """An edit that changed presence alone: its own transaction, the registry untouched."""
        if presence.names():
            with self.writing() as db:
                self.presence_in(db, presence)

    @staticmethod
    def presence_in(db, presence: PresenceChange) -> None:
        """On the caller's write transaction, which may also be committing registry rows."""
        names = presence.names()
        if not names:
            return
        revision = max((header.revision for header in PresenceHeader.select(db)), default=0) + 1
        for name in names:
            PresenceRow(changed_at=revision, name=name, seen=presence.after[name]).upsert(db)
        PresenceHeader(header=1, revision=revision).upsert(db)
