"""The registry as rows: one thread, status, alias or generation per row, so a change writes the
rows it changes and nothing else (plans/REGISTRY-ROWS-PLAN.md).

The whole-document registry rewrote every row on every change: 5.6 MB and 2,933 threads on the
live bus to change one. Each mapping section of the document is one table of (name, value) rows,
the value in its wire form, so a reader keeps decoding only the rows it reads (ThreadRows); the
header row holds the rest of the document. A commit stamps the rows it writes with its revision,
and a reader holding revision r reads only what changed after r.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Annotated, ClassVar, Self

from pydantic import JsonValue

from agent_comms.coordination.errors import StaleFence
from agent_comms.nominal.field_codec import FieldCodec, WireValue
from agent_comms.registry.registry_document import RegistryDocument
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.typed_table import (
    Column,
    DurableStore,
    FrozenColumn,
    Index,
    StoreRows,
    TypedTable,
)

type RawDocument = dict[str, JsonValue]


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
class RevisionStamped(RegistryRow, declared_name=None):
    """A row stamped with the revision that last wrote it, so a reader holding revision r reads
    only the rows with changed_at > r."""

    changed_at: Annotated[int, Column(check="changed_at > 0")]
    indexes = (Index(("changed_at",), unique=False, where=None),)


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
    def section_wire(cls, document: RegistryDocument, earlier: RegistryDocument,
             stored: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        """This section's wire form: an entry still the object ``earlier`` held keeps its stored
        form, so a save encodes only the entries the edit replaced."""
        before = cls.entries(earlier)
        return {name: stored[name] if name in stored and before.get(name) is value
                else FieldCodec.encode(value)
                for name, value in cls.entries(document).items()}

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
    def mapping_in(cls, raw: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        """An absent section is one with no rows, as an older stored form may lack it."""
        value: JsonValue = raw
        for key in cls.section():
            value = value.get(key, {})
        return value

    @classmethod
    def placed(cls, raw: RawDocument, mapping: dict[str, JsonValue]) -> RawDocument:
        """``raw`` with this section replaced by ``mapping``, copying only the dicts on its path."""
        return cls._placed(raw, cls.section(), mapping)

    @staticmethod
    def _placed(raw: RawDocument, path: tuple[str, ...], mapping: dict[str, JsonValue]) -> RawDocument:
        head, *rest = path
        return {**raw, head: RegistryNamedRow._placed(raw.get(head, {}), tuple(rest), mapping)
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
    def section_wire(cls, document: RegistryDocument, earlier: RegistryDocument,
             stored: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        """ThreadRows already keeps each unassigned row's stored form."""
        return document.threads.to_wire()


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
class RegistryCapture:
    """One committed revision of the registry document, in its wire form."""

    mark: RegistryRevisionMark
    raw: RawDocument

    @property
    def revision(self) -> RegistryRevisionMark:
        return self.mark

    @classmethod
    def empty(cls) -> Self:
        return cls(RegistryRevisionMark(0), FieldCodec.encode(RegistryDocument()))

    def encoded(self, document: RegistryDocument, earlier: RegistryDocument) -> RawDocument:
        """``document``'s wire form, given that ``earlier`` was decoded from this capture: only
        what the edit replaced is encoded."""
        families = RegistryNamedRow.families()
        cleared = document
        for family in families:
            cleared = family.cleared(cleared)
        raw = FieldCodec.encode(cleared)
        for family in families:
            raw = family.placed(raw, family.section_wire(document, earlier, family.mapping_in(self.raw)))
        return raw

    def changed(self, changes: RegistryChanges) -> RegistryCapture:
        """This revision with ``changes`` applied: removals first, then rows, so a name removed
        and declared again after this revision ends declared. Unchanged sections stay shared."""
        if changes.mark == self.mark:
            return self
        raw = changes.header.scalars.wire
        for family in RegistryNamedRow.families():
            removed = changes.removed(family)
            written = changes.written(family)
            mapping = family.mapping_in(self.raw)
            if removed or written:
                mapping = {name: value for name, value in mapping.items() if name not in removed}
                mapping.update(written)
            raw = family.placed(raw, mapping)
        return RegistryCapture(changes.mark, raw)


@dataclass(frozen=True)
class RegistryChanges:
    """What changed after a revision: the header, the rows written and the rows removed."""

    mark: RegistryRevisionMark
    header: RegistryHeader
    rows: Mapping[type[RegistryNamedRow], tuple[RegistryNamedRow, ...]]
    removals: tuple[RegistryRemovals, ...]

    def written(self, family: type[RegistryNamedRow]) -> dict[str, JsonValue]:
        return {row.name: row.wire for row in self.rows[family]}

    def removed(self, family: type[RegistryNamedRow]) -> frozenset[str]:
        return frozenset(removal.name for removal in self.removals if removal.family is family)


@dataclass(frozen=True, slots=True)
class RegistryRowStore(DurableStore, RegistryFile):
    """The registry's rows: one owner-only SQLite file beside the bus, which every installation
    carries and nothing else can rebuild."""

    root: Path
    tables = RegistryRows

    def mark(self) -> RegistryRevisionMark:
        with self.snapshot() as db:
            return self.mark_in(db)

    @staticmethod
    def mark_in(db) -> RegistryRevisionMark:
        """The last committed revision; 0 before the first commit."""
        return RegistryRevisionMark(
            max((header.revision for header in RegistryHeader.select(db)), default=0))

    def captured(self, earlier: RegistryCapture) -> RegistryCapture:
        """The committed revision, read as the rows changed after ``earlier``: a reader pays for
        the change, never for the registry's size."""
        with self.snapshot() as db:
            mark = self.mark_in(db)
            if mark == earlier.mark:
                return earlier
            if mark.revision == 0:
                return RegistryCapture.empty()
            since = (earlier.mark.revision,)
            return earlier.changed(RegistryChanges(
                mark=mark,
                header=RegistryHeader.select(db)[0],
                rows={family: tuple(family.select(db, where="changed_at > ?", parameters=since))
                      for family in RegistryNamedRow.families()},
                removals=tuple(RegistryRemovals.select(
                    db, where="changed_at > ?", parameters=since)),
            ))

    def commit(self, raw: RawDocument, previous: RegistryCapture) -> RegistryRevisionMark:
        """Write the rows of ``raw`` that differ from ``previous`` as its successor, in one
        transaction; refused if another revision committed since ``previous``."""
        with self.writing() as db:
            if self.mark_in(db) != previous.mark:
                raise StaleFence("registry rows changed after this write read them")
            mark = previous.mark.successor()
            scalars = raw
            for family in RegistryNamedRow.families():
                scalars = family.placed(scalars, {})
                for row in self.differences(family, raw, previous.raw, mark):
                    row.upsert(db)
                for name in family.mapping_in(previous.raw).keys() - family.mapping_in(raw).keys():
                    family.delete(db, name=name)
                    RegistryRemovals(changed_at=mark.revision, family=family, name=name).upsert(db)
            RegistryHeader(header=1, revision=mark.revision, scalars=StoredWire(scalars)).upsert(db)
            return mark

    @staticmethod
    def differences(
        family: type[RegistryNamedRow], raw: RawDocument, previous: RawDocument,
        mark: RegistryRevisionMark,
    ) -> Iterator[RegistryNamedRow]:
        """The rows ``raw`` writes: identity first, so an unchanged row costs one pointer test."""
        earlier = family.mapping_in(previous)
        for name, value in family.mapping_in(raw).items():
            if name not in earlier or (earlier[name] is not value and earlier[name] != value):
                yield family(changed_at=mark.revision, name=name, value=family.stored(value))
