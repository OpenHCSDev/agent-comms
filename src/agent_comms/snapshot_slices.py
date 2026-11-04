"""How a declared document field is copied for edits and frozen for snapshots.

Each field names its policy in SlicePolicy metadata. The registry document and the
display references both declare their fields with these policies; neither owns them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, MutableMapping
from types import MappingProxyType
from typing import Self

from .nominal.field_codec import FieldCodec, FieldPlan, SlicePolicy


class ThreadSlice[Value](ABC):
    """How a registry field is copied. Each field declares its policy in metadata."""

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
    def reuse(cls, rows: Mapping[str, object], kept: frozenset[str],
              earlier: Value, changed: Value) -> None:
        raise TypeError(f"{cls.__name__} does not hold reusable rows")

    @classmethod
    def own_row_plans(cls, plan: FieldPlan) -> tuple[FieldPlan, ...]:
        """The plans holding one row per thread name: none, unless the field is an own row."""
        return ()

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
    def reuse(cls, rows: Mapping[str, object], kept: frozenset[str],
              earlier: Rows, changed: Rows) -> None: ...

    @classmethod
    def own_row_plans(cls, plan: FieldPlan) -> tuple[FieldPlan, ...]:
        return (plan,)

    @classmethod
    @abstractmethod
    def differing(cls, earlier: Rows, current: Rows) -> frozenset[str]:
        """The names whose own row differs between two values of this field."""

    @classmethod
    def changed_names(cls, earlier: Rows, current: Rows, every: frozenset[str]) -> frozenset[str]:
        return cls.differing(earlier, current)


class MappingRowsSlice(OwnRow[MutableMapping]):
    @classmethod
    def copy(cls, value: Mapping) -> dict:
        return dict(value)

    @classmethod
    def freeze(cls, value: Mapping) -> Mapping:
        return MappingProxyType(cls.copy(value))

    @classmethod
    def reuse(cls, rows: Mapping[str, object], kept: frozenset[str],
              earlier: MutableMapping, changed: MutableMapping) -> None:
        merged = {name: (earlier if name in kept else changed)[name] for name in rows}
        changed.clear()
        changed.update(merged)

    @classmethod
    def differing(cls, earlier: Mapping, current: Mapping) -> frozenset[str]:
        absent = object()
        return frozenset(name for name in earlier.keys() | current.keys()
                         if earlier.get(name, absent) != current.get(name, absent))


class WholeDocumentValue[Value](SnapshotValue[Value]):
    """An immutable field shared by editable documents and snapshots."""

    @classmethod
    def copy(cls, value: Value) -> Value:
        return value

    @classmethod
    def freeze(cls, value: Value) -> Value:
        return cls.copy(value)
