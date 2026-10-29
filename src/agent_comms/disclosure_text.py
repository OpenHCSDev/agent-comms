"""Human-readable declared values shared by heartbeat text and native views."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from itertools import zip_longest
from textwrap import wrap
from typing import ClassVar, Hashable

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, FieldPlan, ValueDescription, ViewColumn


@dataclass(frozen=True)
class DisclosureText:
    value: object

    @classmethod
    def of(cls, value: object) -> DisclosureText:
        return FieldCodec.shape(value).describe(DisclosureShapes)

    def summary(self) -> str:
        return str(self.value)

    @classmethod
    def view(cls, value: object, view: str) -> tuple[str, ...]:
        """Read the original values, not their flattened JSON representations."""
        return tuple(cls.of(column.current(value)).summary()
                     for column in FieldCodec.view_columns(type(value), view))


@dataclass(frozen=True)
class ViewFacts:
    """Declared displayed values; paint-time fields may exclude publication comparison."""

    record: type
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ViewSectionFacts:
    heading: str
    tables: tuple[ViewFacts, ...]


@dataclass(frozen=True)
class ViewTable[R]:
    """One declaration owns semantic cells and both wide and stacked presentation."""

    record: type[R]
    view: str
    records: tuple[R, ...]
    show_header: ClassVar[bool] = True
    row_separator: ClassVar[tuple[str, ...]] = ()

    @cached_property
    def declared_columns(self) -> tuple[ViewColumn, ...]:
        return FieldCodec.view_columns(self.record, self.view)

    @property
    def columns(self) -> tuple[ViewColumn, ...]:
        return self.declared_columns

    @cached_property
    def cells(self) -> tuple[tuple[DisclosureText, ...], ...]:
        return tuple(tuple(DisclosureText.of(column.current(record)) for column in self.declared_columns)
                     for record in self.records)

    @cached_property
    def texts(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(cell.summary() for cell in row) for row in self.cells)

    @cached_property
    def facts(self) -> ViewFacts:
        retained = tuple(index for index, column in enumerate(self.declared_columns)
                         if not column.excluded_in("publication"))
        return ViewFacts(self.record, tuple(self.declared_columns[index].label for index in retained),
                         tuple(tuple(row[index].summary() for index in retained) for row in self.cells))

    @cached_property
    def minimums(self) -> tuple[int, ...]:
        return tuple(column.minimum_width(row[index] for row in self.texts)
                     for index, column in enumerate(self.columns))

    def natural_widths(self) -> tuple[int, ...]:
        return tuple(max(len(column.label), max(
            (len(line) for row in self.texts for line in row[index].splitlines()), default=0,
        )) for index, column in enumerate(self.columns))

    def widths(self, available: int) -> tuple[int, ...]:
        return ViewColumn.fit_widths(self.natural_widths(), available, self.minimums)

    def layout(self, available: int) -> ViewTable[R]:
        """Geometry classification happens once here, never again in the native adapter."""
        if sum(self.minimums) > available:
            return StackedViewTable(self.record, self.view, self.records)
        return self

    def header_rows(self) -> tuple[tuple[str, ...], ...]:
        return (tuple(column.label for column in self.columns),)

    def lines(self) -> tuple[str, ...]:
        return self.render_lines(self.natural_widths())

    def render_lines(self, widths: tuple[int, ...]) -> tuple[str, ...]:
        lines: list[str] = []
        for row in (*self.header_rows(), *self.texts):
            wrapped = tuple(tuple(part for line in text.splitlines() or ("",)
                                  for part in wrap(line, max(1, cell_width), replace_whitespace=False,
                                                   break_long_words=False, break_on_hyphens=False) or ("",))
                            for text, cell_width in zip(row, widths))
            lines.extend("  ".join(value.ljust(cell_width) for value, cell_width in zip(parts, widths)).rstrip()
                         for parts in zip_longest(*wrapped, fillvalue=""))
            lines.extend(self.row_separator)
        return tuple(lines)

    def labelled_lines(self) -> tuple[str, ...]:
        """Compact strips use declared labels too, not another list of metric names."""
        return tuple(" · ".join(f"{column.label}: {cell}"
                                for column, cell in zip(self.columns, row)) for row in self.texts)


class StackedViewTable[R](ViewTable[R]):
    """One native transport cell retains every declared label/value and original record."""

    show_header = False
    row_separator = ("",)

    @property
    def columns(self) -> tuple[ViewColumn, ...]:
        return self.declared_columns[:1]

    @cached_property
    def texts(self) -> tuple[tuple[str, ...], ...]:
        return tuple(("\n".join(f"{column.label}: {cell.summary()}"
                                 for column, cell in zip(self.declared_columns, row)),)
                     for row in self.cells)

    def widths(self, available: int) -> tuple[int, ...]:
        return (max(1, available),)

    def layout(self, available: int) -> ViewTable[R]:
        return self

    def header_rows(self) -> tuple[tuple[str, ...], ...]:
        return ()


class HudTone(DeclaredFamily, affix="Tone"):
    terminal_colour: ClassVar[int]

    @classmethod
    def paint(cls, text: str, colour: bool) -> str:
        return f"\x1b[{cls.terminal_colour}m{text}\x1b[0m" if colour else text


class MutedTone(HudTone):
    terminal_colour = 90


class WarningTone(HudTone):
    terminal_colour = 33


class SuccessTone(HudTone):
    terminal_colour = 32


class AccentTone(HudTone):
    terminal_colour = 36


class ErrorTone(HudTone):
    terminal_colour = 31


class ViewHeading(DeclaredFamily, declared_name=None):
    """Alarm sections and unit groups share paint/headings, not family membership."""

    tone: ClassVar[type[HudTone]] = MutedTone

    @classmethod
    def heading(cls) -> str:
        return cls.declared_name.replace("_", " ").upper()

    @classmethod
    def count_heading(cls, count: int) -> str:
        return f"{cls.heading()} · {count}"

    @classmethod
    def paint(cls, text: str, colour: bool) -> str:
        return cls.tone.paint(text, colour)


class RecordText(DisclosureText):
    def fields(self) -> tuple[FieldPlan, ...]:
        """All disclosed fields; identity-only summaries never remove detail children."""
        return tuple(plan for plan in FieldCodec.field_plans(type(self.value)) if plan.records)

    def summary_fields(self) -> tuple[FieldPlan, ...]:
        return self.fields()

    def summary(self) -> str:
        return " · ".join(DisclosureText.of(plan.current(self.value)).summary()
                          for plan in self.summary_fields())


@dataclass(frozen=True)
class IdentifiedRecordText(RecordText):
    identity: tuple[FieldPlan, ...]

    def summary_fields(self) -> tuple[FieldPlan, ...]:
        return self.identity


@dataclass(frozen=True)
class ClassText(DisclosureText):
    value: type

    def summary(self) -> str:
        return self.value.__name__


@dataclass(frozen=True)
class FamilyText(ClassText):
    value: type[DeclaredFamily]

    def summary(self) -> str:
        return self.value.declared_name


class CollectionText(DisclosureText):
    value: Collection[object]
    noun: ClassVar[str] = "item"

    def summary(self) -> str:
        count = len(self.value)
        return f"{count} {self.noun}{'s' if count != 1 else ''}"


@dataclass(frozen=True)
class SequenceText(CollectionText):
    value: Sequence[object]


@dataclass(frozen=True)
class MappingText(CollectionText):
    value: Mapping[Hashable, object]
    noun = "field"


class DisclosureShapes(ValueDescription[DisclosureText]):
    @classmethod
    def scalar(cls, value: object) -> DisclosureText:
        return DisclosureText(value)

    @classmethod
    def record(cls, value: object) -> DisclosureText:
        return RecordText(value)

    @classmethod
    def identified_record(cls, value: object, identity: tuple[FieldPlan, ...]) -> DisclosureText:
        return IdentifiedRecordText(value, identity)

    @classmethod
    def class_value(cls, value: type) -> DisclosureText:
        return ClassText(value)

    @classmethod
    def family_value(cls, value: type[DeclaredFamily]) -> DisclosureText:
        return FamilyText(value)

    @classmethod
    def mapping(cls, value: Mapping[Hashable, object]) -> DisclosureText:
        return MappingText(value)

    @classmethod
    def sequence(cls, value: Sequence[object]) -> DisclosureText:
        return SequenceText(value)
