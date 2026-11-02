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
            if issubclass(policy.value, ThreadSlice):
                return policy.value
        raise TypeError(f"{plan.name} declares no thread slice")

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
        policy = ThreadSlice.of(plan)
        if not issubclass(policy, SnapshotValue):
            raise TypeError(f"{plan.name} declares no snapshot value policy")
        return policy


class SnapshotRestored:
    """Restoration copies each declared init field by its snapshot policy and re-enters the
    constructor, so derived views are rebuilt rather than pickled."""
    __slots__ = ()

    def __reduce__(self) -> tuple[type[Self], tuple[object, ...]]:
        return type(self), tuple(
            SnapshotValue.of(plan).copy(getattr(self, plan.name))
            for plan in FieldCodec.field_plans(type(self)) if plan.declared.init
        )


class OwnRow[Rows](SnapshotValue[Rows]):
    """The row under the thread's own name."""

    @classmethod
    @abstractmethod
    def reuse(cls, rows: Mapping[str, object], kept: frozenset[str],
              earlier: Rows, changed: Rows) -> None: ...


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


class WholeDocumentValue[Value](SnapshotValue[Value]):
    """An immutable field shared by editable documents and snapshots."""

    @classmethod
    def copy(cls, value: Value) -> Value:
        return value

    @classmethod
    def freeze(cls, value: Value) -> Value:
        return cls.copy(value)
