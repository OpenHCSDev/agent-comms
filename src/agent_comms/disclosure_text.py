"""Human-readable declared values shared by heartbeat text and native views."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, is_dataclass
from functools import cached_property, singledispatchmethod
from itertools import zip_longest
from textwrap import wrap
from typing import ClassVar, Hashable

from agent_comms.declared_family import FamilyMeta
from agent_comms.field_codec import DisclosureIdentity, FieldCodec, FieldPlan, ViewColumn


@dataclass(frozen=True)
class DisclosureText:
    value: object

    @singledispatchmethod
    @classmethod
    def of(cls, value: object) -> DisclosureText:
        """Recover foreign shape once, before heartbeat and UI consumers borrow it."""
        if is_dataclass(value):
            identity = tuple(
                plan for plan in FieldCodec.field_plans(type(value))
                if DisclosureIdentity.read(plan.annotation, False)
            )
            if identity:
                return IdentifiedRecordText(value, identity)
            return RecordText(value)
        return cls(value)

    @of.register(type)
    @classmethod
    def _class(cls, value: type) -> DisclosureText:
        return ClassText(value)

    @of.register(FamilyMeta)
    @classmethod
    def _family(cls, value: FamilyMeta) -> DisclosureText:
        return FamilyText(value)

    @of.register(Mapping)
    @classmethod
    def _mapping(cls, value: Mapping[Hashable, object]) -> DisclosureText:
        return MappingText(value)

    @of.register(Sequence)
    @classmethod
    def _sequence(cls, value: Sequence[object]) -> DisclosureText:
        return SequenceText(value)

    @of.register(str)
    @of.register(bytes)
    @classmethod
    def _scalar(cls, value: str | bytes) -> DisclosureText:
        return cls(value)

    def summary(self, width: int | None = None) -> str:
        text = str(self.value)[:width]
        return text if width is None else text.partition("\n")[0]

    @classmethod
    def view(cls, value: object, view: str) -> tuple[str, ...]:
        """Read the original values, not their flattened JSON representations."""
        return tuple(cls.of(column.current(value)).summary()
                     for column in FieldCodec.view_columns(type(value), view))


@dataclass(frozen=True)
class ViewTable[R]:
    """One declared column set owns cells, content demand and optional reader-side fitting."""

    record: type[R]
    view: str
    records: tuple[R, ...]

    @cached_property
    def columns(self) -> tuple[ViewColumn, ...]:
        return FieldCodec.view_columns(self.record, self.view)

    @cached_property
    def cells(self) -> tuple[tuple[DisclosureText, ...], ...]:
        return tuple(tuple(DisclosureText.of(column.current(record)) for column in self.columns)
                     for record in self.records)

    @cached_property
    def texts(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(cell.summary() for cell in row) for row in self.cells)

    def widths(self, available: int | None = None) -> tuple[int, ...]:
        weights = tuple(max(len(column.label), max(
            (len(line) for row in self.texts for line in row[index].splitlines()), default=0,
        )) for index, column in enumerate(self.columns))
        if available is None:
            return weights
        minimums = tuple(column.minimum_width(row[index] for row in self.texts)
                         for index, column in enumerate(self.columns))
        return ViewColumn.fit_widths(weights, available, minimums)

    def lines(self, width: int | None = None) -> tuple[str, ...]:
        """A producer supplies no width. Only a reader may ask this view to fit its viewport."""
        available = None if width is None else width - len("  ") * (len(self.columns) - 1)
        widths = self.widths(available)
        lines = []
        for row in (tuple(column.label for column in self.columns), *self.texts):
            wrapped = tuple(tuple(part for line in text.splitlines() or ("",)
                                  for part in wrap(line, cell_width, replace_whitespace=False) or ("",))
                            for text, cell_width in zip(row, widths))
            lines.extend("  ".join(value.ljust(cell_width) for value, cell_width in zip(parts, widths)).rstrip()
                         for parts in zip_longest(*wrapped, fillvalue=""))
        return tuple(lines)


class RecordText(DisclosureText):
    def summary_fields(self) -> tuple[FieldPlan, ...]:
        return tuple(plan for plan in FieldCodec.field_plans(type(self.value)) if plan.records)

    def summary(self, width: int | None = None) -> str:
        parts: list[str] = []
        length = 0
        for plan in self.summary_fields():
            part = DisclosureText.of(plan.current(self.value)).summary(width)
            parts.append(part)
            length += len(part) + len(" · ")
            if width is not None and length >= width:
                break
        return " · ".join(parts)[:width]


@dataclass(frozen=True)
class IdentifiedRecordText(RecordText):
    identity: tuple[FieldPlan, ...]

    def summary_fields(self) -> tuple[FieldPlan, ...]:
        return self.identity


@dataclass(frozen=True)
class ClassText(DisclosureText):
    value: type

    def summary(self, width: int | None = None) -> str:
        return self.value.__name__[:width]


@dataclass(frozen=True)
class FamilyText(ClassText):
    value: FamilyMeta

    def summary(self, width: int | None = None) -> str:
        return self.value.declared_name[:width]


class CollectionText(DisclosureText):
    value: Collection[object]
    noun: ClassVar[str] = "item"

    def summary(self, width: int | None = None) -> str:
        count = len(self.value)
        return f"{count} {self.noun}{'s' if count != 1 else ''}"[:width]


@dataclass(frozen=True)
class SequenceText(CollectionText):
    value: Sequence[object]


@dataclass(frozen=True)
class MappingText(CollectionText):
    value: Mapping[Hashable, object]
    noun = "field"
