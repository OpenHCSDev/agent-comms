"""Human-readable declared values shared by heartbeat text and native views."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from functools import cached_property
from itertools import zip_longest
from textwrap import wrap
from types import MappingProxyType
from typing import ClassVar, Hashable

from metaclass_registry.mro_dispatch import handles

from agent_comms.nominal.declared_family import DeclaredFamily, FamilyMeta
from agent_comms.nominal.field_codec import FieldCodec, FieldPlan, PublicationExclude, ValueDescription, ViewColumn, ViewExclusion


class DisclosureKey:
    """Semantic disclosure identity, independent of native widget allocation."""


@dataclass(frozen=True)
class RecordKey(DisclosureKey):
    owner: type


class RootKey(RecordKey):
    """The whole record, not one of its fields."""


@dataclass(frozen=True)
class FieldKey(RecordKey):
    name: str


@dataclass(frozen=True)
class EntityKey(RecordKey):
    parts: tuple[object, ...]


class AnonymousKey(DisclosureKey):
    """An undeclared identity cannot inherit another item's selection."""


@dataclass(frozen=True)
class MappingKey(DisclosureKey):
    key: Hashable


@dataclass(frozen=True)
class DisclosureText(ValueDescription):
    value: object

    @cached_property
    def summary(self) -> str:
        return str(self.value)

    @cached_property
    def sequence_key(self) -> DisclosureKey:
        return AnonymousKey()

    @property
    def identity_part(self) -> object:
        return self.value

    def column_value(self, column: ViewColumn) -> DisclosureText:
        return DisclosureText.describe(column.current(self.value))

    @handles(object, str, bytes)
    @classmethod
    def scalar(cls, value: object) -> DisclosureText:
        return DisclosureText(value)

    @classmethod
    def record(cls, value: object) -> DisclosureText:
        return RecordText(value)

    @classmethod
    def identified_record(cls, value: object, identity: tuple[FieldPlan, ...]) -> DisclosureText:
        return IdentifiedRecordText(value, identity)

    @handles(type)
    @classmethod
    def class_value(cls, value: type) -> DisclosureText:
        return ClassText(value)

    @handles(FamilyMeta)
    @classmethod
    def family_value(cls, value: type[DeclaredFamily]) -> DisclosureText:
        return FamilyText(value)

    @handles(dict, MappingProxyType)
    @classmethod
    def mapping(cls, value: Mapping[Hashable, object]) -> DisclosureText:
        return MappingText(value)

    @handles(list, tuple, frozenset)
    @classmethod
    def sequence(cls, value: Collection[object]) -> DisclosureText:
        return SequenceText(value)


@dataclass(frozen=True)
class DisclosureEntry:
    key: DisclosureKey
    label: str
    value: DisclosureText


class StructuredText(DisclosureText):
    """An expanded branch borrows children prepared once by its source owner."""

    @property
    @abstractmethod
    def children(self) -> tuple[DisclosureEntry, ...]: ...


@dataclass(frozen=True)
class ViewFacts:
    """Declared displayed values; paint-time fields may exclude publication comparison."""

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
    view: type[ViewExclusion]
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
    def row_values(self) -> tuple[DisclosureText, ...]:
        return tuple(DisclosureText.describe(record) for record in self.records)

    @cached_property
    def entities(self) -> Mapping[DisclosureKey, R]:
        entities = {text.sequence_key: record for text, record in zip(self.row_values, self.records)}
        if len(entities) != len(self.records):
            raise ValueError("A declared table requires unique record identities")
        return MappingProxyType(entities)

    @cached_property
    def cells(self) -> tuple[tuple[DisclosureText, ...], ...]:
        return tuple(tuple(text.column_value(column) for column in self.declared_columns)
                     for text in self.row_values)

    @cached_property
    def texts(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(cell.summary for cell in row) for row in self.cells)

    @cached_property
    def facts(self) -> ViewFacts:
        retained = tuple(index for index, column in enumerate(self.declared_columns)
                         if not column.excluded_in(PublicationExclude))
        return ViewFacts(tuple(self.declared_columns[index].label for index in retained),
                         tuple(tuple(row[index].summary for index in retained) for row in self.cells))

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

    def plain_lines(self) -> tuple[str, ...]:
        """One line per row, every cell kept, for a reader with no columns to align to."""
        return tuple(" · ".join(" / ".join(cell.splitlines()) for cell in row if cell)
                     for row in self.texts)

    def folded_lines(self) -> tuple[str, ...]:
        """Every cell of ``plain_lines``, with each repeated value printed once.

        A column whose value repeats across rows is a shared fact of those rows, not a fact of
        each row: rows agreeing on every repeated column print under one labelled line, and
        each row keeps only its own cells. Nineteen units awaiting the same proof read as one
        line and nineteen names, not nineteen copies of the same sentence.
        """
        rows = tuple(tuple(" / ".join(cell.splitlines()) for cell in row) for row in self.texts)
        shared = tuple(index for index, column in enumerate(zip(*rows))
                       if len(set(column)) < len(rows))
        if len(rows) < 2 or not shared:
            return self.plain_lines()
        groups: dict[tuple[str, ...], list[tuple[tuple[str, ...], ...]]] = {}
        for row in self.texts:
            cells = tuple(tuple(cell.splitlines()) for cell in row)
            groups.setdefault(tuple(" / ".join(cells[index]) for index in shared), []).append(cells)
        lines: list[str] = []
        for values, members in groups.items():
            own = tuple(index for index in range(len(self.columns)) if index not in shared)
            # A cell listing items that all share one state (`P821 merged…`, `P847 merged…`)
            # names the state once in the group's line, and the cell keeps the item names.
            tails = {index: self.shared_tail(tuple(line for row in members for line in row[index]))
                     for index in own}
            label = " · ".join((*(f"{self.columns[index].label}: {value}"
                                  for index, value in zip(shared, values) if value),
                                *(f"{self.columns[index].label}: … {tail}"
                                  for index, tail in tails.items() if tail)))
            lines.append(f"{label} ({len(members)})")
            lines.extend("  " + " · ".join(
                " / ".join(line.removesuffix(f" {tails[index]}") if tails[index] else line
                           for line in row[index])
                for index in own if row[index]) for row in members)
        return tuple(lines)

    @staticmethod
    def shared_tail(lines: tuple[str, ...]) -> str:
        """The words after the first that every one of at least two lines ends with."""
        tails = {line.partition(" ")[2] for line in lines}
        return tails.pop() if len(lines) > 1 and len(tails) == 1 else ""

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
        return tuple(("\n".join(f"{column.label}: {cell.summary}"
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


class RecordText(StructuredText):
    @cached_property
    def fields(self) -> tuple[FieldPlan, ...]:
        """Identity-only summaries never remove original detail children."""
        return tuple(plan for plan in FieldCodec.field_plans(type(self.value)) if plan.records)

    @cached_property
    def _field_values(self) -> dict[str, DisclosureText]:
        return {}

    def column_value(self, column: ViewColumn) -> DisclosureText:
        if column.name not in self._field_values:
            self._field_values[column.name] = super().column_value(column)
        return self._field_values[column.name]

    @cached_property
    def children(self) -> tuple[DisclosureEntry, ...]:
        return tuple(DisclosureEntry(FieldKey(type(self.value), plan.name), plan.label,
                                     self.column_value(plan)) for plan in self.fields)

    @cached_property
    def summary(self) -> str:
        return " · ".join(child.value.summary for child in self.children)


@dataclass(frozen=True)
class IdentifiedRecordText(RecordText):
    identity: tuple[FieldPlan, ...]

    @cached_property
    def identity_values(self) -> tuple[DisclosureText, ...]:
        return tuple(self.column_value(plan) for plan in self.identity)

    @cached_property
    def sequence_key(self) -> EntityKey:
        return EntityKey(type(self.value), tuple(part.identity_part for part in self.identity_values))

    @property
    def identity_part(self) -> EntityKey:
        return self.sequence_key

    @cached_property
    def summary(self) -> str:
        return " · ".join(part.summary for part in self.identity_values)


@dataclass(frozen=True)
class ClassText(DisclosureText):
    value: type

    @cached_property
    def summary(self) -> str:
        return self.value.__name__


@dataclass(frozen=True)
class FamilyText(ClassText):
    value: type[DeclaredFamily]

    @cached_property
    def summary(self) -> str:
        return self.value.declared_name


class CollectionText(StructuredText):
    value: Collection[object]
    noun: ClassVar[str] = "item"

    @cached_property
    def summary(self) -> str:
        count = len(self.value)
        return f"{count} {self.noun}{'s' if count != 1 else ''}"


@dataclass(frozen=True)
class SequenceText(CollectionText):
    value: Collection[object]

    @cached_property
    def children(self) -> tuple[DisclosureEntry, ...]:
        entries = []
        for index, value in enumerate(self.value):
            text = DisclosureText.describe(value)
            entries.append(DisclosureEntry(text.sequence_key, str(index + 1), text))
        return tuple(entries)


@dataclass(frozen=True)
class MappingText(CollectionText):
    value: Mapping[Hashable, object]
    noun = "field"

    @cached_property
    def children(self) -> tuple[DisclosureEntry, ...]:
        return tuple(DisclosureEntry(MappingKey(key), str(key), DisclosureText.describe(value))
                     for key, value in self.value.items())
