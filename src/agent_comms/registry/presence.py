"""Keyed presence updates, independent of declaration revisions.

Rows are never dropped: a retained registry revision may still name an old
thread. Presentation scopes those rows without copying the complete collection.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, ClassVar, Self

from agent_comms.messaging.snapshot_slices import SharedRowsSlice
from agent_comms.nominal.field_codec import MalformedRecord
from agent_comms.storage.private_path import FileIdentity, FileRevision, MissingFile
from agent_comms.storage.row_mapping import ReadOnlyRows, RowKeys, RowMapping, SnapshotRows
from agent_comms.storage.store_files import DependentStateTier
from agent_comms.storage.typed_table import Column, DurableStore, FrozenColumn, Index, StoreRows, TypedTable


class PresenceTables(StoreRows):
    """The presence file discovers its declared tables."""


@dataclass(frozen=True)
class PresenceHeader(PresenceTables, TypedTable):
    header: Annotated[int, FrozenColumn(primary_key=True, check="header = 1")]
    revision: Annotated[int, Column(check="revision > 0")]


@dataclass(frozen=True)
class PresenceRow(PresenceTables, TypedTable):
    name: Annotated[str, FrozenColumn(primary_key=True)]
    seen: float
    changed_at: Annotated[int, Column(check="changed_at > 0")]
    indexes = (Index(("changed_at",), unique=False, where=None),)


@dataclass(frozen=True, slots=True)
class PresenceRevision:
    identity: FileIdentity | MissingFile
    counter: int

    def extends(self, earlier: PresenceRevision) -> bool:
        return self.identity == earlier.identity and self.counter >= earlier.counter


@dataclass(frozen=True)
class PresenceCapture:
    revision: PresenceRevision
    seen: RowMapping[str, float]

    @classmethod
    def empty(cls) -> Self:
        return cls(PresenceRevision(MissingFile(), 0), RowMapping().freeze())


class PresenceRows(SnapshotRows[str, float]):
    """A declaration-scoped presence view, editable through a small change set."""

    def __init__(self) -> None:
        self._source = PresenceCapture.empty()
        self._values: RowMapping[str, float] = RowMapping()
        self._scope: RowKeys[str] = RowMapping().keyset()
        self._names: RowMapping[str, object] = RowMapping()

    @classmethod
    def owning(cls, values: Mapping[str, float]) -> PresenceRows:
        if isinstance(values, PresenceRows):
            return values
        result = cls()
        result._values = RowMapping.owning(values).copy()
        result._scope = result._values.keyset()
        result._names = result._scope.rows.copy()
        return result

    def __contains__(self, name: object) -> bool:
        return name in self._names

    def __getitem__(self, name: str) -> float:
        if name not in self:
            raise KeyError(name)
        return self._values[name]

    def __len__(self) -> int:
        return len(self._names)

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __setitem__(self, name: str, when: float) -> None:
        if name not in self._names:
            self._names[name] = None
        self._values[name] = when

    def __delitem__(self, name: str) -> None:
        del self._names[name]

    def clear(self) -> None:
        self._names.clear()

    def popitem(self) -> tuple[str, float]:
        name = self._names.any_key()
        when = self[name]
        del self[name]
        return name, when

    def _copy_into(self, following: PresenceRows) -> PresenceRows:
        following._source = self._source
        following._scope = self._scope
        following._values = self._values.copy()
        following._names = self._names.copy()
        return following

    def copy(self) -> PresenceRows:
        return self._copy_into(PresenceRows())

    def freeze(self) -> PresenceRows:
        result = self._copy_into(FrozenPresenceRows())
        result._values = self._values.freeze()
        result._names = self._names.freeze()
        return result

    def only(self, names: Iterable[str]) -> PresenceRows:
        return PresenceRows.owning({name: self[name] for name in names if name in self})

    def differing(self, earlier: PresenceRows) -> frozenset[str]:
        candidates = (self._values.differing(earlier._values)
                      | self._names.keyset().differing(earlier._names.keyset()))
        return frozenset(name for name in candidates
                         if (name in self) != (name in earlier)
                         or (name in self and self[name] != earlier[name]))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PresenceRows):
            return not self.differing(other)
        if not isinstance(other, Mapping):
            return NotImplemented
        return len(self) == len(other) and all(name in other and self[name] == other[name] for name in self)

    def presented(self, captured: PresenceCapture, scope: RowKeys[str]) -> PresenceRows:
        # Presence writes only add rows. A continuing file therefore needs coverage
        # checks for new declaration keys alone; replacement starts a complete check.
        changed = scope.differing(self._scope) if captured.revision.extends(self._source.revision) else scope
        for name in changed:
            if name in scope and name not in captured.seen:
                raise MalformedRecord("a declared thread has no presence row")
        result = FrozenPresenceRows()
        result._source = captured
        result._values = captured.seen
        result._scope = scope
        result._names = scope.rows.copy()
        return result


class FrozenPresenceRows(ReadOnlyRows[str, float], PresenceRows):
    """A retained scoped view cannot mutate its timestamp rows."""


class PresenceRowsSlice(SharedRowsSlice):
    @classmethod
    def owned(cls, value: Mapping[str, float]) -> PresenceRows:
        return PresenceRows.owning(value)



@dataclass(frozen=True, slots=True)
class PresenceStore(DurableStore, DependentStateTier):
    """One transaction writes only the presence keys that changed."""

    root: Path
    filename: ClassVar[str] = "presence.sqlite3"
    tables = PresenceTables

    def revision_in(self, db: Connection) -> PresenceRevision:
        headers = PresenceHeader.select(db, header=1)
        return PresenceRevision(FileRevision.capture(self.path).file_identity(),
                                headers[0].revision if headers else 0)

    def revision(self) -> PresenceRevision:
        return self.remembered("revision", self._read_revision)

    def _read_revision(self) -> PresenceRevision:
        with self.snapshot() as db:
            return self.revision_in(db)

    def read(self, earlier: PresenceCapture = PresenceCapture.empty()) -> PresenceCapture:
        if self.revision() == earlier.revision:
            return earlier
        with self.snapshot() as db:
            revision = self.revision_in(db)
            if revision == earlier.revision:
                return earlier
            base = earlier if revision.extends(earlier.revision) else PresenceCapture.empty()
            seen = base.seen.copy()
            for row in PresenceRow.select(db, where="changed_at > ?", parameters=(base.revision.counter,)):
                seen[row.name] = row.seen
            return PresenceCapture(revision, seen.freeze())

    def set_rows(self, after: Mapping[str, float], before: Mapping[str, float]) -> None:
        current, earlier = PresenceRows.owning(after), PresenceRows.owning(before)
        changed = tuple(name for name in current.differing(earlier) if name in current)
        if not changed:
            return
        with self.writing() as db:
            revision = self.revision_in(db).counter + 1
            for name in changed:
                PresenceRow(name=name, seen=current[name], changed_at=revision).upsert(db)
            PresenceHeader(header=1, revision=revision).upsert(db)
