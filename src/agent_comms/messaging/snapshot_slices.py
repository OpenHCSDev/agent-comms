"""How a declared document field is copied for edits and frozen for snapshots.

Each field names its policy in SlicePolicy metadata. The registry document and the
display references both declare their fields with these policies; neither owns them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping, MutableMapping
from types import MappingProxyType
from typing import Self

from agent_comms.nominal.field_codec import FieldCodec, FieldPlan, MalformedRecord, SlicePolicy
from agent_comms.storage.row_mapping import RowMapping, SnapshotRows


class ThreadSlice[Value](ABC):
    """How a registry field is copied. Each field declares its policy in metadata."""

    @classmethod
    def owned(cls, value: Value) -> Value:
        """Admit a field into the representation its declared policy owns."""
        return value

    @classmethod
    @abstractmethod
    def copy(cls, value: Value) -> Value:
        """A copy an edit can change without changing ``value``."""

    @staticmethod
    def of(plan: FieldPlan) -> type[ThreadSlice]:
        for policy in SlicePolicy.single(plan.annotation):
            return policy.value
        raise TypeError(f"{plan.name} declares no thread slice")

    @classmethod
    def snapshot_value(cls, plan: FieldPlan) -> type[SnapshotValue]:
        raise TypeError(f"{plan.name} declares no snapshot value policy")

    @classmethod
    def reuse(cls, rows: Mapping[str, object], changed_names: Collection[str],
              earlier: Value, changed: Value) -> Value:
        raise TypeError(f"{cls.__name__} does not hold reusable rows")

    @classmethod
    def wire_changes(cls, current: object, earlier: object) -> object:
        """Whole-field values are decoded as declared; row policies narrow their payload."""
        return current

    @classmethod
    def refreshed(cls, current: object, previous: object, earlier: Value, decoded: Value) -> Value:
        """Merge a decoded revision according to the field's declared representation."""
        return decoded

    @classmethod
    def only(cls, value: Value, names: frozenset[str]) -> Value:
        """This field in a slice holding only ``names``: a field every thread shares is whole."""
        return value

    @classmethod
    def changed_names(cls, earlier: Value, current: Value, every: frozenset[str]) -> frozenset[str]:
        """The names a change to this field can affect: a field shared by every thread
        affects all of them; an own-row field overrides this with the rows that differ."""
        return every if earlier != current else frozenset()

    @classmethod
    def required[Document: type](cls, document: Document) -> Document:
        """Refuse, at class definition, a document field without a slice kind."""
        for plan in FieldCodec.field_plans(document):
            if plan.declared.init and plan.records:
                cls.of(plan)
        return document


class SnapshotValue[Value](ThreadSlice[Value]):
    """A declared registry field owns both editable and snapshot copies."""

    @classmethod
    @abstractmethod
    def freeze(cls, value: Value) -> object: ...

    @staticmethod
    def of(plan: FieldPlan) -> type[SnapshotValue]:
        return ThreadSlice.of(plan).snapshot_value(plan)

    @classmethod
    def snapshot_value(cls, plan: FieldPlan) -> type[SnapshotValue]:
        return cls


class SnapshotRestored:
    """Restoration copies each declared init field by its snapshot policy and re-enters the
    constructor, so derived views are rebuilt rather than pickled."""
    __slots__ = ()

    def __reduce__(self) -> tuple[type[Self], tuple[object, ...]]:
        return type(self), tuple(
            SnapshotValue.of(plan).copy(plan.current(self))
            for plan in FieldCodec.field_plans(type(self)) if plan.declared.init
        )


class OwnRow[Rows](SnapshotValue[Rows]):
    """The row under the thread's own name."""

    @classmethod
    @abstractmethod
    def reuse(cls, rows: Mapping[str, object], changed_names: Collection[str],
              earlier: Rows, changed: Rows) -> Rows: ...

    @classmethod
    def wire_rows(cls, value: object) -> RowMapping:
        """Admit a wire mapping at the row decoder boundary, retaining owned change evidence."""
        if not isinstance(value, Mapping):
            raise MalformedRecord("registry rows must be an object")
        return RowMapping.owning(value)

    @classmethod
    def wire_changes(cls, current: object, earlier: object) -> dict[str, object]:
        rows, before = cls.wire_rows(current), cls.wire_rows(earlier)
        return {name: rows[name] for name in rows.changed_in_order(before) if name in rows}

    @classmethod
    def refreshed(cls, current: object, previous: object, earlier: Rows, decoded: Rows) -> Rows:
        rows, before = cls.wire_rows(current), cls.wire_rows(previous)
        return cls.reuse(rows, rows.changed_in_order(before), earlier, decoded)

    @classmethod
    @abstractmethod
    def differing(cls, earlier: Rows, current: Rows) -> frozenset[str]:
        """The names whose own row differs between two values of this field."""

    @classmethod
    def changed_names(cls, earlier: Rows, current: Rows, every: frozenset[str]) -> frozenset[str]:
        return cls.differing(earlier, current)


class MappingRowsSlice(OwnRow[MutableMapping]):
    """A plain mapping snapshot, without imposing a persistent representation."""

    @classmethod
    def copy(cls, value: Mapping) -> dict:
        return dict(value)

    @classmethod
    def freeze(cls, value: Mapping) -> Mapping:
        return MappingProxyType(cls.copy(value))

    @classmethod
    def reuse(cls, rows: Mapping[str, object], changed_names: Collection[str],
              earlier: MutableMapping, changed: MutableMapping) -> MutableMapping:
        changed_keys = frozenset(changed_names)
        return {name: (changed if name in changed_keys else earlier)[name] for name in rows}

    @classmethod
    def only(cls, value: MutableMapping, names: frozenset[str]) -> MutableMapping:
        return {name: row for name, row in value.items() if name in names}

    @classmethod
    def differing(cls, earlier: Mapping, current: Mapping) -> frozenset[str]:
        absent = object()
        return frozenset(name for name in earlier.keys() | current.keys()
                         if earlier.get(name, absent) != current.get(name, absent))


class SharedRowsSlice(MappingRowsSlice):
    """Edits and snapshots share immutable roots and compare their changed keys."""

    @classmethod
    def owned(cls, value: Mapping) -> SnapshotRows:
        return RowMapping.owning(value)

    @classmethod
    def copy(cls, value: Mapping) -> SnapshotRows:
        return cls.owned(value).copy()

    @classmethod
    def freeze(cls, value: Mapping) -> Mapping:
        return cls.owned(value).freeze()

    @classmethod
    def reuse(cls, rows: Mapping[str, object], changed_names: Collection[str],
              earlier: MutableMapping, changed: MutableMapping) -> SnapshotRows:
        merged = cls.owned(earlier).copy()
        for name in changed_names:
            if name in rows:
                merged[name] = changed[name]
            else:
                del merged[name]
        return merged

    @classmethod
    def only(cls, value: MutableMapping, names: frozenset[str]) -> MutableMapping:
        return cls.owned(value).only(names)

    @classmethod
    def differing(cls, earlier: Mapping, current: Mapping) -> frozenset[str]:
        return cls.owned(current).differing(cls.owned(earlier))


class WholeDocumentValue[Value](SnapshotValue[Value]):
    """An immutable field shared by editable documents and snapshots."""

    @classmethod
    def copy(cls, value: Value) -> Value:
        return value

    @classmethod
    def freeze(cls, value: Value) -> Value:
        return cls.copy(value)
